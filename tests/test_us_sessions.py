"""US sessions, session-specific price sources and order gating."""
import asyncio
import json
import shutil
import subprocess
import time
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app import main
from app.market import MarketError
from app.quote_policy import assess, rejection
from app.trading import validate_quote
from app.us_quotes import NoSessionData, USQuotes, PRIMARY_SESSIONS
from app.us_session import NEW_YORK, clock_session, resolve_session
from app import trade_stream as ts
from test_service import database, client, register  # noqa: F401  (shared fixtures)


def at(text, zone=NEW_YORK):
    return datetime.fromisoformat(text).replace(tzinfo=zone)


def ny(text):
    return at(text).timestamp()


STREAM = {'connected': True, 'conn': 'c1', 'heartbeat': 0, 'subscribed': ['AAPL']}


def live():
    return STREAM | {'heartbeat': time.time()}


@pytest.fixture
def overnight(monkeypatch):
    """Trades stamped 'now' count as overnight prints, whatever the real clock says."""
    from app import quote_policy
    monkeypatch.setattr(quote_policy, 'clock_session', lambda when=None: 'overnight')


def quote(stamp=None, origin='stream', sessions=('overnight',), conn='c1', **extra):
    return {'symbol': 'AAPL', 'price': Decimal('231.15'), 'native_price': Decimal('231.15'), 'currency': 'USD',
            'timestamp': int(stamp or time.time()), 'stale': False, 'origin': origin, 'source': 'KIS',
            'valid_sessions': list(sessions), 'stream_conn': conn} | extra


def order_fails(q, text):
    with pytest.raises(HTTPException) as exc:
        validate_quote('AAPL', q)
    assert exc.value.status_code == 409 and text in exc.value.detail


# 42-14 DST: the New York clock decides, never fixed Korean hours ----------

@pytest.mark.parametrize('utc,expected', [
    ('2026-03-06T13:35', 'pre_market'),   # EST: 08:35 ET
    ('2026-03-09T13:35', 'regular'),      # EDT after the switch: 09:35 ET, same UTC/KST time
    ('2026-11-02T14:35', 'regular'),      # EST again: 09:35 ET
    ('2026-10-30T13:35', 'regular'),      # still EDT: 09:35 ET
    ('2026-01-15T00:30', 'after_hours'),  # KST 09:30 in winter = 19:30 ET
    ('2026-09-15T00:30', 'overnight'),    # KST 09:30 in summer = 20:30 ET
    ('2026-01-15T01:00', 'overnight'),    # KST 10:00 in winter = 20:00 ET
])
def test_session_follows_new_york_clock_across_dst(utc, expected):
    assert clock_session(at(utc, timezone.utc)) == expected


@pytest.mark.parametrize('local,expected', [
    ('2026-09-24T23:10', 'overnight'),    # Thursday night
    ('2026-09-25T03:59', 'overnight'),    # Friday before the pre-market
    ('2026-09-25T04:00', 'pre_market'),
    ('2026-09-25T09:30', 'regular'),
    ('2026-09-25T16:00', 'after_hours'),
    ('2026-09-25T20:30', 'closed'),       # Friday night: no overnight session
    ('2026-09-26T03:00', 'closed'),       # Saturday
    ('2026-09-27T19:59', 'closed'),       # Sunday before the week opens
    ('2026-09-27T20:00', 'overnight'),    # Sunday night opens Monday's overnight
])
def test_session_week_boundaries(local, expected):
    assert clock_session(at(local)) == expected


def test_finnhub_status_adds_holidays_and_early_closes():
    thanksgiving = at('2026-11-26T11:00')
    assert resolve_session({'holiday': 'Thanksgiving', 'session': None}, thanksgiving) == 'closed'
    assert resolve_session({'holiday': 'Thanksgiving'}, at('2026-11-26T02:00')) == 'closed'
    early = at('2026-11-27T14:00')
    assert resolve_session({'holiday': None, 'session': 'post-market'}, early) == 'after_hours'
    assert resolve_session(None, early) == 'regular'
    # Finnhub has no overnight session; the clock supplies it.
    assert resolve_session({'holiday': None, 'session': None}, at('2026-09-24T23:10')) == 'overnight'


# Read-time assessment ------------------------------------------------------

