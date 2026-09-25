"""Characterization tests for market data and streaming: quote age, SSE admission
and version gate, trade-stream frames and control messages, search merging, FX."""
import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from fastapi import HTTPException
from redis.asyncio import Redis
from starlette.requests import Request

import test_market_stream
from app import quote_data, quote_policy
from app import trade_stream as ts
from app.kr_quotes import unified_quote
from app.kr_session import SEOUL
from app.market import Finnhub, MarketError
from app.market_stream import QuoteHub
from app.us_session import NEW_YORK

cache = test_market_stream.cache  # dedicated Redis; skips without SSE_TEST_REDIS_URL
quote = test_market_stream.quote


# Quote max age -----------------------------------------------------------------

@pytest.mark.parametrize('env,kr,us', [
    ({}, 900, 1800),
    ({'MAX_QUOTE_AGE': '60'}, 60, 1800),
    ({'US_MAX_QUOTE_AGE': '120'}, 900, 120),
    ({'MAX_QUOTE_AGE': '61', 'US_MAX_QUOTE_AGE': '121'}, 61, 121),
])
def test_quote_max_age_per_market_reads_env_at_call_time(monkeypatch, env, kr, us):
    for name in ('MAX_QUOTE_AGE', 'US_MAX_QUOTE_AGE'):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert (quote_policy.max_age('KR'), quote_policy.max_age('US'), quote_policy.max_age('XX')) == (kr, us, us)
    assert (quote_data.max_age('KR:005930'), quote_data.max_age('AAPL')) == (kr, us)


def test_stale_flags_follow_the_market_age(monkeypatch):
    monkeypatch.setenv('MAX_QUOTE_AGE', '60')
    monkeypatch.setenv('US_MAX_QUOTE_AGE', '120')
    now = time.time()
    assert quote_data.normalize_quote('KR:005930', quote(symbol='KR:005930', currency='KRW', timestamp=int(now) - 90), now)['stale']
    assert not quote_data.normalize_quote('AAPL', quote(timestamp=int(now) - 90), now)['stale']
    finnhub = Finnhub()
    try:
        stamp = [int(now) - 90]
        finnhub.get = lambda path, params, ttl: {'c': 100, 't': stamp[0], 'd': 1, 'dp': 1, 'h': 101, 'l': 99}
        assert not finnhub.quote('AAPL')['stale']
        monkeypatch.setenv('US_MAX_QUOTE_AGE', '60')
        assert finnhub.quote('AAPL')['stale']
        monkeypatch.delenv('US_MAX_QUOTE_AGE')
        stamp[0] = int(now) - 1700
        assert not finnhub.quote('AAPL')['stale']
        stamp[0] = int(now) - 1900
        assert finnhub.quote('AAPL')['stale']
    finally:
        finnhub.client.close()


class TradedKIS:
    """One traded minute bar `age` seconds ago; not NXT-listed, not an ETP."""
    configured = True

    def __init__(self, age): self.age = age

    def get(self, path, tr_id, params, ttl=15, tr_cont=''):
        if path.endswith('inquire-price'):
            return {'output': {'stck_sdpr': '0', 'rprs_mrkt_kor_name': 'KOSPI', 'bstp_kor_isnm': '전기·전자'}}
        stamp = datetime.now(SEOUL) - timedelta(seconds=self.age)
        return {'output1': {}, 'output2': [{'stck_bsop_date': stamp.strftime('%Y%m%d'), 'stck_cntg_hour': stamp.strftime('%H%M%S'),
                                            'stck_prpr': '70000', 'cntg_vol': '3'}]}


def test_korean_rest_quote_stale_flag_uses_the_korean_age(monkeypatch):
    monkeypatch.setenv('MAX_QUOTE_AGE', '60')
    assert unified_quote(TradedKIS(90), 'KR:005930')['stale']
    monkeypatch.setenv('MAX_QUOTE_AGE', '120')
    assert not unified_quote(TradedKIS(90), 'KR:005930')['stale']
    monkeypatch.delenv('MAX_QUOTE_AGE')
    assert not unified_quote(TradedKIS(800), 'KR:005930')['stale']
    assert unified_quote(TradedKIS(1000), 'KR:005930')['stale']


# SSE admission -----------------------------------------------------------------

def sse_request(origin=None, client=('test', 1)):
    headers = [(b'host', b'test')] + ([(b'origin', origin.encode())] if origin else [])
    return Request({'type': 'http', 'headers': headers, 'client': client, 'method': 'GET', 'path': '/api/market-stream/AAPL'})


