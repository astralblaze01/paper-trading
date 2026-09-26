"""Daily performance snapshots, benchmarks, period returns and fill metadata."""
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from uuid import uuid4

import pytest
from sqlalchemy import select, func

from test_service import database, client, register  # noqa: F401  (shared fixtures)
from app import main
from app.db import Session, User, Position, Wallet, PerformanceSnapshot, BenchmarkSnapshot, SnapshotRun, Transaction
from app.market import MarketError
from app.performance_snapshots import capture_daily_snapshots, series, scheduled_for, SEOUL
from app.portfolio import performance_return, portfolio

NOW = datetime.now(timezone.utc)
DAY = NOW.astimezone(SEOUL).date()


class FX:
    def current_rate(self, source='USD', target='KRW'):
        return {'rate': D(1400), 'date': '2026-09-24'}


class Market:
    client = type('Client', (), {'close': lambda self: None})()
    def __init__(self, quotes): self.quotes = quotes
    def quote(self, symbol):
        if symbol not in self.quotes: raise MarketError('no price')
        return dict(self.quotes[symbol])


def us(price, age=60, **extra):
    return {'price': D(price), 'native_price': D(price), 'currency': 'USD', 'timestamp': int(NOW.timestamp()) - age,
            'stale': False, 'source': 'KIS', 'price_mode': 'overnight_rest', 'session': 'overnight'} | extra


def kr(price, age=60):
    return {'price': D(price) / 1400, 'native_price': D(price), 'currency': 'KRW', 'timestamp': int(NOW.timestamp()) - age,
            'stale': False, 'source': 'KIS 분봉 / ECB 일별 기준환율'}


BENCH = {'SPY': us('767.45'), 'QQQ': us('742.39'), 'KR:069500': kr('45000')}


def account(name, usd='1000', krw='500000', holdings=(), admin=False, active=True, contributions='0'):
    with Session.begin() as db:
        u = User(username=name, password_hash='x', cash=D(usd), initial_usd=D(100000), initial_krw=D(140000000),
                 net_contributions_krw=D(contributions), is_admin=admin, active=active)
        db.add(u); db.flush()
        db.add_all([Wallet(user_id=u.id, currency='USD', balance=D(usd)), Wallet(user_id=u.id, currency='KRW', balance=D(krw))])
        for symbol, quantity, cost in holdings:
            db.add(Position(user_id=u.id, symbol=symbol, quantity=quantity, average_cost=D(cost), native_average_cost=D(cost)))
        return u.id


def run(market, now=NOW, force=True):
    return capture_daily_snapshots(market, FX(), now=now, force=force)


def snapshot(uid):
    with Session() as db:
        return db.scalar(select(PerformanceSnapshot).where(PerformanceSnapshot.user_id == uid))


def test_snapshot_values_positions_fx_and_quote_metadata():               # 37-1, 37-5, 37-6, 37-7
    uid = account('alice', holdings=[('AAPL', 10, '200'), ('KR:005930', 20, '70000')], contributions='1400000')
    assert run(Market({'AAPL': us('231.15'), 'KR:005930': kr('81500')} | BENCH)) == 'complete'
    s = snapshot(uid)
    expected = D(500000) + D(1000) * 1400 + D('231.15') * 10 * 1400 + D(81500) * 20
    assert s.equity_krw == expected and s.equity_usd == (expected / 1400).quantize(D('.0001'))
    assert (s.cash_usd, s.cash_krw, s.fx_rate, s.fx_date) == (D(1000), D(500000), D(1400), '2026-09-24')
    assert s.cumulative_return_pct == performance_return(expected, D(140000000), D(1400000)).quantize(D('.00000001'))
    assert s.snapshot_date == DAY and s.net_contributions_krw == D(1400000)
    aapl = s.positions['AAPL']
    assert (aapl['quantity'], aapl['currency'], set(aapl)) == (10, 'USD', {'quantity', 'native_price', 'currency', 'market_value_native', 'market_value_krw', 'average_cost'})
    assert (D(aapl['native_price']), D(aapl['market_value_native']), D(aapl['market_value_krw']), D(aapl['average_cost'])) == (D('231.15'), D('2311.5'), D(3236100), D(200))
    assert s.positions['KR:005930']['currency'] == 'KRW' and D(s.positions['KR:005930']['native_price']) == 81500
    meta = s.quote_metadata['AAPL']
    assert (meta['source'], meta['stale'], meta['price_mode'], meta['session'], meta['fallback']) == ('KIS', False, 'overnight_rest', 'overnight', False)
    assert meta['timestamp'] == int(NOW.timestamp()) - 60
    assert s.quality == {'complete': True, 'stale': False, 'oldest_quote_at': s.oldest_quote_at.isoformat(),
                         'missing_symbols': [], 'fallback_symbols': []}
    assert s.baseline['baseline_note'] == 'registration'


