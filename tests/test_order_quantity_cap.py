"""One order is at most 1,000,000 shares, whichever way its quantity is given."""
from decimal import Decimal as D
from uuid import uuid4

from test_service import database, client, register, seed, order, FakeMarket
from app.db import Session, Position, User
from app.trading import execute_order, preview_order
from sqlalchemy import select


def hold(uid, quantity, symbol='AAPL'):
    with Session.begin() as db:
        db.add(Position(user_id=uid, symbol=symbol, quantity=quantity, average_cost=D(1), native_average_cost=D(1)))


def held(uid, symbol='AAPL'):
    with Session() as db:
        p = db.get(Position, (uid, symbol))
        return p.quantity if p else 0


def test_use_max_sell_is_capped_like_the_preview():
    uid = seed()
    hold(uid, 1500000)
    assert preview_order(uid, 'AAPL', 'sell', 1, FakeMarket())['max_quantity'] == 1000000
    o = order(side='sell', quantity=1, use_max=True)
    first = execute_order(uid, o, FakeMarket())
    assert first['quantity'] == 1000000 and held(uid) == 500000
    # A replay answers with the capped fill and sells nothing more.
    assert execute_order(uid, o, FakeMarket()) == {'id': first['id'], 'replayed': True, 'quantity': 1000000}
    assert held(uid) == 500000
    # The rest is sold by the next maximum order.
    assert execute_order(uid, order(side='sell', quantity=1, use_max=True), FakeMarket())['quantity'] == 500000


def test_use_max_sell_of_exactly_the_cap_sells_everything():
    uid = seed()
    hold(uid, 1000000)
    assert execute_order(uid, order(side='sell', quantity=1, use_max=True), FakeMarket())['quantity'] == 1000000
    assert held(uid) == 0


def test_direct_quantities_are_whole_shares_up_to_the_cap(client):
    headers = {'x-csrf-token': register(client)}
    with Session() as db: uid = db.scalar(select(User.id).where(User.username == 'alice'))
    hold(uid, 1000001)
    body = {'symbol': 'AAPL', 'side': 'sell'}
    for quantity in (1000001, 1.5, '2', 0):
        r = client.post('/api/orders', headers=headers, json=body | {'quantity': quantity, 'request_id': str(uuid4())})
        assert r.status_code == 422, quantity
    assert client.get('/api/order-preview?symbol=AAPL&side=sell&quantity=1000001').status_code == 422
    r = client.post('/api/orders', headers=headers, json=body | {'quantity': 1000000, 'request_id': str(uuid4())})
    assert r.status_code == 200 and r.json()['quantity'] == 1000000 and held(uid) == 1
