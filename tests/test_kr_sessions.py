"""Korean KRX/NXT sessions, unified quotes, capability and common order checks."""
import asyncio
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from fastapi import HTTPException

from test_service import database, client, register  # noqa: F401  (shared fixtures)
from app import main
from app.kr_quotes import unified_quote, valid_sessions
from app.kr_session import SEOUL, clock_session, venues
from app.quote_policy import assess, rejection
from app.trading import validate_quote
from app import trade_stream as ts


def seoul(text):
    return datetime.fromisoformat(text).replace(tzinfo=SEOUL)


@pytest.mark.parametrize('local,expected', [
    ('2026-09-23T07:59', 'closed'), ('2026-09-23T08:00', 'pre_market'), ('2026-09-23T08:49', 'pre_market'),
    ('2026-09-23T08:55', 'closed'),         # KRX opening auction: no continuous trading
    ('2026-09-23T09:00', 'regular'), ('2026-09-23T15:29', 'regular'),
    ('2026-09-23T15:35', 'closed'),         # break between the sessions
    ('2026-09-23T15:40', 'after_hours'), ('2026-09-23T19:59', 'after_hours'), ('2026-09-23T20:00', 'closed'),
    ('2026-09-26T10:00', 'closed'),         # Saturday
])
def test_korean_timetable(local, expected):
    assert clock_session(seoul(local)) == expected


def test_venues_follow_the_timetable():
    assert venues('pre_market') == ['NXT']
    assert venues('after_hours', seoul('2026-09-23T15:45')) == ['NXT']
    assert venues('after_hours', seoul('2026-09-23T16:30')) == ['NXT', 'KRX']


def test_capability_decides_extended_sessions():
    assert valid_sessions({'nxt': True, 'etp': False}) == ['pre_market', 'regular', 'after_hours']
    assert valid_sessions({'nxt': False, 'etp': False}) == ['regular', 'after_hours']   # KRX after-market only
    assert valid_sessions({'nxt': False, 'etp': True}) == ['regular']                  # ETF/ETN
    assert valid_sessions({'nxt': False, 'etp': True, 'known': False}) == ['regular']  # unknown: least capable


def kr_quote(stamp, sessions=('pre_market', 'regular', 'after_hours'), origin='rest', conn=None):
    return {'symbol': 'KR:005930', 'price': D('200'), 'native_price': D('281500'), 'currency': 'KRW',
            'timestamp': int(stamp), 'stale': False, 'origin': origin, 'source': 'KIS', 'venue': 'UNIFIED',
            'valid_sessions': list(sessions), 'stream_conn': conn}


@pytest.fixture
def korea_now(monkeypatch):
    """Pin the Korean trade-session clock so stamps taken 'now' fall in a chosen session."""
    from app import quote_policy
    def pin(session):
        monkeypatch.setattr(quote_policy.kr_session, 'clock_session', lambda when=None, trading_day=True: session)
    return pin


@pytest.mark.parametrize('session', ['pre_market', 'regular', 'after_hours'])  # KR-1, KR-3, KR-4
def test_unified_print_fills_in_its_session(korea_now, session):
    korea_now(session)
    q = assess(kr_quote(time.time() - 60), session, None)
    assert (q['market'], q['session_tradeable'], q['price_mode'], q['venue']) == ('KR', True, 'rest' if session == 'regular' else 'extended_rest', 'UNIFIED')
    assert validate_quote('KR:005930', q)[1] == D('281500')


def test_pre_market_without_nxt_listing_is_refused(korea_now):               # KR-2
    korea_now('pre_market')
    q = assess(kr_quote(time.time() - 60, sessions=valid_sessions({'nxt': False, 'etp': False})), 'pre_market', None)
    assert not q['session_tradeable']
    with pytest.raises(HTTPException) as exc:
        validate_quote('KR:005930', q)
    assert exc.value.detail == '현재 한국 프리장 체결 시세를 확인할 수 없어 주문할 수 없습니다.'


