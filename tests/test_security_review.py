"""Regression coverage for authentication and untrusted request metadata."""
from types import SimpleNamespace

from sqlalchemy import select
from test_service import database, client, register
from app import main
from app.db import Session, User
from app.security import client_ip


def test_proxy_header_requires_explicit_trust(monkeypatch):
    request = SimpleNamespace(client=SimpleNamespace(host='192.0.2.1'),
                              headers={'x-real-ip': '203.0.113.9'})
    monkeypatch.delenv('TRUST_PROXY_HEADERS', raising=False)
    assert client_ip(request) == '192.0.2.1'
    monkeypatch.setenv('TRUST_PROXY_HEADERS', 'true')
    assert client_ip(request) == '203.0.113.9'
    for value in ('garbage', '203.0.113.9, 192.0.2.1', ''):
        request.headers['x-real-ip'] = value
        assert client_ip(request) == '192.0.2.1'
    request.headers['x-real-ip'] = '2001:0db8::1'
    assert client_ip(request) == '2001:db8::1'


def test_rotating_spoofed_ips_cannot_bypass_auth_limit(client, monkeypatch):
    monkeypatch.delenv('TRUST_PROXY_HEADERS', raising=False)
    for n in range(main.RATE_LIMITS['auth']):
        response = client.post('/api/login', headers={'x-real-ip': f'203.0.113.{n}'})
        assert response.status_code != 429
    assert client.post('/api/login', headers={'x-real-ip': '198.51.100.1'}).status_code == 429


def test_validation_errors_do_not_echo_secrets(client):
    token = client.get('/api/session').json()['csrf']
    for path, body in (
        ('/api/login', {'username': 'alice', 'password': 'secret'}),
        ('/api/register', {'username': 'alice', 'password': 'private-password-123'}),
        ('/api/login', {'username': 'alice', 'password': {'secret': 'private-password-123'}}),
    ):
        response = client.post(path, headers={'x-csrf-token': token}, json=body)
        assert response.status_code == 422
        for error in response.json()['detail']:
            assert set(error) <= {'type', 'loc', 'msg'}
        assert 'private-password-123' not in response.text
        assert '"secret"' not in response.text


def test_suspended_account_cannot_create_login_session(client):
    token = register(client)
    assert client.post('/api/logout', headers={'x-csrf-token': token}).status_code == 200
    with Session.begin() as db:
        db.scalar(select(User).where(User.username == 'alice')).active = False
    token = client.get('/api/session').json()['csrf']
    response = client.post('/api/login', headers={'x-csrf-token': token},
                           json={'username': 'alice', 'password': 'a-secure-password-123'})
    assert response.status_code == 403
    assert client.get('/api/session').json()['username'] is None
    assert client.get('/api/portfolio').status_code == 401
