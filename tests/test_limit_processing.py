"""The worker's retry-or-reject decision for queued orders (limits.process)."""
import time
from datetime import datetime, timezone
from decimal import Decimal as D
from uuid import uuid4

import pytest
from test_service import database, seed, FakeMarket
from app.db import Session, LimitOrder, Position, Transaction
from app.limits import process
from app.market import MarketError


def queue(uid, symbol='AAPL', side='buy', quantity=1, order_type='limit', limit_price='1000', request_id=None):
    with Session.begin() as db:
        row = LimitOrder(user_id=uid, request_id=request_id or str(uuid4()), symbol=symbol, side=side, quantity=quantity,
                         limit_price=D(limit_price), order_type=order_type, use_max=False, status='pending',
                         created_at=datetime.now(timezone.utc))
        db.add(row); db.flush()
        return row.id


def state(order_id):
    with Session() as db:
        row = db.get(LimitOrder, order_id)
        return row.status, row.reason


def hold(uid, symbol, quantity):
    with Session.begin() as db:
        db.add(Position(user_id=uid, symbol=symbol, quantity=quantity, average_cost=D(1), native_average_cost=D(1)))


class StaleMarket(FakeMarket):
    def quote(self, symbol):
        return super().quote(symbol) | {'timestamp': int(time.time()) - 100000}


class DownMarket(FakeMarket):
    def quote(self, symbol): raise MarketError('공급자 응답 없음')


def test_a_fillable_order_fills():
    uid = seed()
    oid = queue(uid)
    assert process(FakeMarket()) == 1 and state(oid) == ('filled', None)


def test_retryable_rejections_stay_pending_with_their_reason():
    uid = seed()
    oid = queue(uid)
    assert process(StaleMarket()) == 0 and state(oid) == ('pending', '시세가 만료되었습니다.')
    assert process(DownMarket()) == 0 and state(oid) == ('pending', '시세 조회 대기')
    assert process(FakeMarket()) == 1 and state(oid)[0] == 'filled'


def test_permanent_rejections_are_rejected_once(monkeypatch):
    uid = seed()
    too_big = queue(uid, quantity=5000)
    no_shares = queue(uid, side='sell', quantity=3, limit_price='1')
    # A market order whose request id already filled a different order.
    used = str(uuid4())
    with Session.begin() as db:
        db.add(Transaction(user_id=uid, request_id=used, symbol='AAPL', side='sell', quantity=1, price=D(100),
                           native_price=D(100), quote_time=datetime.now(timezone.utc), created_at=datetime.now(timezone.utc)))
    conflict = queue(uid, order_type='market', request_id=used)
    hold(uid, 'KR:005930', 1)
    monkeypatch.setenv('KR_SELL_FEE_BPS', '9999'); monkeypatch.setenv('KR_SELL_TAX_BPS', '9999')
    fees = queue(uid, symbol='KR:005930', side='sell', limit_price='1')
    assert process(FakeMarket()) == 0
    assert state(too_big) == ('rejected', 'USD 잔액이 부족합니다. 필요한 통화로 먼저 환전하세요.')
    assert state(no_shares) == ('rejected', '보유 수량이 부족합니다.')
    assert state(conflict) == ('rejected', '동일 주문 ID에 다른 주문을 사용할 수 없습니다.')
    assert state(fees) == ('rejected', '수수료/세금 설정을 확인하세요.')
    # Rejected orders are not picked up again.
    assert process(FakeMarket()) == 0


def test_an_unexpected_error_is_not_classified(monkeypatch):
    uid = seed()
    oid = queue(uid)
    class Broken(FakeMarket):
        def quote(self, symbol): raise RuntimeError('bug')
    with pytest.raises(RuntimeError):
        process(Broken())
    assert state(oid) == ('pending', None)