def test_etf_after_hours_is_refused(korea_now):                               # KR-5
    korea_now('after_hours')
    q = assess(kr_quote(time.time() - 60, sessions=valid_sessions({'nxt': False, 'etp': True})), 'after_hours', None)
    assert rejection(q) == '현재 한국 애프터장 체결 시세를 확인할 수 없어 주문할 수 없습니다.'


def test_regular_close_is_not_an_after_hours_price():
    # A 15:29 print stays a regular-session print at 16:30.
    close = seoul('2026-09-23T15:29').timestamp()
    q = assess(kr_quote(close), 'after_hours', None, now=seoul('2026-09-23T16:30').timestamp())
    assert q['trade_session'] == 'regular' and not q['session_tradeable']


class HolidayKIS:
    configured = True
    def __init__(self, open_day): self.open_day = open_day
    def get(self, path, tr_id, params, ttl=15, tr_cont=''):
        if path.endswith('chk-holiday'):
            return {'output': [{'bass_dt': params['BASS_DT'], 'opnd_yn': 'Y' if self.open_day else 'N'}]}
        raise AssertionError(path)


def test_provider_calendar_overrides_the_timetable(monkeypatch):              # KR-6, KR-7
    from app import kr_session, redis_cache as rc
    from app.providers import KRProvider
    monkeypatch.setattr(kr_session, 'clock_session', lambda now=None, trading_day=True: 'closed' if not trading_day else 'regular')
    monkeypatch.setattr(rc.redis_cache, 'stream_status', lambda: None)
    closed = KRProvider(HolidayKIS(False)).market_status()
    assert (closed['session'], closed['label'], closed['open'], closed['tradable'], closed['verified']) == ('closed', '휴장', False, False, True)
    opened = KRProvider(HolidayKIS(True)).market_status()
    assert (opened['session'], opened['open'], opened['tradable'], opened['venue'], opened['schedule_verified']) == ('regular', True, True, 'UNIFIED', False)
    # A special opening day that starts late: yesterday's prints never fill today.
    yesterday = time.time() - 86400
    assert not assess(kr_quote(yesterday), 'regular', None)['session_tradeable']


def test_holiday_order_is_refused_with_reason(client, monkeypatch):
    from app.providers import KRProvider
    headers = {'x-csrf-token': register(client)}
    main.market.providers = {'KR': KRProvider(HolidayKIS(False)), 'US': KRProvider(HolidayKIS(True))}
    r = client.post('/api/orders', headers=headers, json={'symbol': 'KR:005930', 'side': 'buy', 'quantity': 1, 'request_id': '6f3d3b0e-2f4a-4a53-9f66-0d7b4d5c1a11'})
    assert r.status_code == 409 and '휴장일' in r.json()['detail']


def test_stream_disconnect_falls_back_or_refuses(korea_now):                  # KR-8
    korea_now('after_hours')
    now = time.time()
    down = {'connected': False, 'conn': 'c2', 'heartbeat': now, 'subscribed': ['KR:005930']}
    recent = assess(kr_quote(now - 60, origin='stream', conn='c1'), 'after_hours', down, now=now)
    assert (recent['price_mode'], recent['realtime'], recent['session_tradeable']) == ('cached', False, True)
    old = assess(kr_quote(now - 3600, origin='stream', conn='c1'), 'after_hours', down, now=now)
    assert not old['session_tradeable']
    live = assess(kr_quote(now - 3600, origin='stream', conn='c2'), 'after_hours', down | {'connected': True}, now=now)
    assert live['price_mode'] == 'extended_stream' and live['realtime']


class BarsKIS:
    configured = True
    def __init__(self, bars): self.bars = bars
    def get(self, path, tr_id, params, ttl=15, tr_cont=''):
        if path.endswith('inquire-price'):
            return {'output': {'stck_sdpr': '0' if params['FID_COND_MRKT_DIV_CODE'] == 'NX' else '111000',
                               'rprs_mrkt_kor_name': 'ETF', 'bstp_kor_isnm': 'ETF(실물복제)'}}
        assert params['FID_COND_MRKT_DIV_CODE'] == 'J'   # not NXT-listed: KRX source
        return {'output1': {'stck_prpr': '113145', 'prdy_vrss': '1265'}, 'output2': self.bars}