def refusal(hub, symbol='AAPL', origin=None, uid=1):
    with pytest.raises(HTTPException) as failure:
        asyncio.run(hub.response(sse_request(origin), symbol, uid, lambda _: None))
    return failure.value.status_code, failure.value.detail, failure.value.headers


def test_sse_request_checks_come_before_any_redis_call(monkeypatch):
    hub = QuoteHub('redis://unused')
    hub.redis = object()  # any Redis call would raise AttributeError, not HTTPException
    monkeypatch.delenv('QUOTE_SSE_ENABLED', raising=False)
    assert refusal(hub, 'bad!', 'https://other') == (503, '시세 스트림이 비활성 상태입니다.', None)
    monkeypatch.setenv('QUOTE_SSE_ENABLED', 'true')
    monkeypatch.setenv('MARKET_CACHE_MODE', 'worker')
    monkeypatch.setenv('REDIS_URL', 'redis://unused')
    hub.redis = None
    assert refusal(hub, 'bad!', 'https://other')[:2] == (503, '시세 스트림이 비활성 상태입니다.')
    hub.redis = object()
    assert refusal(hub, 'bad!', 'https://other') == (422, '잘못된 종목 코드입니다.', None)
    assert refusal(hub, 'AAPL', 'https://other') == (403, '허용되지 않은 출처입니다.', None)
    assert refusal(hub, 'AAPL', 'https://test:8000') == (403, '허용되지 않은 출처입니다.', None)


def test_sse_admission_limits_and_readiness(cache, monkeypatch):
    monkeypatch.setenv('QUOTE_SSE_ENABLED', 'true')
    monkeypatch.setenv('MARKET_CACHE_MODE', 'worker')

    async def scenario():
        hub = QuoteHub(cache.url)
        # No subscriber task: the hub never becomes ready.
        hub.redis = Redis.from_url(cache.url, decode_responses=True)
        try:
            with pytest.raises(HTTPException) as failure:
                await hub.response(sse_request(), 'AAPL', 7, lambda _: None)
            assert (failure.value.status_code, failure.value.detail) == (503, '시세 연결을 준비 중입니다.')
            # The attempt counted, the lease taken for the wait was released again.
            assert cache.client.get('market:sse:attempt:user:7') == '1' and cache.client.zcard('market:sse:user:7') == 0
            assert not hub.listeners
            hub.ready.set()
            for n in range(5):
                assert await hub.lease(['market:sse:user:8', 'market:sse:ip:other'], str(n))
            with pytest.raises(HTTPException) as failure:
                await hub.response(sse_request(), 'AAPL', 8, lambda _: None)
            assert (failure.value.status_code, failure.value.detail, failure.value.headers) == (429, '시세 연결 수 제한입니다.', {'Retry-After': '60'})
            cache.client.set('market:sse:attempt:user:9', 30)
            with pytest.raises(HTTPException) as failure:
                await hub.response(sse_request(), 'AAPL', 9, lambda _: None)
            assert (failure.value.status_code, failure.value.detail, failure.value.headers) == (429, '연결 시도가 너무 많습니다.', {'Retry-After': '60'})
            assert cache.client.zcard('market:sse:user:9') == 0  # refused before leasing
            assert not hub.listeners
        finally:
            await hub.redis.aclose()
    asyncio.run(scenario())


def test_sse_lease_lifetime_follows_the_clamped_heartbeat(cache, monkeypatch):
    async def scenario():
        hub = QuoteHub(cache.url)
        hub.redis = Redis.from_url(cache.url, decode_responses=True)
        try:
            for value, ttl in ((None, 60), ('1', 20), ('5', 20), ('15', 60), ('30', 120), ('99', 120)):
                if value is None:
                    monkeypatch.delenv('QUOTE_SSE_HEARTBEAT', raising=False)
                else:
                    monkeypatch.setenv('QUOTE_SSE_HEARTBEAT', value)
                keys = [f'market:sse:user:h{value}', f'market:sse:ip:h{value}']
                assert await hub.lease(keys, 't')
                assert abs(cache.client.zscore(keys[0], 't') - time.time() - ttl) < 2
                assert ttl * 2 - 2 <= cache.client.ttl(keys[1]) <= ttl * 2
        finally:
            await hub.redis.aclose()
    asyncio.run(scenario())


# SSE version gate ----------------------------------------------------------------

def parse_event(text):
    fields = dict(line.split(': ', 1) for line in text.strip().split('\n'))
    data = json.loads(fields['data'])
    return fields['event'], data.get('version', data.get('state'))