def test_overnight_stream_trade_fills(overnight):                          # 42-1
    q = assess(quote(), 'overnight', live())
    assert (q['price_mode'], q['realtime'], q['session_tradeable'], q['stale']) == ('overnight_stream', True, True, False)
    assert validate_quote('AAPL', q)[1] == Decimal('231.15')


def test_overnight_without_day_market_source_is_refused():                 # 42-2
    q = assess(quote(ny('2026-09-24T15:59'), origin='rest', sessions=['regular'], source='Finnhub'),
               'overnight', None, now=ny('2026-09-24T23:10'))
    assert q['price_mode'] == 'unavailable' and not q['session_tradeable']
    assert rejection(q) == '현재 미국 데이마켓 체결 시세를 확인할 수 없어 주문할 수 없습니다.'


def test_overnight_never_uses_regular_close():                             # 42-3
    now = ny('2026-09-24T21:00')
    # A fresh-looking primary-exchange print still belongs to another session.
    closing = quote(ny('2026-09-24T15:59:59'), origin='rest', sessions=PRIMARY_SESSIONS)
    q = assess(closing, 'overnight', STREAM | {'heartbeat': now}, now=now)
    assert q['trade_session'] == 'regular' and not q['session_tradeable']
    with pytest.raises(HTTPException) as exc:
        validate_quote('AAPL', q)
    assert '데이마켓' in exc.value.detail


class FakeKIS:
    configured = True

    def __init__(self, bars=None, fail=False):
        self.bars, self.fail = bars or {}, fail

    def get(self, path, tr_id, params, ttl=15, tr_cont=''):
        if self.fail:
            raise MarketError('KIS down')
        if path.endswith('/price'):
            return {'output': {'last': '230.00', 'base': '230.00'}}
        return {'output1': {'stim': '200000', 'etim': '040000'}, 'output2': self.bars.get(params['EXCD'], [])}


class FakeFinnhub:
    def quote(self, symbol):
        return {'symbol': symbol, 'price': Decimal('230'), 'timestamp': int(ny('2026-09-24T16:00')), 'stale': False}


def test_symbol_without_day_market_prints_is_not_tradeable(monkeypatch):   # 42-4 / 47
    monkeypatch.setattr(USQuotes, 'exchange', lambda self, s: 'NAS')
    source = USQuotes(FakeFinnhub(), FakeKIS(bars={'BAQ': []}))
    q = source.quote('AAPL', 'overnight')
    assert q['source'] == 'Finnhub'  # display fallback only
    q = assess(q, 'overnight', None, now=ny('2026-09-24T23:10'))
    assert q['session_tradeable'] is False and q['price_mode'] == 'unavailable'
    with pytest.raises(NoSessionData):
        source.kis_bars('AAPL', True)


def test_day_market_rest_bar_fills(monkeypatch):
    monkeypatch.setattr(USQuotes, 'exchange', lambda self, s: 'NAS')
    now = time.time()
    local = datetime.fromtimestamp(now - 60, NEW_YORK)
    bar = {'xymd': local.strftime('%Y%m%d'), 'xhms': local.strftime('%H%M00'), 'last': '231.50'}
    q = USQuotes(FakeFinnhub(), FakeKIS(bars={'BAQ': [bar]})).quote('AAPL', 'overnight')
    assert q['exchange'] == 'BAQ' and q['valid_sessions'] == ['overnight'] and q['change'] == Decimal('1.5')
    session = clock_session(datetime.fromtimestamp(now - 60, timezone.utc))
    q = assess(q, session, None, now=now)
    assert q['session_tradeable'] == (session == 'overnight')


@pytest.mark.parametrize('session,stamp,mode', [
    ('pre_market', '2026-09-25T08:00', 'extended_stream'),                  # 42-5
    ('after_hours', '2026-09-25T17:00', 'extended_stream'),                 # 42-6
    ('regular', '2026-09-25T11:00', 'trade_stream'),                        # 42-7
])
def test_primary_stream_fills_in_its_sessions(session, stamp, mode):
    stream = STREAM | {'heartbeat': ny(stamp) + 1}
    q = assess(quote(ny(stamp), sessions=PRIMARY_SESSIONS), session, stream, now=ny(stamp) + 2)
    assert q['price_mode'] == mode and q['realtime'] and q['session_tradeable'] and q['source'] == 'KIS'


