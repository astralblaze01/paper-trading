"""The Finnhub regular-session trade stream: validation, slots, Redis writes and their readers."""
import asyncio
import json
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app import finnhub_stream as fs
from app import market_worker as mw
from app.quote_policy import assess
from app.us_session import NEW_YORK
from datetime import datetime


def ny(text):
    return datetime.fromisoformat(text).replace(tzinfo=NEW_YORK).timestamp()


def trade(symbol='AAPL', price=231.5, stamp='2026-10-05T10:00:00', **extra):
    return {'s': symbol, 'p': price, 't': int(ny(stamp) * 1000), 'v': 10, 'c': ['1']} | extra


class FakeWS:
    def __init__(self): self.sent = []
    async def send(self, message): self.sent.append(json.loads(message))


class FakeCache:
    client = None
    def __init__(self, snapshots=None):
        self.values, self.published, self.snapshots = {}, [], snapshots or {}
    def set_json(self, key, value, ttl): self.values[key] = value; return True
    def get_json(self, key): return self.snapshots.get(key)
    def store_quote(self, symbol, q, ttl): self.published.append(q); return True


# A trade becomes a quote ----------------------------------------------------

def test_regular_session_trade_becomes_a_stream_quote():
    now = ny('2026-10-05T10:00:01')
    q = fs.trade_quote(trade(), 'AAPL', 'c1', now=now)
    assert q['native_price'] == q['price'] == Decimal('231.5') and q['currency'] == 'USD'
    assert q['timestamp'] == int(ny('2026-10-05T10:00:00'))
    assert (q['origin'], q['stream'], q['source'], q['valid_sessions'], q['stream_conn']) == ('stream', 'finnhub', 'Finnhub', ['regular'], 'c1')


@pytest.mark.parametrize('bad', [
    trade(stamp='2026-10-05T09:29:00'),   # Finnhub also sends pre-market prints
    trade(stamp='2026-10-05T16:05:00'),   # and after-hours ones
    trade(symbol='MSFT'),                  # another symbol than the one asked for
    trade(price=0), trade(price='x'), trade(price=float('nan')),
    trade(stamp='2026-10-05T10:05:00'),   # ahead of our clock by more than the skew
])
def test_prints_outside_the_regular_session_or_malformed_are_dropped(bad):
    assert fs.trade_quote(bad, 'AAPL', 'c1', now=ny('2026-10-05T10:00:01')) is None


def test_a_print_far_from_the_last_price_is_dropped():
    now = ny('2026-10-05T10:00:01')
    assert fs.trade_quote(trade(price=320), 'AAPL', 'c1', Decimal('231'), now) is None
    assert fs.trade_quote(trade(price=240), 'AAPL', 'c1', Decimal('231'), now) is not None


def test_day_fields_come_from_a_same_day_snapshot():
    q = fs.trade_quote(trade(price=232), 'AAPL', 'c1', now=ny('2026-10-05T10:00:01'))
    # Finnhub REST: price 230 is +2 on the day, so the previous close was 228.
    snapshot = {'native_price': '230', 'change': '2', 'high': '231', 'low': '229', 'volume': 5,
                'timestamp': ny('2026-10-05T09:59:30')}
    out = fs.day_fields(q, snapshot, high=Decimal('233'))
    assert out['change'] == Decimal('4') and out['change_pct'] == Decimal('1.7544')
    assert (out['high'], out['low'], out['volume']) == (Decimal('233'), Decimal('229'), 5)
    # Yesterday's snapshot says nothing about today's change.
    old = snapshot | {'timestamp': ny('2026-10-02T15:59:00')}
    assert fs.day_fields(q, old)['change'] is None


# Slots ----------------------------------------------------------------------

def stream(limit=3):
    s = fs.FinnhubStream('k', FakeCache())
    s.effective_limit = s.limit = limit
    return s


def test_viewed_symbols_come_first_and_held_slots_stay():
    s, ws = stream(), FakeWS()
    asyncio.run(s.reconcile(ws, ['NVDA'], ['AAPL', 'MSFT', 'TSLA', 'AMD']))
    assert s.active == ['NVDA', 'AAPL', 'MSFT'] and s.queued == ['TSLA', 'AMD']
    # Someone opens AMD: it takes a slot from the worker's set, viewed NVDA is gone.
    asyncio.run(s.reconcile(ws, ['AMD'], ['TSLA', 'MSFT', 'AAPL']))
    assert sorted(s.active) == ['AAPL', 'AMD', 'MSFT']
    assert [(m['type'], m['symbol']) for m in ws.sent[3:]] == [('unsubscribe', 'NVDA'), ('subscribe', 'AMD')]
    # Nothing changes: nothing is sent.
    asyncio.run(s.reconcile(ws, ['AMD'], ['TSLA', 'MSFT', 'AAPL']))
    assert len(ws.sent) == 5