def test_sse_version_gate_through_the_stream(cache, monkeypatch):
    monkeypatch.setenv('QUOTE_SSE_ENABLED', 'true')
    monkeypatch.setenv('MARKET_CACHE_MODE', 'worker')
    monkeypatch.setenv('QUOTE_SSE_HEARTBEAT', '30')  # no heartbeat inside the scenario

    async def scenario():
        hub = QuoteHub(cache.url)
        await hub.start()
        await asyncio.wait_for(hub.ready.wait(), 3)
        try:
            assert cache.store_quote('AAPL', quote(), 30)
            await asyncio.sleep(.2)  # its notification reaches the hub before anyone listens
            e = cache.get_json('market:price:AAPL')['_version'].split(':')[0]
            f = 'f' * 32 if e != 'f' * 32 else 'e' * 32

            async def stream(symbol):
                response = await hub.response(sse_request(), symbol, 1, lambda _: None)
                events = response.body_iterator
                queue, = hub.listeners[symbol]

                async def after(*items):
                    """Feed items one at a time, each once the stream took the last; return the next event."""
                    async def take():
                        return await anext(events)
                    pending = asyncio.create_task(take())
                    await asyncio.sleep(0)
                    for item in items:
                        hub.fanout(symbol, item)
                        while not queue.empty() and not pending.done():
                            await asyncio.sleep(.01)
                    return parse_event(await asyncio.wait_for(pending, 3))
                return events, after

            def q(name, epoch, n):
                return name, {'version': f'{epoch}:{n}', 'n': n}

            events, after = await stream('AAPL')
            assert parse_event(await anext(events)) == ('snapshot', f'{e}:1')
            assert await after(q('quote', e, 1), q('quote', e, 2)) == ('quote', f'{e}:2')        # a repeated quote is dropped
            assert await after(q('snapshot', e, 2)) == ('snapshot', f'{e}:2')                    # a repeated snapshot is re-sent
            assert await after(q('quote', e, 1), q('snapshot', e, 1), q('quote', e, 3)) == ('quote', f'{e}:3')  # older: dropped
            assert await after(q('quote', e, 100)) == ('quote', f'{e}:100')
            assert await after(q('quote', e, 99), q('quote', e, 101)) == ('quote', f'{e}:101')   # numeric, not text order
            assert await after(q('quote', f, 1)) == ('snapshot', f'{f}:1')                       # a new epoch is a snapshot
            assert await after(q('quote', e, 9)) == ('snapshot', f'{e}:9')
            assert await after(('status', {'state': 'waiting'})) == ('status', 'waiting')
            assert await after(q('quote', e, 9), q('quote', e, 10)) == ('quote', f'{e}:10')     # status keeps the gate
            assert await after(('status', {'state': 'restart'})) == ('status', 'restart')
            with pytest.raises(StopAsyncIteration):
                await anext(events)
            assert not hub.listeners

            events, after = await stream('MSFT')  # nothing stored: no previous version
            assert parse_event(await anext(events)) == ('status', 'waiting')
            assert await after(q('quote', e, 5)) == ('quote', f'{e}:5')
            assert await after(q('quote', e, 4), q('quote', e, 6)) == ('quote', f'{e}:6')
            await events.aclose()
            assert not hub.listeners
        finally:
            await hub.stop()
    asyncio.run(scenario())


# Trade stream ----------------------------------------------------------------------

class StreamKIS:
    configured = True
    key, secret = 'k', 's'

    def __init__(self): self.calls = []

    def get(self, path, tr_id, params, ttl=15, tr_cont=''):
        self.calls.append((path, tr_id, params, ttl))
        return {'output': [{'bass_dt': params['BASS_DT'], 'opnd_yn': 'Y'}]}


class StreamCache:
    """Only what TradeStream calls; records every TTL."""
    client = None

    def __init__(self):
        self.values, self.ttls, self.published, self.deleted = {}, {}, [], []

    def set_json(self, key, value, ttl):
        self.values[key], self.ttls[key] = value, ttl
        return True

    def store_quote(self, symbol, q, ttl):
        self.published.append((symbol, q, ttl))
        return True

    def get_or_load(self, key, ttl, load):
        self.ttls[key] = ttl
        return {'approval_key': 'approved'}

    def key(self, namespace, value): return namespace

    def delete(self, key): self.deleted.append(key)


class RecordingWS:
    def __init__(self): self.sent = []
    async def send(self, message): self.sent.append(message)


def control(key, rt_cd='0', code='OPSP0000', text='SUBSCRIBE SUCCESS'):
    return json.dumps({'header': {'tr_id': 'HDFSCNT0', 'tr_key': key}, 'body': {'rt_cd': rt_cd, 'msg_cd': code, 'msg1': text}})