def test_regular_rest_fallback_when_stream_is_down():                      # 42-8
    now = ny('2026-09-25T11:00')
    q = assess(quote(now - 30, origin='rest', sessions=['regular'], source='Finnhub'), 'regular',
               STREAM | {'connected': False}, now=now)
    assert (q['price_mode'], q['realtime'], q['session_tradeable']) == ('rest', False, True)


def test_extended_hours_reject_unverified_rest():                          # 42-9
    now = ny('2026-09-25T17:00')
    # Finnhub is verified for the regular session only, even with an after-hours time.
    q = assess(quote(now - 30, origin='rest', sessions=['regular'], source='Finnhub'), 'after_hours', None, now=now)
    assert not q['session_tradeable']
    assert rejection(q) == '현재 미국 애프터장 체결 시세를 확인할 수 없어 주문할 수 없습니다.'


def test_stale_stream_is_not_realtime(monkeypatch):                         # 42-10
    monkeypatch.setenv('US_STREAM_MAX_AGE', '10')
    now = ny('2026-09-24T23:10')
    q = assess(quote(now - 5), 'overnight', STREAM | {'heartbeat': now - 30}, now=now)
    assert not q['realtime'] and q['price_mode'] == 'cached' and q['session_tradeable']
    q = assess(quote(now - 3600), 'overnight', STREAM | {'heartbeat': now - 30}, now=now)
    assert q['stale'] and not q['session_tradeable']
    # A quiet symbol on a healthy stream keeps its last trade as the live price.
    q = assess(quote(now - 3600), 'overnight', STREAM | {'heartbeat': now}, now=now)
    assert q['realtime'] and q['session_tradeable']


def test_stream_reconnect_restores_priority():                              # 42-11
    now = ny('2026-09-24T23:10')
    reconnected = STREAM | {'conn': 'c2', 'heartbeat': now}
    assert assess(quote(now - 5, conn='c1'), 'overnight', reconnected, now=now)['price_mode'] == 'cached'
    assert assess(quote(now - 1, conn='c2'), 'overnight', reconnected, now=now)['price_mode'] == 'overnight_stream'


def test_legacy_quotes_are_untouched():                                     # 43
    legacy = {'symbol': 'AAPL', 'price': Decimal(1), 'timestamp': int(time.time()), 'stale': False}
    assert assess(legacy, 'overnight', None) is legacy and rejection(legacy) is None


# Market overview ------------------------------------------------------------

class Adapter:
    def __init__(self, status): self.status = status
    def get(self, path, params, ttl): return self.status


@pytest.mark.parametrize('now,kis,stream,expected', [
    ('2026-09-24T23:10', True, True, {'session': 'overnight', 'label': '데이마켓', 'price_mode': 'overnight_stream',
                                       'day_market_status': 'realtime', 'extended_prices': False,
                                       'extended_price_status': 'not_current_session', 'day_market_supported': True}),
    ('2026-09-24T23:10', False, False, {'session': 'overnight', 'price_mode': 'unavailable', 'day_market_supported': False,
                                         'day_market_status': 'unsupported'}),
    ('2026-09-25T17:00', True, True, {'session': 'after_hours', 'label': '애프터장', 'price_mode': 'extended_stream',
                                       'extended_prices': True, 'extended_price_status': 'realtime',
                                       'day_market_status': 'available'}),
    ('2026-09-25T17:00', True, False, {'price_mode': 'extended_rest', 'extended_prices': True, 'extended_price_status': 'rest'}),
    ('2026-09-25T17:00', False, False, {'extended_prices': False, 'extended_price_status': 'unsupported', 'price_mode': 'unavailable'}),
    ('2026-09-25T11:00', False, False, {'session': 'regular', 'price_mode': 'rest', 'source': 'Finnhub'}),
])
def test_market_overview_reports_real_state(monkeypatch, now, kis, stream, expected):  # 42-12
    from app import providers, us_session, quote_policy, redis_cache as rc
    from app.providers import USProvider
    frozen = at(now)
    real = us_session.clock_session
    monkeypatch.setattr(us_session, 'clock_session', lambda when=None: real(when or frozen))
    monkeypatch.setattr(quote_policy.time, 'time', lambda: frozen.timestamp())
    finnhub = {'session': {'2026-09-25T17:00': 'post-market', '2026-09-25T11:00': 'regular'}.get(now), 'holiday': None}
    monkeypatch.setattr(rc.redis_cache, 'stream_status',
                        lambda: {'connected': True, 'conn': 'c', 'heartbeat': frozen.timestamp()} if stream else None)
    status = USProvider(Adapter(finnhub), FakeKIS() if kis else None).market_status()
    assert {k: status[k] for k in expected} == expected