def test_a_too_many_symbols_error_lowers_the_cap():
    s, ws = stream(), FakeWS()
    asyncio.run(s.reconcile(ws, [], ['AAPL', 'MSFT', 'TSLA']))
    asyncio.run(s.handle(ws, json.dumps({'type': 'error', 'msg': 'Subscribing to too many symbols'})))
    assert s.active == ['AAPL', 'MSFT'] and s.effective_limit == 2 and ws.sent[-1] == {'type': 'unsubscribe', 'symbol': 'TSLA'}
    asyncio.run(s.reconcile(ws, [], ['AAPL', 'MSFT', 'TSLA']))
    assert s.active == ['AAPL', 'MSFT']  # not asked again on this connection
    s.reset()
    assert s.effective_limit == 3 and not s.active


def test_trades_reach_redis_with_day_fields(monkeypatch):
    now = ny('2026-10-05T10:00:01')
    monkeypatch.setattr(fs.time, 'time', lambda: now)
    snapshot = {'native_price': '230', 'change': '2', 'high': '231', 'low': '229', 'timestamp': ny('2026-10-05T09:59:30')}
    cache = FakeCache({'market:price:AAPL': snapshot})
    s = fs.FinnhubStream('k', cache)
    s.conn, s.active = 'c1', ['AAPL']
    message = {'type': 'trade', 'data': [trade(price=232, stamp='2026-10-05T09:59:59'), trade(price=233),
                                          trade(price=231, stamp='2026-10-05T09:59:58'),  # arrives late: older
                                          trade('MSFT', 500)]}                               # not subscribed
    asyncio.run(s.handle(FakeWS(), json.dumps(message)))
    s.flush()
    stored = cache.values['market:trade:AAPL']
    assert stored['native_price'] == Decimal('233') and stored['change'] == Decimal('5') and stored['high'] == Decimal('233')
    assert cache.published == [stored] and 'market:trade:MSFT' not in cache.values
    s.flush()
    assert len(cache.published) == 1  # nothing new, nothing written


# Readers ----------------------------------------------------------------------

def finnhub_quote(now, conn='f1'):
    return fs.trade_quote(trade(stamp=datetime.fromtimestamp(now - 2, NEW_YORK).replace(tzinfo=None).isoformat()),
                          'AAPL', conn, now=now)


def test_multi_market_judges_a_finnhub_price_by_the_finnhub_stream(monkeypatch):
    from app.multi_market import MultiMarket
    now = ny('2026-10-05T10:00:00')
    monkeypatch.setattr('app.quote_policy.time.time', lambda: now)
    m = MultiMarket()
    try:
        monkeypatch.setattr(m.providers['US'], 'session', lambda: 'regular')
        live = {'connected': True, 'conn': 'f1', 'heartbeat': now, 'subscribed': ['AAPL']}
        monkeypatch.setattr(m, 'stream_status', lambda: None)  # the KIS stream is down
        monkeypatch.setattr(m, 'finnhub_stream_status', lambda: live)
        q = m.assess('AAPL', finnhub_quote(now))
        assert (q['price_mode'], q['realtime'], q['session_tradeable']) == ('trade_stream', True, True)
        # After a reconnect the old connection's price is only a recent REST-like price.
        q = m.assess('AAPL', finnhub_quote(now, conn='f0'))
        assert (q['price_mode'], q['realtime'], q['session_tradeable']) == ('cached', False, True)
    finally:
        m.close()


def test_a_finnhub_price_is_never_tradeable_after_the_regular_session():
    now = ny('2026-10-05T16:10:00')
    q = fs.trade_quote(trade(stamp='2026-10-05T15:59:59'), 'AAPL', 'f1', now=now)
    q = assess(q, 'after_hours', {'connected': True, 'conn': 'f1', 'heartbeat': now, 'subscribed': ['AAPL']}, now=now)
    assert not q['session_tradeable']