def test_one_snapshot_per_account_per_day():                              # 37-2
    uid = account('alice')
    market = Market(BENCH)
    assert run(market) == 'complete'
    assert run(market, force=False, now=NOW + timedelta(hours=24) - timedelta(hours=24)) in ('done', 'waiting')
    assert run(market) == 'complete'
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(PerformanceSnapshot).where(PerformanceSnapshot.user_id == uid)) == 1
        assert db.scalar(select(func.count()).select_from(BenchmarkSnapshot)) == 3


def test_all_active_regular_accounts_but_not_admins():                     # 37-3
    ids = [account('alice'), account('bob')]
    admin, inactive = account('root', admin=True), account('gone', active=False)
    run(Market(BENCH))
    with Session() as db:
        stored = set(db.scalars(select(PerformanceSnapshot.user_id)))
        assert stored == set(ids) and admin not in stored and inactive not in stored
        assert db.get(SnapshotRun, DAY).eligible == 2


def test_missing_or_ancient_prices_are_never_stored_as_complete():         # 37-4
    good = account('alice')
    missing = account('bob', holdings=[('TSLA', 1, '300')])
    ancient = account('carol', holdings=[('MSFT', 1, '400')])
    weekend = account('dave', holdings=[('NVDA', 1, '200')])
    market = Market(BENCH | {'MSFT': us('497', age=8 * 86400), 'NVDA': us('224.58', age=2 * 86400, stale=True)})
    assert run(market) == 'partial'
    assert snapshot(good) and snapshot(missing) is None and snapshot(ancient) is None
    # A weekend valuation on the last real quote is kept and marked stale.
    s = snapshot(weekend)
    assert s.stale and s.quality['stale'] and s.quote_metadata['NVDA']['stale']
    with Session() as db:
        r = db.get(SnapshotRun, DAY)
        assert r.failed == 2 and 'TSLA' in r.errors['bob'] and 'MSFT' in r.errors['carol'] and r.finished_at is None
    # The next attempt picks up only what is still missing.
    market.quotes['TSLA'] = us('380')
    run(market)
    assert D(snapshot(missing).positions['TSLA']['native_price']) == 380


def test_benchmarks_are_stored_once_a_day():                               # 37-10
    run(Market(BENCH))
    with Session() as db:
        rows = {b.symbol: b for b in db.scalars(select(BenchmarkSnapshot))}
    assert set(rows) == {'SPY', 'QQQ', 'KR:069500'}
    assert rows['SPY'].price == D('767.45') and rows['SPY'].currency == 'USD' and rows['SPY'].fx_rate == D(1400)
    assert rows['KR:069500'].currency == 'KRW' and rows['SPY'].price_mode == 'overnight_rest'


def test_schedule_waits_for_the_hour_and_leaves_missed_days_empty(monkeypatch):
    monkeypatch.setenv('DAILY_SNAPSHOT_HOUR', '7')
    monkeypatch.setenv('DAILY_SNAPSHOT_RETRY_MINUTES', '120')
    account('alice', holdings=[('TSLA', 1, '300')])
    day, scheduled = scheduled_for(NOW)
    assert run(Market(BENCH), now=scheduled - timedelta(minutes=1), force=False) == 'waiting'
    assert run(Market({}), now=scheduled + timedelta(minutes=1), force=False) == 'partial'
    assert run(Market(BENCH), now=scheduled + timedelta(hours=3), force=False) == 'missed'
    with Session() as db: assert db.get(SnapshotRun, day).outcome == 'missed'
    # An admin can still capture the day by hand; the run then says what happened.
    assert run(Market(BENCH | {'TSLA': us('380')})) == 'complete'
    with Session() as db: assert db.get(SnapshotRun, day).outcome == 'complete'