def test_trade_stream_control_messages():
    stream = ts.TradeStream(StreamKIS(), StreamCache())
    ws, run = RecordingWS(), asyncio.run
    # PINGPONG is echoed verbatim before any error check.
    ping = json.dumps({'header': {'tr_id': 'PINGPONG', 'datetime': '20260925120000'}, 'body': {'msg1': 'ALREADY IN USE'}})
    run(stream.handle(ws, ping))
    assert ws.sent == [ping]
    # Not JSON, or an answer to nothing we sent: ignored.
    run(stream.handle(ws, 'not json'))
    run(stream.handle(ws, control('DNASAAPL')))
    assert stream.active == {} and stream.last_error is None
    stream.sent = {'HDFSCNT0|DNASAAPL': ('AAPL', 'subscribe', time.monotonic())}
    run(stream.handle(ws, control('DNASAAPL')))
    assert stream.active == {'HDFSCNT0|DNASAAPL': 'AAPL'} and 'AAPL' in stream.since and stream.sent == {}
    stream.latest['AAPL'] = {'native_price': 1}
    stream.sent = {'HDFSCNT0|DNASAAPL': ('AAPL', 'unsubscribe', time.monotonic())}
    run(stream.handle(ws, control('DNASAAPL', text='UNSUBSCRIBE SUCCESS')))
    assert stream.active == {} and stream.since == {} and stream.latest == {}
    stream.sent = {'HDFSCNT0|DNASMSFT': ('MSFT', 'subscribe', time.monotonic())}
    run(stream.handle(ws, control('DNASMSFT', '1', 'OPSP9999', 'FAILED')))
    assert stream.last_error == 'OPSP9999' and stream.active == {} and stream.sent == {}
    stream.sent = {'HDFSCNT0|DNASMSFT': ('MSFT', 'subscribe', time.monotonic())}
    run(stream.handle(ws, control('DNASMSFT', '1', None, 'FAILED')))
    assert stream.last_error == 'subscribe_failed'
    stream.active = {'HDFSCNT0|DNASAAPL': 'AAPL'}
    stream.sent = {'HDFSCNT0|DNASMSFT': ('MSFT', 'subscribe', time.monotonic())}
    run(stream.handle(ws, control('DNASMSFT', '1', 'OPSP0008', 'MAX SUBSCRIBE OVER')))
    assert stream.effective_limit == 1 and stream.last_error == 'subscribe_failed' and stream.sent == {}
    assert ws.sent == [ping]  # acks are never answered
    # 'ALREADY IN USE' wins over an approval rejection and keeps the cached key.
    with pytest.raises(ts.StreamRejected, match='already has a WebSocket session'):
        run(stream.handle(None, json.dumps({'header': {}, 'body': {'msg_cd': 'OPSP0011', 'msg1': 'ALREADY IN USE appkey'}})))
    assert stream.cache.deleted == []
    with pytest.raises(ts.StreamRejected, match='approval key rejected'):
        run(stream.handle(None, json.dumps({'header': {}, 'body': {'msg_cd': 'X', 'msg1': ' Invalid Approval : NOT FOUND '}})))
    assert stream.cache.deleted == ['market:kis:ws-approval']


def us_frame(tr_key='RBAQNVDA', symbol='NVDA', local=None, last='223.9800', short=False):
    local = local or datetime(2026, 9, 24, 23, 13, 13, tzinfo=NEW_YORK)
    fields = dict.fromkeys(ts.FIELDS, '1') | {
        'RSYM': tr_key, 'SYMB': symbol, 'XYMD': local.strftime('%Y%m%d'), 'XHMS': local.strftime('%H%M%S'),
        'LAST': last, 'SIGN': '5', 'DIFF': '0.6000', 'RATE': '-0.27', 'HIGH': '', 'TVOL': '52000'}
    values = [fields[name] for name in ts.FIELDS][:-1 if short else None]
    return values


def kr_frame(code='005930', local=None, price='281500'):
    local = local or datetime.now(SEOUL)
    values = ['0'] * 46
    values[0], values[1], values[2], values[3], values[4], values[5] = code, local.strftime('%H%M%S'), price, '2', '5000', '1.81'
    values[8], values[9], values[12], values[13], values[14], values[33] = '283000', '279000', '10', '120000', '', local.strftime('%Y%m%d')
    return values


def message(tr_id, *records):
    return f'0|{tr_id}|{len(records):03d}|' + '^'.join('^'.join(values) for values in records)