def test_market_overview_is_realtime_on_the_finnhub_stream(monkeypatch):
    from app import us_session, quote_policy, redis_cache as rc
    from app.providers import USProvider
    from test_us_sessions import Adapter
    frozen = datetime.fromisoformat('2026-10-05T11:00').replace(tzinfo=NEW_YORK)
    real = us_session.clock_session
    monkeypatch.setattr(us_session, 'clock_session', lambda when=None: real(when or frozen))
    monkeypatch.setattr(quote_policy.time, 'time', lambda: frozen.timestamp())
    monkeypatch.setattr(rc.redis_cache, 'stream_status', lambda: None)
    monkeypatch.setattr(rc.redis_cache, 'finnhub_stream_status',
                        lambda: {'connected': True, 'conn': 'f1', 'heartbeat': frozen.timestamp()})
    status = USProvider(Adapter({'session': 'regular', 'holiday': None}), None).market_status()
    assert (status['price_mode'], status['stream_connected'], status['source']) == ('trade_stream', True, 'Finnhub')


# The market-worker ----------------------------------------------------------

def test_a_streamed_symbol_is_refreshed_over_rest_every_five_minutes(monkeypatch, tmp_path):
    """Finnhub REST allows 50 calls a minute for all symbols; at one call per symbol every
    15 s, 68 requested US symbols needed about 270."""
    clock, collected, ticks = [0.0], [], iter([1000.0, 1020.0, 1040.0, 1000.0 + mw.STREAMED_REFRESH + 1])
    def collect(market, symbol, ttl):
        collected.append((symbol, clock[0])); return {'timestamp': 0}, 'stored'
    def next_refresh(timeout=1):
        try: clock[0] = next(ticks)
        except StopIteration: raise Stop
    class Stop(Exception): pass
    monkeypatch.setenv('MARKET_WORKER_MODE', '')
    monkeypatch.setattr(mw.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(mw, 'HEARTBEAT', tmp_path / 'heartbeat')
    state = SimpleNamespace(session=lambda: 'regular')
    monkeypatch.setattr(mw, 'MultiMarket', lambda: SimpleNamespace(close=lambda: None, providers={'US': state, 'KR': state}))
    monkeypatch.setattr(mw, 'refresh_master', lambda: 1)
    monkeypatch.setattr(mw, 'refresh_us_master', lambda: 1)
    monkeypatch.setattr(mw, 'collect_quote', collect)
    monkeypatch.setattr(mw, 'finnhub_streamed', lambda: {'AAPL'})
    monkeypatch.setattr(mw.redis_cache, 'next_refresh', next_refresh)
    monkeypatch.setattr(mw.redis_cache, 'pop_refresh', lambda: None, raising=False)
    monkeypatch.setattr(mw.redis_cache, 'requested_symbols', lambda: ['AAPL', 'MSFT'])
    with pytest.raises(Stop):
        mw.main()
    assert [t for s, t in collected if s == 'AAPL'] == [1000.0, 1000.0 + mw.STREAMED_REFRESH + 1]
    assert [t for s, t in collected if s == 'MSFT'] == [1000.0, 1020.0, 1040.0, 1000.0 + mw.STREAMED_REFRESH + 1]


def test_a_newer_finnhub_trade_keeps_the_rest_day_change(monkeypatch):
    now = ny('2026-10-05T10:00:01')
    rest = {'symbol': 'AAPL', 'price': Decimal('230'), 'native_price': Decimal('230'), 'currency': 'USD', 'fx_rate': 1,
            'timestamp': int(ny('2026-10-05T09:59:30')), 'change': Decimal('2'), 'high': Decimal('231'), 'low': Decimal('229'),
            'stale': False, 'origin': 'rest', 'valid_sessions': ['regular']}
    streamed = fs.trade_quote(trade(price=232), 'AAPL', 'f1', now=now)
    stored = []
    monkeypatch.setattr(mw.redis_cache, 'get_json', lambda key: streamed if key == 'market:trade:AAPL' else None)
    monkeypatch.setattr(mw.redis_cache, 'store_quote', lambda symbol, q, ttl: stored.append(q) or True)
    monkeypatch.setattr(mw.redis_cache, 'set_json', lambda *a, **k: True)
    market = SimpleNamespace(quote_direct=lambda symbol: dict(rest))
    quote, state = mw.collect_quote(market, 'AAPL', 120)
    assert state == 'stored' and quote['native_price'] == Decimal('232') and quote['change'] == Decimal('4')
    assert quote['origin'] == 'stream' and stored == [quote]
