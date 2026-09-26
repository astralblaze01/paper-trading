"""The worker's retry-or-reject decision for queued orders (limits.process)."""
import time
from datetime import datetime, timezone
from decimal import Decimal as D
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from test_service import database, client, seed, FakeMarket
import app.limits
from app import main
from app.accounts import delete_account_data
from app.db import Session, LimitOrder, Position, Transaction, User
from app.limits import process
from app.market import MarketError
from app.security import WORKER_TOKEN_HEADER, worker_token


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


# Rows deleted between the pending listing and the per-order lock --------------

def member(name):
    with Session.begin() as db:
        u = User(username=name, password_hash='unused'); db.add(u); db.flush()
        return u.id


def vanish_before_lock(monkeypatch, victim, remove):
    """Commit remove() in another transaction when process() is about to lock `victim`,
    exactly as a concurrent admin clear or account deletion after the listing would."""
    real, armed = app.limits.lock_user, [True]
    def lock_user(db, uid):
        if uid == victim and armed:
            armed.clear()
            with Session.begin() as other:
                remove(other)
        return real(db, uid)
    monkeypatch.setattr(app.limits, 'lock_user', lock_user)


def drop_orders(uid):
    return lambda db: db.execute(delete(LimitOrder).where(LimitOrder.user_id == uid))


def drop_account(uid):
    return lambda db: delete_account_data(db, db.get(User, uid))


def test_an_order_deleted_after_listing_is_skipped_and_the_next_one_fills(monkeypatch):
    gone, kept = member('gone_order'), member('kept')
    gone_order, kept_order = queue(gone), queue(kept)
    vanish_before_lock(monkeypatch, gone, drop_orders(gone))
    assert process(FakeMarket()) == 1
    with Session() as db:
        assert db.get(LimitOrder, gone_order) is None   # not recreated or restored
    assert state(kept_order) == ('filled', None)


def test_an_account_deleted_after_listing_is_skipped_and_the_next_one_fills(monkeypatch):
    gone, kept = member('gone_user'), member('kept')
    queue(gone); kept_order = queue(kept)
    vanish_before_lock(monkeypatch, gone, drop_account(gone))
    assert process(FakeMarket()) == 1
    with Session() as db:
        assert db.get(User, gone) is None and db.scalar(select(LimitOrder).where(LimitOrder.user_id == gone)) is None
    assert state(kept_order) == ('filled', None)


def test_internal_jobs_run_every_job_when_a_queued_order_vanishes(client, monkeypatch):
    gone = member('gone_jobs')
    queue(gone)
    vanish_before_lock(monkeypatch, gone, drop_account(gone))
    ran = []
    monkeypatch.setenv('WEEKLY_ENABLED', 'true')
    monkeypatch.setattr(main, 'tick', lambda market, fx=None: ran.append('weekly') or 'waiting')
    monkeypatch.setattr(main, 'capture_daily_snapshots', lambda market, fx: ran.append('snapshots') or 'waiting')
    r = client.post('/internal/jobs', headers={WORKER_TOKEN_HEADER: worker_token(main.secret)})
    assert r.status_code == 200 and r.json() == {'filled': 0, 'weekly': 'waiting', 'snapshots': 'waiting'}
    assert ran == ['weekly', 'snapshots']


def test_internal_jobs_still_surface_an_unexpected_limit_error(client, monkeypatch):
    queue(member('broken'))
    class Broken(FakeMarket):
        def quote(self, symbol): raise RuntimeError('bug')
    monkeypatch.setattr(main, 'market', Broken())
    ran = []
    monkeypatch.setattr(main, 'capture_daily_snapshots', lambda market, fx: ran.append('snapshots'))
    with pytest.raises(RuntimeError):
        client.post('/internal/jobs', headers={WORKER_TOKEN_HEADER: worker_token(main.secret)})
    assert ran == []   # unchanged policy: a programming error fails the request instead of being hidden