def test_trade_quote_keys_and_windows():
    now = datetime(2026, 9, 24, 23, 13, 14, tzinfo=NEW_YORK).timestamp()
    fields = dict(zip(ts.FIELDS, us_frame()))
    bare = ts.trade_quote(fields, 'NVDA', 'RBAQNVDA', 'c1', now=now)
    assert ts.trade_quote(fields, 'NVDA', 'HDFSCNT0|RBAQNVDA', 'c1', now=now) == bare
    assert (bare['exchange'], bare['data_status'], bare['valid_sessions'], bare['change'], bare['high'], bare['volume']) == (
        'BAQ', 'KIS BAQ 실시간 체결 · 데이마켓', ['overnight'], Decimal('-0.6'), None, '52000')
    regular = datetime(2026, 9, 24, 10, 0, 0, tzinfo=NEW_YORK)
    primary = ts.trade_quote(dict(zip(ts.FIELDS, us_frame('DNASNVDA', local=regular))), 'NVDA', 'HDFSCNT0|DNASNVDA', 'c1',
                             now=regular.timestamp() + 1)
    assert (primary['exchange'], primary['data_status'], primary['valid_sessions']) == (
        'NAS', 'KIS NAS 실시간 체결', ['pre_market', 'regular', 'after_hours'])
    # A print is accepted up to 24 h old and 60 s in the future.
    evening = datetime(2026, 9, 24, 19, 0, 0, tzinfo=NEW_YORK).timestamp()
    for offset, accepted in ((-86399, True), (-86400, False), (60, True), (61, False)):
        local = datetime.fromtimestamp(evening + offset, NEW_YORK)
        q = ts.trade_quote(dict(zip(ts.FIELDS, us_frame('DNASNVDA', local=local))), 'NVDA', 'DNASNVDA', 'c1', now=evening)
        assert (q is not None) == accepted, offset


def test_korean_trade_windows(monkeypatch):
    monkeypatch.setattr(ts.kr_session, 'clock_session', lambda when=None, trading_day=True: 'regular')
    now = datetime(2026, 9, 23, 21, 0, 0, tzinfo=SEOUL).timestamp()
    # A print is accepted up to 12 h old and 60 s in the future, on the same Seoul date.
    for offset, accepted in ((-43199, True), (-43200, False), (60, True), (61, False)):
        values = kr_frame(local=datetime.fromtimestamp(now + offset, SEOUL))
        assert (ts.kr_trade_quote(values, 'KR:005930', 'c1', now=now) is not None) == accepted, offset
    q = ts.kr_trade_quote(kr_frame(local=datetime.fromtimestamp(now, SEOUL)), 'KR:005930', 'c1', now=now, venue='KRX')
    assert q['data_status'] == 'KIS KRX 실시간 체결' and q['turnover'] is None and q['native_price'] == Decimal('281500.0000')
    assert ts.kr_trade_quote(kr_frame(price='364000'), 'KR:005930', 'c1', reference='280000') is not None   # exactly 30 % away
    assert ts.kr_trade_quote(kr_frame(price='364100'), 'KR:005930', 'c1', reference='280000') is None
    assert ts.kr_trade_quote(kr_frame(price='196000'), 'KR:005930', 'c1', reference='280000') is not None
    assert ts.kr_trade_quote(kr_frame(price='195900'), 'KR:005930', 'c1', reference='280000') is None


def test_trade_frames_route_by_subscription(monkeypatch):
    stream = ts.TradeStream(StreamKIS(), StreamCache())
    stored = []
    monkeypatch.setattr(stream, 'store', lambda symbol, q: stored.append((symbol, q)))
    monkeypatch.setattr(stream, 'reference', lambda symbol: None)
    monkeypatch.setattr(ts.time, 'time', lambda: datetime(2026, 9, 24, 23, 13, 14, tzinfo=NEW_YORK).timestamp())
    stream.conn = 'c1'
    # A requested subscription already delivers before its ack; an unsubscribe request does not.
    stream.sent = {'HDFSCNT0|RBAQNVDA': ('NVDA', 'subscribe', 0), 'HDFSCNT0|RBAQAAPL': ('AAPL', 'unsubscribe', 0)}
    asyncio.run(stream.handle(None, message('HDFSCNT0', us_frame(), us_frame('RBAQAAPL', 'AAPL'), us_frame('RBAQMSFT', 'MSFT'))))
    assert [(s, q['stream_conn']) for s, q in stored] == [('NVDA', 'c1')]
    # A record too short for the US layout is skipped.
    stream.active = {'HDFSCNT0|RBAQNVDA': 'NVDA'}
    stored.clear()
    asyncio.run(stream.handle(None, '1|HDFSCNT0|001|' + '^'.join(us_frame(short=True))))
    assert stored == []
    monkeypatch.undo()
    monkeypatch.setattr(stream, 'store', lambda symbol, q: stored.append((symbol, q)))
    monkeypatch.setattr(ts.kr_session, 'clock_session', lambda when=None, trading_day=True: 'after_hours')
    references = []
    monkeypatch.setattr(stream, 'reference', lambda symbol: references.append(symbol) or '281000')
    stream.active = {'H0STCNT0|035720': 'KR:035720', 'H0UNCNT0|005930': 'KR:005930'}
    asyncio.run(stream.handle(None, message('H0STCNT0', kr_frame('035720'))))
    asyncio.run(stream.handle(None, message('H0UNCNT0', kr_frame('005930'))))
    assert [(s, q['venue']) for s, q in stored] == [('KR:035720', 'KRX'), ('KR:005930', 'UNIFIED')]
    assert references == ['KR:035720', 'KR:005930']
    # The last validated print is the reference before Redis is asked.
    stored.clear()
    stream.latest['KR:005930'] = {'native_price': '900000'}
    asyncio.run(stream.handle(None, message('H0UNCNT0', kr_frame('005930'))))
    assert stored == [] and references == ['KR:035720', 'KR:005930']


