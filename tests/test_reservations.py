"""Reservation orders (예약 주문): trigger directions, fills, validation, and notice types."""
from decimal import Decimal as D
from uuid import uuid4

import pytest
from sqlalchemy import select

from test_service import database, client, register, FakeMarket  # noqa: F401  (shared fixtures)
from app import main
from app.db import Session, User, LimitOrder, Transaction, Position
from app.limits import process, triggered


class PricedMarket(FakeMarket):
    def __init__(self, price): self.price = D(price)
    def quote(self, symbol): return super().quote(symbol) | {'price': self.price}


def reserve(c, token, **changes):
    body = {'symbol': 'AAPL', 'side': 'buy', 'quantity': 1, 'limit_price': '95', 'trigger': 'below', 'request_id': str(uuid4())} | changes
    return c.post('/api/limit-orders', headers={'x-csrf-token': token}, json=body)


def uid_of(name='alice'):
    with Session() as db: return db.scalar(select(User.id).where(User.username == name))


@pytest.mark.parametrize('side,order_type,price,target,fires', [
    ('buy', 'limit', 95, 95, True), ('buy', 'limit', 96, 95, False),      # 지정가 매수: at or below
    ('sell', 'limit', 105, 105, True), ('sell', 'limit', 104, 105, False),  # 익절 매도: at or above
    ('buy', 'stop', 110, 110, True), ('buy', 'stop', 109, 110, False),      # 돌파 매수: at or above
    ('sell', 'stop', 90, 90, True), ('sell', 'stop', 91, 90, False)])       # 손절 매도: at or below
def test_trigger_directions(side, order_type, price, target, fires):
    assert triggered(side, order_type, D(price), D(target)) is fires


def test_buy_below_waits_then_fills_at_the_market_price(client):
    token = register(client)
    r = reserve(client, token, quantity=2, limit_price='95')
    assert r.status_code == 200 and r.json()['status'] == 'pending'
    assert process(PricedMarket(100)) == 0
    assert process(PricedMarket('94.5')) == 1
    order = client.get('/api/limit-orders').json()[0]
    assert (order['status'], order['trigger'], D(order['filled_price'])) == ('filled', 'below', D('94.5')) and order['filled_at']
    with Session() as db:
        t = db.scalar(select(Transaction))
        assert (t.side, t.quantity, t.native_price) == ('buy', 2, D('94.5'))


def test_stop_loss_sells_when_the_price_falls(client):
    token = register(client)
    client.post('/api/orders', headers={'x-csrf-token': token}, json={'symbol': 'AAPL', 'side': 'buy', 'quantity': 3, 'request_id': str(uuid4())})
    assert reserve(client, token, side='sell', quantity=3, limit_price='90', trigger='below').status_code == 200
    assert process(PricedMarket(95)) == 0
    assert process(PricedMarket(89)) == 1
    with Session() as db: assert db.get(Position, (uid_of(), 'AAPL')) is None


def test_reservations_are_checked_when_placed(client):
    token = register(client)
    # Selling more than is held, or a won price below 1원, is refused up front.
    assert reserve(client, token, side='sell', trigger='above', limit_price='120').json()['detail'] == '보유 수량보다 많이 예약 매도할 수 없습니다.'
    assert reserve(client, token, symbol='KR:005930', limit_price='70000.5').status_code == 422
    assert reserve(client, token, trigger='sideways').status_code == 422
    # A replay of the same request returns the first order; 20 pending is the limit.
    body = {'request_id': str(uuid4())}
    first = reserve(client, token, **body).json()
    assert reserve(client, token, **body).json() == first | {'replayed': True}
    for _ in range(19): assert reserve(client, token).status_code == 200
    assert reserve(client, token).status_code == 409


def test_unfundable_buy_is_rejected_at_fill_time(client):
    token = register(client)
    assert reserve(client, token, quantity=5000, limit_price='100').status_code == 200   # $500,000 > $100,000
    assert process(PricedMarket(100)) == 0
    order = client.get('/api/limit-orders').json()[0]
    assert order['status'] == 'rejected' and 'USD' in order['reason']


def test_maintenance_notice_kinds(client):
    from test_features import admin_and_user
    headers, _ = admin_and_user(client)
    templates = client.get('/api/admin').json()['notice_templates']
    for kind, maintenance in (('maintenance_now', True), ('maintenance_done', False)):
        client.post('/api/admin/notice/clear', headers=headers)
        t = templates[kind]
        posted = client.post('/api/admin/notice', headers=headers, json={'kind': kind, 'title': t['title'], 'body': t['body']}).json()
        assert posted['posted']['label'] == t['label'] and posted['maintenance'] is maintenance