# Orders through the API ------------------------------------------------------

class SessionMarket:
    key = 'test'
    client = type('Client', (), {'close': lambda self: None})()

    def __init__(self, label, q): self.q = q; self.providers = {'US': self, 'KR': self}; self.label = label
    def status(self): return {'us': True, 'kr': True}
    def market_status(self): return {'label': self.label, 'timezone': 'X', 'verified': True}
    def quote(self, symbol): return dict(self.q)


def post_order(client, headers):
    return client.post('/api/orders', headers=headers, json={'symbol': 'AAPL', 'side': 'buy', 'quantity': 1,
                                                             'request_id': str(uuid4())})


def test_day_market_order_fills_on_verified_print(client, monkeypatch, overnight):
    headers = {'x-csrf-token': register(client)}
    monkeypatch.setattr(main, 'market', SessionMarket('데이마켓', assess(quote(), 'overnight', live())))
    r = post_order(client, headers)
    assert r.status_code == 200 and r.json()['quantity'] == 1


def test_day_market_order_refused_on_regular_close(client, monkeypatch, overnight):
    headers = {'x-csrf-token': register(client)}
    # Recent and otherwise valid, but Finnhub is only verified for the regular session.
    close = quote(time.time() - 60, origin='rest', sessions=['regular'], source='Finnhub')
    monkeypatch.setattr(main, 'market', SessionMarket('데이마켓', assess(close, 'overnight', None)))
    r = post_order(client, headers)
    assert r.status_code == 409 and r.json()['detail'] == '현재 미국 데이마켓 체결 시세를 확인할 수 없어 주문할 수 없습니다.'
    preview = client.get('/api/order-preview?symbol=AAPL').json()
    assert preview['indicative_only'] and preview['session_tradeable'] is False


# Trade stream worker ----------------------------------------------------------

RAW = ('0|HDFSCNT0|001|RBAQNVDA^NVDA^4^20260925^20260924^231313^20260925^121313^223.9800^224.5000^223.5000^'
       '223.9800^5^0.6000^-0.27^223.9700^223.9900^10^20^5^52000^11650000^100^200^98.5^2')


def test_parse_and_validate_captured_frame():
    fields = ts.parse_trades(RAW)[0]
    q = ts.trade_quote(fields, 'NVDA', 'RBAQNVDA', 'c1', now=ny('2026-09-24T23:13:14'))
    assert q['price'] == Decimal('223.98') and q['change'] == Decimal('-0.6') and q['valid_sessions'] == ['overnight']
    assert q['timestamp'] == int(ny('2026-09-24T23:13:13'))
    # The same print under a primary-exchange key is not a regular/extended trade.
    assert ts.trade_quote(fields | {'RSYM': 'DNASNVDA'}, 'NVDA', 'DNASNVDA', 'c1', now=ny('2026-09-24T23:13:14')) is None
    assert ts.trade_quote(fields, 'AAPL', 'RBAQNVDA', 'c1') is None
    assert ts.parse_trades('0|HDFSCNT0|001|short^frame') == []


class FakeWS:
    def __init__(self): self.sent = []
    async def send(self, message): self.sent.append(json.loads(message) if message.startswith('{') else message)


class FakeCache:
    client = None
    def __init__(self): self.values, self.published = {}, []
    def set_json(self, key, value, ttl): self.values[key] = value; return True
    def store_quote(self, symbol, q, ttl): self.published.append(q); return True
    def stream_interest(self): return []


def ack(key, code='OPSP0000', ok=True, text='SUBSCRIBE SUCCESS'):
    return json.dumps({'header': {'tr_id': 'HDFSCNT0', 'tr_key': key}, 'body': {'rt_cd': '0' if ok else '1', 'msg_cd': code, 'msg1': text}})