def test_zero_volume_filler_bars_are_not_trades():
    now = datetime.now(SEOUL)
    bar = lambda minutes, vol: {'stck_bsop_date': (now - timedelta(minutes=minutes)).strftime('%Y%m%d'),
                                'stck_cntg_hour': (now - timedelta(minutes=minutes)).strftime('%H%M00'),
                                'stck_prpr': '113145', 'cntg_vol': vol}
    q = unified_quote(BarsKIS([bar(1, '0'), bar(2, '0'), bar(40, '25')]), 'KR:069500')
    assert q['timestamp'] == int(datetime.strptime(bar(40, '25')['stck_bsop_date'] + bar(40, '25')['stck_cntg_hour'], '%Y%m%d%H%M%S').replace(tzinfo=SEOUL).timestamp())
    assert q['valid_sessions'] == ['regular'] and q['change'] == D(1265) and q['venue'] == 'KRX'   # ETF: regular only
    only_fillers = unified_quote(BarsKIS([bar(1, '0'), bar(2, '0')]), 'KR:069500')
    assert only_fillers['stale'] and only_fillers['valid_sessions'] == []


# Korean stream frames ----------------------------------------------------------

def frame(code='005930', date=None, hour=None, price='281500', sign='2'):
    now = datetime.now(SEOUL)
    fields = ['0'] * 46
    fields[0], fields[1], fields[2], fields[3], fields[4], fields[5] = code, hour or now.strftime('%H%M%S'), price, sign, '5000', '1.81'
    fields[8], fields[9], fields[12], fields[13], fields[33] = '283000', '279000', '10', '120000', date or now.strftime('%Y%m%d')
    return fields


def test_korean_stream_trade_validation(monkeypatch):
    monkeypatch.setattr(ts.kr_session, 'clock_session', lambda when=None, trading_day=True: 'after_hours')
    q = ts.kr_trade_quote(frame(), 'KR:005930', 'c1', reference='280000')
    assert (q['native_price'], q['venue'], q['origin'], q['currency'], q['change']) == (D(281500), 'UNIFIED', 'stream', 'KRW', D(5000))
    assert ts.kr_trade_quote(frame(sign='5'), 'KR:005930', 'c1')['change'] == D(-5000)
    yesterday = (datetime.now(SEOUL) - timedelta(days=1)).strftime('%Y%m%d')
    assert ts.kr_trade_quote(frame(date=yesterday), 'KR:005930', 'c1') is None       # not today's business date
    assert ts.kr_trade_quote(frame(code='000660'), 'KR:005930', 'c1') is None          # another symbol
    assert ts.kr_trade_quote(frame(price='900000'), 'KR:005930', 'c1', reference='280000') is None  # misread field
    assert ts.kr_trade_quote(frame()[:10], 'KR:005930', 'c1') is None                   # short record
    monkeypatch.setattr(ts.kr_session, 'clock_session', lambda when=None, trading_day=True: 'closed')
    assert ts.kr_trade_quote(frame(), 'KR:005930', 'c1') is None                        # outside a session