def seed_series(uid, rows):
    with Session.begin() as db:
        for day, equity, contributions, baseline in rows:
            db.add(PerformanceSnapshot(
                user_id=uid, snapshot_date=day, snapshot_at=NOW, scheduled_for=NOW, equity_krw=D(equity),
                equity_usd=D(equity) / 1400, cash_krw=0, cash_usd=0, net_contributions_krw=D(contributions),
                initial_equity_krw=D(baseline), cumulative_return_pct=performance_return(D(equity), D(baseline), D(contributions)),
                fx_rate=D(1400), fx_date='2026-09-24', positions={}, quote_metadata={}, quality={}, stale=False,
                baseline={'initial_equity_krw': str(baseline), 'performance_since': None}, created_at=NOW))


def test_period_returns_remove_grants_and_break_on_rebase():                # 37-8, 37-9
    uid = account('alice')
    d = date(2026, 9, 1)
    seed_series(uid, [(d, 100, 0, 100), (d + timedelta(1), 110, 0, 100),
                      # A 50 grant arrives: equity jumps to 165 but only 5 of it was earned.
                      (d + timedelta(2), 165, 50, 100), (d + timedelta(9), 176, 50, 100)])
    r = series(uid, d, d + timedelta(9))
    assert [p['daily_return_pct'] for p in r['snapshots']][:3] == [None, D(10), (D(115) / D(110) - 1) * 100]
    assert r['period_return_pct'] == D(26) and not r['baseline_changed']   # (176-50)/100-1
    week = series(uid, d + timedelta(2), d + timedelta(9))
    assert week['period_return_pct'] == (D(176) / D(165) - 1) * 100
    seed_series(uid, [(d + timedelta(10), 200, 0, 200), (d + timedelta(11), 210, 0, 200)])
    r = series(uid, d, d + timedelta(11))
    assert r['baseline_changed'] and r['period_start'] == d + timedelta(10) and r['period_return_pct'] == D(5)
    assert r['snapshots'][4]['daily_return_pct'] is None


def test_account_return_differs_by_basis_when_the_rate_moves():
    # $100,000 started at 1,300 KRW/USD and is still held as dollars; the rate is now 1,400.
    uid = account('holder', usd='100000', krw='0')
    with Session.begin() as db: db.get(User, uid).initial_krw = D(130000000)
    p = portfolio(uid, Market({}), FX())
    assert p['pnl'] == D(10000000) and float(p['return_pct']) == pytest.approx(100 / 13)
    # In dollars nothing was earned: the whole KRW gain is the exchange rate.
    assert (p['pnl_usd'], p['return_pct_usd'], p['initial_usd']) == (0, 0, 100000)


def test_snapshots_carry_the_usd_basis_and_old_rows_have_none():
    uid = account('alice', usd='101000', krw='0', contributions='1400000')
    with Session.begin() as db: db.get(User, uid).net_contributions_usd = D(1000)
    assert run(Market(BENCH)) == 'complete'
    s = snapshot(uid)
    assert (s.initial_equity_usd, s.net_contributions_usd) == (D(100000), D(1000))
    d = DAY - timedelta(2)
    seed_series(uid, [(d, 140000000, 0, '140000000.0000')])   # recorded before the USD columns existed
    r = series(uid, d, DAY)
    assert r['snapshots'][0]['cumulative_return_usd_pct'] is None
    assert r['snapshots'][-1]['cumulative_return_usd_pct'] == 0
    assert r['period_return_usd_pct'] is None and r['period_return_pct'] is not None


