"""Characterization tests for the admin overview and archive endpoints."""
import re
from sqlalchemy import select
from test_service import database, client, register
from test_accounting_refactor import admin, member
from app.db import Session, User, UserAdminNote, Wallet
from app.notices import TEMPLATES
from datetime import datetime, timezone


def test_admin_overview_keys_values_and_permission(client, monkeypatch):
    member_token = register(client, 'plainuser')
    assert client.get('/api/admin').status_code == 403
    client.post('/api/logout', headers={'x-csrf-token': member_token}, json={})
    client.cookies.clear()
    headers, admin_id = admin(client)
    uid = member('noted')
    now = datetime.now(timezone.utc)
    with Session.begin() as db:
        db.add(UserAdminNote(user_id=uid, note='메모', created_at=now, updated_at=now))
        db.add(Wallet(user_id=uid, currency='USD', balance=12))
    monkeypatch.setenv('KR_SELL_TAX_BPS', '15')
    body = client.get('/api/admin').json()
    assert list(body) == ['users', 'initial_usd', 'notices', 'notice', 'maintenance', 'notice_templates', 'fees', 'health',
                          'quotes', 'providers', 'counts', 'us_market', 'kr_market']
    rows = {u['username']: u for u in body['users']}
    assert [u['id'] for u in body['users']] == sorted(u['id'] for u in body['users'])
    assert list(rows['noted']) == ['id', 'username', 'active', 'admin', 'created_at', 'initial_usd', 'initial_krw', 'note', 'wallets']
    assert (rows['noted']['note'], rows['noted']['wallets']) == ('메모', {'USD': 12})
    assert rows['operator']['admin'] is True and rows['plainuser']['note'] == ''
    assert body['fees'] == {'FX_FEE_BPS': 10, 'FX_SPREAD_BPS': 5, 'US_BUY_FEE_BPS': 0, 'US_SELL_FEE_BPS': 0,
                            'KR_BUY_FEE_BPS': 0, 'KR_SELL_FEE_BPS': 0, 'KR_SELL_TAX_BPS': 15}
    assert list(body['counts']) == ['users', 'transactions', 'positions', 'pending_orders']
    assert body['counts'] == {'users': 3, 'transactions': 0, 'positions': 0, 'pending_orders': 0}
    assert body['notice'] is None and body['maintenance'] is False
    assert body['notice_templates'] == TEMPLATES
    assert body['health']['database'] == 'ok'


def test_archives_list_newest_first_with_fixed_keys(client):
    headers, admin_id = admin(client)
    uid = member('archived')
    assert client.get('/api/admin/archives').json() == []
    for label in ('시즌1', '시즌2'):
        assert client.post(f'/api/admin/users/{uid}/reset', headers=headers,
                           json={'label': label, 'confirmation': 'RESET'}).json() == {'ok': True, 'archived': True}
    rows = client.get('/api/admin/archives').json()
    assert [r['label'] for r in rows] == ['시즌2', '시즌1']
    assert list(rows[0]) == ['id', 'user_id', 'label', 'data', 'created_at', 'username']
    assert rows[0]['user_id'] == uid and rows[0]['data']['actor'] == admin_id
    # The audit page names the account, not its id.
    assert rows[0]['username'] == 'archived'
    # JSONB stores the snapshot, so its key order is Postgres's, not the code's.
    assert set(rows[1]['data']) == {'wallets', 'positions', 'initial_krw', 'actor'}


def test_admin_pages_are_the_app_page_at_their_own_urls(client):
    """Each admin section has its own URL; the page itself holds no data, the APIs check the account."""
    for path in ('/admin', '/admin/users', '/admin/accounts', '/admin/notices', '/admin/system', '/admin/settings', '/admin/audit'):
        r = client.get(path)
        assert r.status_code == 200, path
        assert 'data-page="admin"' in r.text and '{{' not in r.text
        assert re.search(rf'<meta property="og:url" content="https?://[^"/]+{path}">', r.text), path
    for path in ('/admin/nope', '/admin/users/1', '/admin/Users'):
        assert client.get(path).status_code == 404, path
    # Signed out or not an administrator, the data stays behind the API.
    assert client.get('/api/admin').status_code == 401
    register(client, 'curious')
    assert client.get('/admin/users').status_code == 200
    assert client.get('/api/admin').status_code == 403


def test_admin_password_reset_signs_out_and_forces_a_new_password(client):
    from fastapi.testclient import TestClient
    from app import main
    from app.db import AdminAudit
    def login(c, password):
        token = c.get('/api/session').json()['csrf']
        r = c.post('/api/login', headers={'x-csrf-token': token}, json={'username': 'forgetful', 'password': password})
        return r, c.get('/api/session').json()['csrf']  # signing in issues a new token
    with TestClient(main.app, base_url='https://testserver') as phone, TestClient(main.app, base_url='https://testserver') as laptop:
        register(phone, 'forgetful')  # signs in with 'a-secure-password-123'
        assert phone.get('/api/portfolio').status_code == 200
        headers, admin_id = admin(client)
        target = client.get('/api/admin').json()['users']
        uid = next(u['id'] for u in target if u['username'] == 'forgetful')
        assert client.post(f'/api/admin/users/{admin_id}/password', headers=headers).status_code == 409  # not their own
        r = client.post(f'/api/admin/users/{uid}/password', headers=headers)
        assert r.status_code == 200
        temporary = r.json()['temporary_password']
        assert len(temporary) == 12 and r.json()['username'] == 'forgetful'
        with Session() as db:
            audit = db.scalar(select(AdminAudit).where(AdminAudit.action == 'password_reset'))
            assert audit.target_id == uid and temporary not in str(audit.data)
        # The session from before the reset is signed out; the old password no longer works.
        assert phone.get('/api/portfolio').status_code == 401
        assert login(phone, 'a-secure-password-123')[0].status_code == 401
        r, token = login(phone, temporary)
        assert r.status_code == 200 and r.json()['password_temporary'] is True
        assert phone.get('/api/session').json()['password_temporary'] is True
        blocked = phone.get('/api/portfolio')
        assert blocked.status_code == 403 and blocked.headers['X-Password-Change'] == '1'
        assert login(laptop, temporary)[0].status_code == 200
        change = lambda body: phone.post('/api/account/password', headers={'x-csrf-token': token}, json=body)
        assert change({'current_password': 'wrong-password', 'new_password': 'brand-new-pass-1', 'new_password_confirm': 'brand-new-pass-1'}).status_code == 401
        assert change({'current_password': temporary, 'new_password': 'brand-new-pass-1', 'new_password_confirm': 'different-pass-1'}).status_code == 422
        assert change({'current_password': temporary, 'new_password': 'brand-new-pass-1', 'new_password_confirm': 'brand-new-pass-1'}).status_code == 200
        # This device carries on; the other one is signed out.
        assert phone.get('/api/portfolio').status_code == 200
        assert phone.get('/api/session').json()['password_temporary'] is False
        assert laptop.get('/api/portfolio').status_code == 401
        assert login(laptop, temporary)[0].status_code == 401
        assert login(laptop, 'brand-new-pass-1')[0].status_code == 200
    member_token = register(client, 'plainuser2')
    assert client.post(f'/api/admin/users/{uid}/password', headers={'x-csrf-token': member_token}).status_code == 403
