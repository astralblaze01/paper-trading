"""Characterization tests for the admin overview and archive endpoints."""
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
    assert list(body) == ['users', 'initial_usd', 'notice', 'notice_templates', 'maintenance', 'fees', 'health',
                          'providers', 'counts', 'us_market', 'kr_market']
    rows = {u['username']: u for u in body['users']}
    assert [u['id'] for u in body['users']] == sorted(u['id'] for u in body['users'])
    assert list(rows['noted']) == ['id', 'username', 'active', 'admin', 'initial_usd', 'initial_krw', 'note', 'wallets']
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
    assert list(rows[0]) == ['id', 'user_id', 'label', 'data', 'created_at']
    assert rows[0]['user_id'] == uid and rows[0]['data']['actor'] == admin_id
    # JSONB stores the snapshot, so its key order is Postgres's, not the code's.
    assert set(rows[1]['data']) == {'wallets', 'positions', 'initial_krw', 'actor'}
