"""AI trader accounts: their decision log, and the administrator's read-only view of everything they own."""
from uuid import uuid4
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from test_service import database, client, register
from app import main
from app.db import Session, User, AiDecision, Transaction
from app.accounts import delete_account_data

RECORD = {'status': 'ok', 'summary': '삼성전자 매수', 'data': {'model': 'm', 'analysis': '추세', 'results': [], 'research_data': [[{'request': {}, 'data': 'big'}]]}}


def mark(name, **flags):
    with Session.begin() as db:
        u = db.scalar(select(User).where(User.username == name))
        for k, v in flags.items(): setattr(u, k, v)


def ai_and_admin():
    """ai_bot (an AI account, signed in) and admin (signed in), each with its own client and CSRF token."""
    bot = TestClient(main.app, base_url='https://testserver'); bot_token = register(bot, 'ai_bot'); mark('ai_bot', is_ai=True)
    boss = TestClient(main.app, base_url='https://testserver'); boss_token = register(boss, 'boss'); mark('boss', is_admin=True)
    return bot, bot_token, boss, boss_token


def test_only_an_ai_account_can_log_decisions(client):
    token = register(client)
    assert client.post('/api/ai/decisions', headers={'x-csrf-token': token}, json=RECORD).status_code == 403
    bot, bot_token, *_ = ai_and_admin()
    assert bot.post('/api/ai/decisions', headers={'x-csrf-token': bot_token}, json=RECORD).status_code == 200
    rows = bot.get('/api/ai/decisions').json()['rows']
    # Lists leave the bulky research data out; one record has all of it.
    assert rows[0]['summary'] == '삼성전자 매수' and 'research_data' not in rows[0]['data']
    assert bot.get(f"/api/ai/decisions/{rows[0]['id']}").json()['data']['research_data'][0][0]['data'] == 'big'
    big = RECORD | {'data': {'blob': 'x' * 250_000}}
    assert bot.post('/api/ai/decisions', headers={'x-csrf-token': bot_token}, json=big).status_code == 413


def test_an_admin_reads_everything_an_ai_account_owns(client):
    bot, bot_token, boss, _ = ai_and_admin()
    bot.post('/api/ai/decisions', headers={'x-csrf-token': bot_token}, json=RECORD)
    assert bot.post('/api/orders', headers={'x-csrf-token': bot_token}, json=main.Order(symbol='AAPL', side='buy', quantity=2, request_id=uuid4()).model_dump(mode='json')).status_code == 200
    view = {'x-view-as': 'AI_BOT'}
    assert boss.get('/api/portfolio', headers=view).json()['username'] == 'ai_bot'
    assert boss.get('/api/portfolio').json()['username'] == 'boss'   # without the header: the admin's own
    assert boss.get('/api/transactions', headers=view).json()[0]['quantity'] == 2
    for path in ('/api/transactions/summary', '/api/transactions/months', '/api/fees', '/api/fx/history', '/api/limit-orders',
                 '/api/watchlist', '/api/dividends', '/api/profile', '/api/performance/me', '/api/ai/decisions'):
        assert boss.get(path, headers=view).status_code == 200, path
    listed = boss.get('/api/admin/ai').json()
    assert [a['username'] for a in listed] == ['ai_bot'] and listed[0]['decisions'] == 1 and listed[0]['last']['summary'] == '삼성전자 매수'


def test_the_view_is_read_only_admin_only_and_ai_only(client):
    bot, bot_token, boss, boss_token = ai_and_admin()
    view = {'x-view-as': 'ai_bot'}
    order = main.Order(symbol='AAPL', side='buy', quantity=1, request_id=uuid4()).model_dump(mode='json')
    # Nothing can be done in the AI's name.
    r = boss.post('/api/orders', headers=view | {'x-csrf-token': boss_token}, json=order)
    assert r.status_code == 403
    with Session() as db: assert db.scalar(select(func.count()).select_from(Transaction)) == 0
    # Only administrators may look, and only at AI accounts.
    token = register(client)
    assert client.get('/api/portfolio', headers=view).status_code == 403
    assert boss.get('/api/portfolio', headers={'x-view-as': 'alice'}).status_code == 404
    assert client.get('/api/admin/ai').status_code == 403
    # Admin endpoints keep the admin's own identity rules: the header does not lend them to anyone.
    assert boss.get('/api/admin', headers=view).status_code == 403


def test_deleting_an_ai_account_removes_its_log(client):
    bot, bot_token, *_ = ai_and_admin()
    bot.post('/api/ai/decisions', headers={'x-csrf-token': bot_token}, json=RECORD)
    with Session.begin() as db: delete_account_data(db, db.scalar(select(User).where(User.username == 'ai_bot')))
    with Session() as db: assert db.scalar(select(func.count()).select_from(AiDecision)) == 0