def test_stream_subscriptions_respect_limit_and_session(monkeypatch):
    stream = ts.TradeStream(FakeKIS(), FakeCache())
    monkeypatch.setattr(stream, 'exchange', lambda s: 'AMS' if s == 'SPY' else 'NAS')
    ws = FakeWS()
    run = asyncio.run
    run(stream.reconcile(ws, 'k', 'overnight', ['AAPL', 'SPY', 'NVDA', 'MSFT', 'KR:005930']))
    keys = [m['body']['input']['tr_key'] for m in ws.sent]
    assert keys == ['RBAQAAPL', 'RBAASPY', 'RBAQNVDA'] and stream.queued == ['MSFT', 'KR:005930']
    run(stream.reconcile(ws, 'k', 'overnight', ['AAPL', 'SPY', 'NVDA', 'MSFT']))
    assert len(ws.sent) == 3  # no duplicate subscribe while acks are pending
    for key in ('RBAQAAPL', 'RBAASPY'):
        run(stream.handle(ws, ack(key)))
    run(stream.handle(ws, ack('RBAQNVDA', 'OPSP0008', False, 'MAX SUBSCRIBE OVER')))
    assert stream.effective_limit == 2 and sorted(stream.active) == ['HDFSCNT0|RBAASPY', 'HDFSCNT0|RBAQAAPL']
    # Pre-market needs the primary-exchange key: resubscribe, never reuse the overnight one.
    stream.since = {s: 0 for s in stream.since}
    run(stream.reconcile(ws, 'k', 'pre_market', ['AAPL', 'SPY']))
    assert [(m['header']['tr_type'], m['body']['input']['tr_key']) for m in ws.sent[3:]] == [('2', 'RBAQAAPL'), ('2', 'RBAASPY')]
    with pytest.raises(ts.StreamRejected):
        run(stream.handle(ws, json.dumps({'header': {}, 'body': {'msg1': 'ALREADY IN USE appkey'}})))


def test_stream_trade_reaches_redis_and_reconnect_resets():
    cache = FakeCache()
    stream = ts.TradeStream(FakeKIS(), cache)
    stream.conn = 'c1'
    stream.active = {'HDFSCNT0|RBAQNVDA': 'NVDA'}
    real = time.time
    try:
        ts.time.time = lambda: ny('2026-09-24T23:13:14')
        asyncio.run(stream.handle(FakeWS(), RAW))
    finally:
        ts.time.time = real
    assert cache.values['market:trade:NVDA']['stream_conn'] == 'c1' and cache.published[-1]['price'] == Decimal('223.98')
    stream.reset()
    assert stream.conn is None and not stream.active and stream.effective_limit == stream.limit


# 42-13 UI text follows the server state -------------------------------------

@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_ui_only_says_unsupported_when_unsupported():
    source = open('app/static/app.js').read()
    start = source.index('window.marketPriceText=')
    body = source[start:source.index('};', start) + 2]
    script = 'const window={};' + body + '''
const cases=[
 [{market:'US',session:'overnight',open:true,tradable:true,price_mode:'overnight_stream'},'실시간'],
 [{market:'US',session:'overnight',open:true,tradable:true,price_mode:'overnight_rest'},'보조 시세'],
 [{market:'US',session:'overnight',open:true,tradable:false,price_mode:'unavailable'},'시장 열림 · 주문 시세 확인 불가'],
 [{market:'KR',session:'after_hours',open:true,tradable:true,price_mode:'extended_stream'},'실시간'],
 [{market:'KR',session:'pre_market',open:true,tradable:true,price_mode:'extended_rest'},'보조 시세'],
 [{market:'US',session:'regular',open:true,tradable:true,price_mode:'rest'},'보조 시세'],
 [{market:'US',session:'after_hours',price_mode:'unavailable'},'체결 시세 미지원'],
 [{market:'US',session:'closed'},''],[{market:'KR',label:'정규장'},'']];
for(const [x,want] of cases){const got=window.marketPriceText(x);if(got!==want)throw new Error(JSON.stringify(x)+' '+got);}'''
    subprocess.run(['node', '-e', script], check=True)


def test_pre_upgrade_snapshot_is_regular_only(monkeypatch):
    from app.multi_market import MultiMarket
    m = MultiMarket()
    try:
        monkeypatch.setattr(m.providers['US'], 'session', lambda: 'overnight')
        legacy = {'symbol': 'AAPL', 'price': Decimal(1), 'native_price': Decimal(1), 'currency': 'USD',
                  'timestamp': int(time.time()), 'stale': False}
        q = m.assess('AAPL', legacy)
        assert q['session_tradeable'] is False and '데이마켓' in rejection(q)
    finally:
        m.close()
