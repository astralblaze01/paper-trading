"""Stock splits: the KIS schedule, restating holdings once on the day, and replays across them."""
from datetime import date, datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select

from test_service import database, client, register, FakeMarket  # noqa: F401  (shared fixtures)
from app import splits, dividends
from app.db import Session, User, Position, Transaction, Wallet, LimitOrder, SplitEvent, SplitApplication
from app.portfolio import dual_costs


def uid_of(name='alice'):
    with Session() as db: return db.scalar(select(User.id).where(User.username == name))


def trade(c, token, symbol='AAPL', quantity=1, side='buy'):
    r = c.post('/api/orders', headers={'x-csrf-token': token}, json={'symbol': symbol, 'side': side, 'quantity': quantity, 'request_id': str(uuid4())})
    assert r.status_code == 200, r.text


def test_schedule_reads_korean_face_value_changes_and_us_split_rights():
    class KIS:
        def get(self, path, tr, params, ttl, tr_cont=''):
            if path.endswith('rev-split'):
                return {'output1': [
                    {'sht_cd': '033050', 'inter_bf_face_amt': '000001000', 'inter_af_face_amt': '000005000', 'list_dt': '2026/10/30'},  # 5 → 1
                    {'sht_cd': '033050', 'inter_bf_face_amt': '000005000', 'inter_af_face_amt': '000000100', 'list_dt': ''},           # not listed yet
                    {'sht_cd': '999999', 'inter_bf_face_amt': '000005000', 'inter_af_face_amt': '000000500', 'list_dt': '20261101'}]}
            kind = params['RGHT_TYPE_CD']
            rows = {'14': [{'pdno': 'MUU', 'acpl_bass_dt': '20260714', 'stck_alct_rt': '2000.000000000000'},
                           {'pdno': 'MUUX', 'acpl_bass_dt': '20260714', 'stck_alct_rt': '300'}],
                    '15': [{'pdno': 'MUU', 'acpl_bass_dt': '20261120', 'stck_alct_rt': '10.0'}]}.get(kind, [])
            return {'output': rows, '_tr_cont': 'D'}
    kr = splits.schedule('KR:033050', KIS(), date(2026, 10, 2), date(2025, 1, 1), date(2027, 1, 1))
    assert [(e['effective_date'], e['ratio']) for e in kr] == [(date(2026, 10, 30), D('0.2'))]
    us = splits.schedule('MUU', KIS(), date(2026, 10, 2), date(2025, 1, 1), date(2027, 1, 1))
    assert [(e['effective_date'], e['ratio']) for e in us] == [(date(2026, 7, 14), D(20)), (date(2026, 11, 20), D('0.1'))]


def add_split(symbol, day, ratio):
    with Session.begin() as db:
        db.add(SplitEvent(symbol=symbol, effective_date=day, ratio=D(ratio), source='test'))


def test_a_split_restates_holdings_and_reservations_once_on_its_day(client):
    token = register(client)
    trade(client, token, quantity=10)
    uid = uid_of()
    with Session() as db: before = db.get(Position, (uid, 'AAPL'))
    cost = before.native_average_cost
    r = client.post('/api/limit-orders', headers={'x-csrf-token': token},
                    json={'symbol': 'AAPL', 'side': 'buy', 'quantity': 3, 'limit_price': '90', 'trigger': 'below', 'request_id': str(uuid4())})
    assert r.status_code == 200, r.text
    add_split('AAPL', date(2026, 11, 2), 4)
    # Not before the day starts in New York (Nov 2, 00:00 EST = 05:00 UTC).
    assert splits.apply_due(FakeMarket(), datetime(2026, 11, 2, 4, 59, tzinfo=timezone.utc)) == 0
    assert splits.apply_due(FakeMarket(), datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)) == 1
    with Session() as db:
        p = db.get(Position, (uid, 'AAPL'))
        assert (p.quantity, p.native_average_cost) == (40, cost / 4)
        order = db.scalar(select(LimitOrder).where(LimitOrder.user_id == uid))
        assert (order.quantity, order.limit_price, order.status) == (12, D('22.5'), 'pending')
        a = db.scalar(select(SplitApplication))
        assert (a.before_quantity, a.after_quantity, a.cash) == (10, 40, 0)
    assert splits.apply_due(FakeMarket(), datetime(2026, 11, 3, tzinfo=timezone.utc)) == 0      # once only
    with Session() as db: assert db.get(Position, (uid, 'AAPL')).quantity == 40
    body = client.get('/api/splits').json()
    assert body['applied'][0]['after_quantity'] == 40 and body['applied'][0]['name'] == '애플'


def test_a_reverse_split_pays_the_fraction_in_cash(client):
    token = register(client)
    trade(client, token, quantity=15)
    uid = uid_of()
    with Session() as db: usd = db.get(Wallet, (uid, 'USD')).balance
    add_split('AAPL', date(2026, 11, 2), '0.1')
    # 15 shares → 1.5: one share, and half a share at today's $100 (FakeMarket) in cash.
    assert splits.apply_due(FakeMarket(), datetime(2026, 11, 3, tzinfo=timezone.utc)) == 1
    with Session() as db:
        assert db.get(Position, (uid, 'AAPL')).quantity == 1
        assert db.get(Wallet, (uid, 'USD')).balance - usd == D(50)
        assert db.get(User, uid).cash == db.get(Wallet, (uid, 'USD')).balance


def test_dividend_holdings_and_cost_basis_replay_across_a_split(client):
    token = register(client)
    trade(client, token, quantity=10)
    uid = uid_of()
    with Session.begin() as db:
        for t in db.scalars(select(Transaction).where(Transaction.user_id == uid)):
            t.created_at, t.usd_krw = datetime(2026, 10, 20, tzinfo=timezone.utc), D(1300)
    add_split('AAPL', date(2026, 11, 2), 4)
    splits.apply_due(FakeMarket(), datetime(2026, 11, 3, tzinfo=timezone.utc))
    with Session() as db:
        assert dividends.holding_at(db, uid, 'AAPL', datetime(2026, 11, 1, tzinfo=timezone.utc)) == 10      # before the split
        assert dividends.holding_at(db, uid, 'AAPL', datetime(2026, 12, 1, tzinfo=timezone.utc)) == 40      # after it
        trades = db.scalars(select(Transaction).where(Transaction.user_id == uid).order_by(Transaction.id)).all()
        book = dual_costs(trades, splits.applied(db))
    # Without the split the replay counted 10 shares against a holding of 40 and showed no KRW/USD basis.
    assert book['AAPL']['quantity'] == 40


def test_splits_before_this_feature_started_are_not_applied(client):
    token = register(client)
    trade(client, token, quantity=10)
    add_split('AAPL', date(2026, 7, 14), 20)
    assert splits.apply_due(FakeMarket(), datetime(2026, 11, 3, tzinfo=timezone.utc)) == 0
    with Session() as db: assert db.get(Position, (uid_of(), 'AAPL')).quantity == 10