def test_period_return_in_usd_removes_grants():
    uid = account('bob')
    d = date(2026, 9, 1)
    seed_series(uid, [(d, 140000, 0, 140000), (d + timedelta(1), 210000, 70000, 140000)])
    with Session.begin() as db:
        for row in db.scalars(select(PerformanceSnapshot).where(PerformanceSnapshot.user_id == uid)):
            row.initial_equity_usd, row.net_contributions_usd = D(100), row.net_contributions_krw / 1400
    r = series(uid, d, d + timedelta(1))
    assert r['period_return_usd_pct'] == 0 and r['snapshots'][-1]['cumulative_return_usd_pct'] == 0


def test_performance_api_periods_and_benchmark_excess(client):
    token = register(client)
    with Session() as db: uid = db.scalar(select(User.id).where(User.username == 'alice'))
    today = datetime.now(timezone.utc).astimezone(SEOUL).date()
    seed_series(uid, [(today - timedelta(40), 100, 0, 100), (today - timedelta(7), 110, 0, 100), (today, 121, 0, 100)])
    with Session.begin() as db:
        for day, price, fx in ((today - timedelta(7), 100, 1400), (today, 105, 1440)):
            db.add(BenchmarkSnapshot(symbol='SPY', snapshot_date=day, price=D(price), currency='USD', quote_time=NOW,
                                     source='KIS', stale=False, fx_rate=D(fx), fx_date='2026-09-24', created_at=NOW))
    week = client.get('/api/performance/me?period=1W').json()
    assert [p['date'] for p in week['snapshots']] == [str(today - timedelta(7)), str(today)]
    assert float(week['period_return_pct']) == pytest.approx(10)
    spy = week['benchmarks']['SPY']
    assert float(spy['return_pct']) == pytest.approx(5) and float(spy['return_krw_pct']) == pytest.approx(8)
    assert float(spy['excess_return_pct']) == pytest.approx(2)
    assert float(client.get('/api/performance/me?period=ALL').json()['period_return_pct']) == pytest.approx(21)
    assert len(client.get('/api/performance/alice?period=1M').json()['snapshots']) == 2
    assert client.get(f'/api/performance/me?from={today}&to={today - timedelta(1)}').status_code == 422
    assert client.get('/api/performance/nobody').status_code == 404


def test_fill_records_session_source_and_request_time(client):
    headers = {'x-csrf-token': register(client)}
    class Quoted:
        key = 'test'
        client = Market.client
        def status(self): return {'us': True, 'kr': True}
        def quote(self, symbol):
            return {'symbol': symbol, 'price': D(100), 'currency': 'USD', 'timestamp': int(time.time()), 'stale': False,
                    'source': 'KIS', 'price_mode': 'trade_stream', 'session': 'regular', 'session_tradeable': True}
    main.market = Quoted()
    before = datetime.now(timezone.utc)
    r = client.post('/api/orders', headers=headers, json={'symbol': 'AAPL', 'side': 'buy', 'quantity': 1, 'request_id': str(uuid4())})
    assert r.status_code == 200, r.text
    with Session() as db: t = db.scalar(select(Transaction))
    assert (t.market_session, t.quote_source, t.price_mode, t.quote_stale) == ('regular', 'KIS', 'trade_stream', False)
    assert before <= t.order_requested_at <= t.created_at
    assert client.get('/api/transactions').json()[0]['price_mode'] == 'trade_stream'


def test_admin_status_and_manual_run(client):
    headers = {'x-csrf-token': register(client, 'root')}
    with Session.begin() as db: db.scalar(select(User).where(User.username == 'root')).is_admin = True
    account('alice')
    main.market = Market(BENCH)
    main.fx = FX()
    r = client.post('/api/admin/performance-snapshots/run', headers=headers).json()
    assert r['result'] == 'complete' and r['today_run']['succeeded'] == 1 and r['today_run']['benchmarks']['SPY'] == 'stored'
    status = client.get('/api/admin/performance-snapshots').json()
    assert status['collection_started'] == str(DAY) and status['total_snapshots'] == 1


def test_account_deletion_removes_its_snapshots():
    from app.accounts import delete_account_data
    uid = account('alice')
    run(Market(BENCH))
    with Session.begin() as db: delete_account_data(db, db.get(User, uid))
    assert snapshot(uid) is None