def test_korean_symbols_share_the_stream_slots(monkeypatch):
    class WS:
        def __init__(self): self.sent = []
        async def send(self, m): import json; self.sent.append(json.loads(m)['body']['input'])
    stream = ts.TradeStream(BarsKIS([]), type('C', (), {'client': None, 'stream_interest': lambda s: []})())
    caps = {'KR:005930': {'nxt': True, 'etp': False}, 'KR:035720': {'nxt': False, 'etp': False}, 'KR:069500': {'nxt': False, 'etp': True}}
    monkeypatch.setattr(ts, 'capability', lambda kis, s: caps[s])
    monkeypatch.setattr(stream, 'exchange', lambda s: 'NAS')
    ws = WS()
    asyncio.run(stream.reconcile(ws, 'k', 'closed', ['KR:005930', 'KR:069500', 'KR:035720', 'AAPL'], kr='after_hours'))
    # NXT-listed: unified; KRX-only stock: KRX channel; the ETF cannot trade
    # after hours so it takes no slot; the US is closed.
    assert ws.sent == [{'tr_id': 'H0UNCNT0', 'tr_key': '005930'}, {'tr_id': 'H0STCNT0', 'tr_key': '035720'}]
    assert stream.queued == ['KR:069500', 'AAPL']
    data = '0|H0UNCNT0|001|' + '^'.join(frame())
    stream.active = {'H0UNCNT0|005930': 'KR:005930'}
    stored = []
    monkeypatch.setattr(stream, 'store', lambda s, q: stored.append(q))
    monkeypatch.setattr(stream, 'reference', lambda s: None)
    monkeypatch.setattr(ts.kr_session, 'clock_session', lambda when=None, trading_day=True: 'after_hours')
    asyncio.run(stream.handle(ws, data))
    assert stored and stored[0]['symbol'] == 'KR:005930'


# Common --------------------------------------------------------------------------

def test_open_but_not_tradable_is_explained(client):                          # Common-1
    class Open:
        def market_status(self): return {'label': '애프터장', 'session': 'after_hours', 'open': True, 'tradable': False}
    headers = {'x-csrf-token': register(client)}
    main.market.providers = {'KR': Open(), 'US': Open()}
    r = client.post('/api/orders', headers=headers, json={'symbol': 'KR:005930', 'side': 'buy', 'quantity': 1, 'request_id': '1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed'})
    assert r.status_code == 409 and '열려 있지만 주문에 쓸 시세를 확인할 수 없어' in r.json()['detail']


def base_quote(**changes):
    return {'symbol': 'AAPL', 'price': D(100), 'currency': 'USD', 'timestamp': int(time.time()), 'stale': False} | changes


def test_invalid_prices_and_times_are_refused():                             # Common-2..5
    for q, text in ((base_quote(stale=True), '오래된 시세'), (base_quote(price=D(0)), '유효한 가격'),
                    (base_quote(price=D(-1)), '유효한 가격'), (base_quote(timestamp=int(time.time()) + 3600), '만료'),
                    (base_quote(currency='KRW'), '통화')):
        with pytest.raises(HTTPException) as exc:
            validate_quote('AAPL', q)
        assert text in exc.value.detail


def test_redis_outage_degrades_without_crashing():                            # Common-6
    from redis.exceptions import ConnectionError as RedisConnectionError
    from app.redis_cache import RedisCache
    class Down:
        def __getattr__(self, name):
            def fail(*a, **k): raise RedisConnectionError('down')
            return fail
    cache = RedisCache(); cache.client = Down()
    assert cache.stream_status() is None and cache.stream_interest() == [] and cache.get_json('x') is None
    cache.request_stream('AAPL'); cache.request_quote('AAPL', force=True)
    assert cache.store_quote('AAPL', base_quote(native_price=D(100)), 30) is False


def test_previous_day_bars_use_that_days_last_trade():
    """On a holiday KIS answers 'bars up to now' with the previous day's bars
    up to the same clock time; the last trade of that day must be used."""
    class PrevDayKIS(BarsKIS):
        hours = []
        def get(self, path, tr_id, params, ttl=15, tr_cont=''):
            if path.endswith('inquire-price'):
                return super().get(path, tr_id, params, ttl)
            self.hours.append(params['FID_INPUT_HOUR_1'])
            day = (datetime.now(SEOUL) - timedelta(days=2)).strftime('%Y%m%d')
            price, hour = ('286500', '195900') if params['FID_INPUT_HOUR_1'] == '200000' else ('283750', '133100')
            return {'output1': {}, 'output2': [{'stck_bsop_date': day, 'stck_cntg_hour': hour, 'stck_prpr': price, 'cntg_vol': '5'}]}
    kis = PrevDayKIS([])
    q = unified_quote(kis, 'KR:069500')
    assert kis.hours[-1] == '200000' and q['native_price' if 'native_price' in q else 'price'] == D(286500) and q['stale']