def test_trade_store_throttle_keepalive_and_ttls(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(ts.time, 'monotonic', lambda: clock[0])
    cache = StreamCache()
    stream = ts.TradeStream(StreamKIS(), cache)

    def trade(stamp):
        return {'symbol': 'AAPL', 'price': Decimal(1), 'native_price': Decimal(1), 'timestamp': stamp, 'market': 'US'}

    def tick(seconds):  # binary fractions keep the clock exact
        clock[0] += seconds

    stream.store('AAPL', trade(1))
    assert cache.values['market:trade:AAPL']['timestamp'] == 1 and cache.ttls['market:trade:AAPL'] == 43200
    assert [(q['timestamp'], ttl) for _, q, ttl in cache.published] == [(1, 120)]
    tick(.25); stream.store('AAPL', trade(2))
    assert len(cache.published) == 1 and stream.latest['AAPL']['timestamp'] == 2   # two per second at most
    tick(.25); stream.store('AAPL', trade(3))
    assert [q['timestamp'] for _, q, _ in cache.published] == [1, 3]
    tick(.25); stream.store('AAPL', trade(4))
    stream.flush()
    assert len(cache.published) == 2  # not subscribed: nothing to flush
    stream.active = {'HDFSCNT0|DNASAAPL': 'AAPL'}
    stream.flush()
    assert [q['timestamp'] for _, q, _ in cache.published] == [1, 3, 4]
    tick(29.75); stream.flush()
    assert len(cache.published) == 3
    tick(.25); stream.flush()
    assert [q['timestamp'] for _, q, _ in cache.published] == [1, 3, 4, 4]  # keepalive
    # Korean prints are converted with the reference rate.
    stream.fx = type('FX', (), {'krw_to_usd': lambda self: (Decimal('0.000714285714'), '2026-09-23')})()
    stream.store('KR:005930', {'symbol': 'KR:005930', 'native_price': Decimal('281500'), 'timestamp': 5, 'market': 'KR'})
    stored = cache.values['market:trade:KR:005930']
    assert (stored['price'], stored['fx_rate'], stored['fx_date']) == (Decimal('201.0714'), Decimal('0.000714285714'), '2026-09-23')
    stream.state, stream.last_message, stream.queued = 'connected', 7.5, ['MSFT']
    stream.write_status(True)
    status = cache.values['market:stream:status']
    assert list(status) == ['state', 'connected', 'conn', 'heartbeat', 'last_message', 'subscribed', 'limit', 'queued',
                            'reconnects', 'last_error']
    assert cache.ttls['market:stream:status'] == 30 and status['subscribed'] == ['AAPL'] and status['queued'] == ['MSFT']
    assert stream.approval() == 'approved' and cache.ttls['market:kis:ws-approval'] == 43200


def test_trade_stream_lookup_retries_and_leader_lease(monkeypatch):
    clock = [5000.0]
    monkeypatch.setattr(ts.time, 'monotonic', lambda: clock[0])
    kis = StreamKIS()
    stream = ts.TradeStream(kis, StreamCache())
    lookups = []

    def missing(symbol):
        lookups.append(symbol)
        raise MarketError('no exchange')
    stream.lookup = type('Lookup', (), {'exchange': lambda self, symbol: missing(symbol)})()
    assert stream.exchange('ZZZ') is None and lookups == ['ZZZ']
    clock[0] = 5599.5
    assert stream.exchange('ZZZ') is None and lookups == ['ZZZ']
    clock[0] = 5600.0
    assert stream.exchange('ZZZ') is None and lookups == ['ZZZ', 'ZZZ']
    # The KIS trading day is asked again only after ten minutes.
    assert stream.kr_trading_day() is True and len(kis.calls) == 1 and kis.calls[0][3] == 86400
    clock[0] = 6200.0
    assert stream.kr_trading_day() is True and len(kis.calls) == 1
    clock[0] = 6200.5
    assert stream.kr_trading_day() is True and len(kis.calls) == 2

    class Client:
        def __init__(self, owner): self.owner, self.calls = owner, []
        def set(self, key, value, nx, ex): self.calls.append(('set', key, ex)); return self.owner is None
        def get(self, key): self.calls.append(('get', key)); return self.owner
        def expire(self, key, seconds): self.calls.append(('expire', key, seconds))
    stream.cache.client = Client(None)
    assert stream.lead() and stream.cache.client.calls == [('set', 'market:stream:leader', 30), ('expire', 'market:stream:leader', 30)]
    stream.cache.client = Client(stream.token)
    assert stream.lead() and stream.cache.client.calls[-1] == ('expire', 'market:stream:leader', 30)
    stream.cache.client = Client('other')
    assert not stream.lead() and stream.cache.client.calls[-1] == ('get', 'market:stream:leader')


# Search merging ------------------------------------------------------------------

class MasterRedis:
    def __init__(self, rows): self.rows = rows
    def get_json(self, key): return self.rows if key == 'market:symbols:kr' else None


def kr_row(code, name):
    return {'symbol': 'KR:' + code, 'name': name, 'category': 'kr', 'currency': 'KRW', 'exchange': 'kospi'}


def test_search_merges_sources_in_order_without_duplicates(monkeypatch):
    from app.instruments import discover
    from app.multi_market import MultiMarket
    master = ([kr_row('005930', '삼성전자 다른 이름'), kr_row('028260', '삼성물산'), kr_row('028260', '삼성물산 중복')]
              + [kr_row(f'{100000 + n}', f'삼성테스트{n}') for n in range(10)]
              + [kr_row(f'{200000 + n}', f'테슬라관련{n}') for n in range(35)]
              + [kr_row(f'{300000 + n}', f'KR테스트{n}') for n in range(32)])
    monkeypatch.setattr('app.kr_symbols.redis_cache', MasterRedis(master))
    us_rows = [{'symbol': 'TSLA', 'name': '테슬라', 'english': 'TESLA INC', 'exchange': 'NAS', 'etf': False},
               {'symbol': 'TSLL', 'name': '디렉시온 테슬라 2배 ETF', 'english': 'TSLA BULL 2X', 'exchange': 'NAS', 'etf': True},
               {'symbol': 'TSLA', 'name': '테슬라', 'english': 'DUPLICATE', 'exchange': 'NAS', 'etf': False}]
    monkeypatch.setattr('app.us_symbols._rows', lambda: us_rows)
    market = MultiMarket()
    market.us.key = ''
    quoted, finnhub = [], []
    try:
        # Catalog first; the master adds each new symbol once, and its first occurrence wins.
        assert market.search('삼성', 'kr') == ([{'symbol': 'KR:005930', 'name': '삼성전자', 'category': 'kr', 'currency': 'KRW'},
                                               kr_row('028260', '삼성물산')] + [kr_row(f'{100000 + n}', f'삼성테스트{n}') for n in range(10)])
        assert market.search('삼성', 'all') == market.search('삼성', 'kr')
        # Korea keeps every row; the other paths cap the list at 30, Korean rows before US ones.
        catalog = [r['symbol'] for r in discover('KR', 'kr')]
        assert [r['symbol'] for r in market.search('KR', 'kr')] == catalog + [f'KR:{300000 + n}' for n in range(30)]
        catalog = [r['symbol'] for r in discover('KR', 'all')]
        assert [r['symbol'] for r in market.search('KR', 'all')] == (catalog + [f'KR:{300000 + n}' for n in range(30)])[:30]
        assert [r['symbol'] for r in market.search('테슬라', 'all')] == [f'KR:{200000 + n}' for n in range(30)]
        assert market.search('테슬라', 'us') == [{'symbol': 'TSLA', 'name': '테슬라', 'category': 'us', 'currency': 'USD'},
                                                {'symbol': 'TSLL', 'name': '디렉시온 테슬라 2배 ETF', 'category': 'us', 'currency': 'USD'}]
        # A 6-digit code found in the master needs no quote; an unknown one asks KIS for the name.
        monkeypatch.setattr(market.kr, 'quote', lambda symbol: quoted.append(symbol) or {'name': '새종목'})
        assert market.search('100007', 'kr') == [kr_row('100007', '삼성테스트7')]
        assert market.search('KR:999999', 'all') == [{'symbol': 'KR:999999', 'name': '새종목', 'category': 'kr', 'currency': 'KRW'}]
        assert quoted == ['KR:999999']

        def search(query):
            finnhub.append(query)
            return ([{'symbol': 'MSFT', 'name': 'MICROSOFT CORP'}, {'symbol': 'MU', 'name': 'MICRON'},
                     {'symbol': 'MU', 'name': 'MICRON 2'}, {'symbol': 'BRK.B', 'name': 'BERKSHIRE'}]
                    + [{'symbol': f'MC{n}', 'name': f'M{n}'} for n in range(40)])
        monkeypatch.setattr(market.us, 'search', search)
        market.us.key = ''
        assert [r['symbol'] for r in market.search('Micro', 'us')] == ['MSFT'] and finnhub == []
        market.us.key = 'test'
        found = market.search('Micro', 'us')
        assert found[:2] == [{'symbol': 'MSFT', 'name': 'Microsoft', 'category': 'us', 'currency': 'USD'},
                             {'symbol': 'MU', 'name': 'MICRON', 'category': 'us', 'currency': 'USD'}]
        assert [r['symbol'] for r in found[2:]] == [f'MC{n}' for n in range(28)] and finnhub == ['Micro']
        assert market.search('Micro', 'kr') == [] and finnhub == ['Micro']
        assert [r['symbol'] for r in market.search('', 'us')][:3] == ['AAPL', 'MSFT', 'NVDA'] and finnhub == ['Micro']
    finally:
        market.close()


# Reference FX --------------------------------------------------------------------------

class FakeRedis:
    def __init__(self): self.data = {}
    def get(self, key): return self.data.get(key)
    def setex(self, key, ttl, value): self.data[key] = value
    def delete(self, key): self.data.pop(key, None)
    def lock(self, name, **kwargs):
        import threading
        return threading.Lock()


def test_reference_fx_shared_cache_repair_and_cooldown(monkeypatch):
    from app.multi_market import ReferenceFX
    from app.redis_cache import redis_cache
    fake = FakeRedis()
    monkeypatch.setattr(redis_cache, 'client', fake)
    today = datetime.now(timezone.utc).date().isoformat()
    good = {'base': 'USD', 'quote': 'KRW', 'rate': 1400, 'date': today}
    answers, calls, made = [], [], []

    def handler(request):
        calls.append(request.url.path)
        answer = answers.pop(0)
        return answer if isinstance(answer, httpx.Response) else httpx.Response(200, json=answer)

    def fx():
        made.append(ReferenceFX())
        made[-1].client.close()
        made[-1].client = httpx.Client(base_url='https://test', transport=httpx.MockTransport(handler))
        return made[-1]
    key = 'market:fx:USD:KRW:v2'
    none = '유효한 기준환율이 없어 한국 종목을 평가하거나 거래할 수 없습니다.'
    try:
        fake.data[key] = json.dumps(good | {'rate': 1250})
        assert fx().krw_to_usd() == (Decimal('0.000800000000'), today) and calls == []   # the shared value
        fake.data[key] = json.dumps(good | {'base': 'EUR'})
        answers.append(good)
        second = fx()
        assert second.krw_to_usd() == (Decimal('0.000714285714'), today) and calls == ['/v2/providers/ecb/rate/USD/KRW']
        assert json.loads(fake.data[key]) == good                                          # repaired
        assert second.krw_to_usd() == (Decimal('0.000714285714'), today) and len(calls) == 1
        fake.data.clear()
        answers.append(good | {'date': '2000-01-01'})
        third = fx()
        with pytest.raises(MarketError) as failure:
            third.krw_to_usd()
        assert str(failure.value) == none and key not in fake.data and len(calls) == 2
        with pytest.raises(MarketError) as failure:
            third.krw_to_usd()
        assert str(failure.value) == '환율 조회를 잠시 후 다시 시도하세요.' and len(calls) == 2
        answers.append(httpx.Response(503))
        fourth = fx()
        with pytest.raises(MarketError) as failure:
            fourth.krw_to_usd()
        assert str(failure.value) == none and isinstance(failure.value.__cause__, httpx.HTTPError) and len(calls) == 3
        with pytest.raises(MarketError, match='잠시 후'):
            fourth.krw_to_usd()
        answers.append(good | {'rate': 'x'})
        with pytest.raises(MarketError) as failure:
            fx().krw_to_usd()
        assert str(failure.value) == none and key not in fake.data and len(calls) == 4
    finally:
        for f in made:
            f.client.close()
