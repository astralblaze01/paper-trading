"""Characterization tests for the web core: request middleware, ranking cache and market-status helpers."""
import hashlib
import hmac
import io
import logging
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from uuid import uuid4
from zoneinfo import ZoneInfo
import pytest
from sqlalchemy import func, select
from test_service import database, client, register, FakeMarket, order
from app import main, security
from app.db import Session, User, LimitOrder, Transaction, UserProfileImage
from app.market import MarketError
from app.portfolio import RETURN_BASIS
from app.trading import execute_order


def uid_of(name):
    with Session() as db: return db.scalar(select(User.id).where(User.username == name))


# 요청 미들웨어 -------------------------------------------------------------

RATE_CASES = [
    ('POST', '/api/login', 'auth', 20), ('GET', '/api/login', 'auth', 20),
    ('POST', '/api/register', 'auth', 20), ('POST', '/api/account/delete', 'auth', 20),
    ('GET', '/api/profile', 'profile', 30), ('POST', '/api/profile', 'profile', 30),
    ('POST', '/api/profile/image', 'profile', 30), ('POST', '/api/profile/image/delete', 'profile', 30),
    ('GET', '/api/profiles', 'profile', 30),  # a prefix, not a path segment
    ('POST', '/api/orders', 'trade', 30), ('POST', '/api/fx/exchange', 'trade', 30),
    ('POST', '/api/limit-orders', 'trade', 30),
    ('GET', '/api/orders', None, None), ('PUT', '/api/orders', None, None),
    ('GET', '/api/fx/exchange', None, None), ('GET', '/api/limit-orders', None, None),
    ('POST', '/api/limit-orders/1/cancel', None, None),
    ('GET', '/api/search', 'market', 120), ('POST', '/api/search', 'market', 120),
    ('GET', '/api/quote/AAPL', 'market', 120), ('GET', '/api/quotes', 'market', 120),
    ('GET', '/api/candles/AAPL', 'market', 120), ('GET', '/api/explore', 'market', 120),
    ('GET', '/api/order-preview', 'market', 120), ('POST', '/api/fx/preview', 'market', 120),
    ('GET', '/api/fx/share', 'market', 120), ('GET', '/api/market-status/AAPL', 'market', 120),
    ('GET', '/api/company/AAPL', 'market', 120), ('GET', '/api/portfolios/alice', 'market', 120),
    ('GET', '/api/performance/me', 'market', 120), ('GET', '/api/performance/alice', 'market', 120),
    ('POST', '/api/popularity', 'event', 60), ('GET', '/api/popularity', 'event', 60),
    ('GET', '/api/popularity/x', None, None),
    ('GET', '/api/portfolio', None, None), ('GET', '/api/market-overview', None, None),
    ('GET', '/api/market-stream/AAPL', None, None), ('GET', '/api/session', None, None),
    ('POST', '/api/logout', None, None), ('GET', '/api/ranking', None, None),
    ('GET', '/api/transactions', None, None), ('GET', '/api/weekly', None, None),
    ('GET', '/api/fx', None, None), ('GET', '/api/fx/history', None, None),
    ('GET', '/api/watchlist', None, None), ('GET', '/api/notice', None, None),
    ('GET', '/api/admin', None, None), ('GET', '/api/users/alice/avatar', None, None),
    ('GET', '/health', None, None), ('GET', '/', None, None), ('POST', '/internal/jobs', None, None),
]


def record_limits(monkeypatch, allowed=True):
    calls = []
    def allow(key, limit, seconds=60):
        calls.append((key, limit)); return allowed
    monkeypatch.setattr(security.limiter, 'allow', allow)
    return calls


def test_rate_limit_category_and_limit_for_every_branch(client, monkeypatch):
    calls = record_limits(monkeypatch)
    for method, path, category, limit in RATE_CASES:
        calls.clear()
        client.request(method, path)
        expected = [(category, limit)] if category else []
        assert [(key[1], n) for key, n in calls] == expected, (method, path)
    calls.clear()
    client.get('/api/search')
    client.get('/api/search', headers={'x-real-ip': '203.0.113.9'})
    assert [key[0] for key, _ in calls] == ['testclient', '203.0.113.9']


def test_rate_limited_response_and_uncategorised_paths(client, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger='request')
    record_limits(monkeypatch, allowed=False)
    r = client.get('/api/search', headers={'x-request-id': 'req-429'})
    assert r.status_code == 429 and r.json() == {'detail': '요청이 너무 많습니다. 잠시 후 다시 시도하세요.'}
    assert r.headers['retry-after'] == '60' and r.headers['x-request-id'] == 'req-429'
    assert 'cache-control' not in r.headers
    assert not [rec for rec in caplog.records if rec.name == 'request']  # no access log for a refusal
    generated = client.post('/api/login', headers={'x-request-id': 'not valid!'}).headers['x-request-id']
    assert re.fullmatch(r'[0-9a-f]{24}', generated)
    assert client.get('/api/session').status_code == 200  # never limited


def test_request_id_cache_control_and_access_log(client, caplog):
    caplog.set_level(logging.INFO, logger='request')
    for supplied, echoed in (('abc-123_x.y', True), ('a' * 80, True), ('a' * 81, False), ('bad id', False), ('', False)):
        got = client.get('/api/session', headers={'x-request-id': supplied}).headers['x-request-id']
        assert (got == supplied) if echoed else re.fullmatch(r'[0-9a-f]{24}', got), supplied
    assert client.get('/api/session').headers['cache-control'] == 'no-store'
    assert client.get('/api/does-not-exist').headers['cache-control'] == 'no-store'
    assert client.get('/api/ranking').headers['cache-control'] == 'no-store'  # 401 as well
    assert 'cache-control' not in client.get('/health').headers
    assert client.get('/').headers['cache-control'] == 'no-cache'
    headers = {'x-csrf-token': register(client, 'painter')}
    from PIL import Image
    png = io.BytesIO(); Image.new('RGB', (40, 30)).save(png, 'PNG')
    assert client.post('/api/profile/image', headers=headers | {'content-type': 'image/png'}, content=png.getvalue()).status_code == 200
    assert client.get('/api/users/painter/avatar').headers['cache-control'] == 'private, max-age=86400'
    caplog.clear()
    client.get('/api/session', headers={'x-request-id': 'trace-1'})
    rec, = [rec for rec in caplog.records if rec.name == 'request']
    assert rec.getMessage() == 'request completed'
    assert (rec.request_id, rec.user_id, rec.method, rec.path, rec.status_code) == ('trace-1', uid_of('painter'), 'GET', '/api/session', 200)
    assert isinstance(rec.duration_ms, float)


# 랭킹 -------------------------------------------------------------------

RANKING_KEYS = ['rows', 'errors', 'incomplete', 'base_currency', 'updated_at', 'return_basis', 'stale',
                'refresh_interval_seconds', 'refreshed', 'market_open', 'market_status', 'next_refresh_at']
ROW_KEYS = ['rank', 'username', 'equity', 'equity_usd', 'return_pct', 'stale', 'fx', 'image_version']
HOLD = '시세 또는 기준환율을 확인할 수 없어 랭킹을 보류합니다.'
SEOUL = ZoneInfo('Asia/Seoul')
B1 = datetime(2026, 9, 24, 9, 0, 10, tzinfo=SEOUL)
B2 = B1 + timedelta(seconds=10)


def at_bucket(monkeypatch, bucket):
    monkeypatch.setattr(main, '_ranking_bucket', lambda now=None: bucket)
    return (bucket + timedelta(seconds=10)).astimezone(timezone.utc).isoformat()


class Provider:
    def __init__(self, status): self.status = status
    def market_status(self):
        if isinstance(self.status, Exception): raise self.status
        return self.status


def no_revalue(*args, **kwargs):
    raise AssertionError('must be served from the cached snapshot')


def test_ranking_same_bucket_serves_the_cached_payload(client, monkeypatch):
    main._ranking_cache.clear()
    boundary = at_bucket(monkeypatch, B1)
    register(client)
    first = client.get('/api/ranking').json()
    assert list(first) == RANKING_KEYS and list(first['rows'][0]) == ROW_KEYS
    assert first['refreshed'] is True and first['market_open'] is None and first['market_status'] == []
    assert first['next_refresh_at'] == boundary and first['errors'] == [] and first['incomplete'] is False
    cached = main._ranking_cache[main.market]
    assert set(cached) == {'bucket', 'payload'} and cached['bucket'] == B1 and list(cached['payload']) == RANKING_KEYS[:8]
    monkeypatch.setattr(main, 'wallet_portfolio', no_revalue)
    second = client.get('/api/ranking').json()
    assert list(second) == RANKING_KEYS and second == first | {'refreshed': False}


def test_ranking_prunes_ineligible_rows_in_the_cached_payload(client, monkeypatch):
    main._ranking_cache.clear()
    at_bucket(monkeypatch, B1)
    register(client, 'alice'); register(client, 'bob')
    first = client.get('/api/ranking').json()
    assert [(r['rank'], r['username']) for r in first['rows']] == [(1, 'alice'), (2, 'bob')]
    with Session.begin() as db: db.scalar(select(User).where(User.username == 'alice')).active = False
    monkeypatch.setattr(main, 'wallet_portfolio', no_revalue)
    second = client.get('/api/ranking').json()
    assert second['rows'] == [first['rows'][1] | {'rank': 1}] and list(second['rows'][0]) == ROW_KEYS
    assert main._ranking_cache[main.market]['payload']['rows'] == second['rows']  # pruned in place


def test_ranking_failure_without_a_cache(client, monkeypatch):
    main._ranking_cache.clear()
    boundary = at_bucket(monkeypatch, B1)
    register(client)
    monkeypatch.setattr(main, 'wallet_portfolio', lambda *args: {'return_pct': None})
    r = client.get('/api/ranking').json()
    assert list(r) == RANKING_KEYS
    assert r == {'rows': [], 'errors': [HOLD], 'incomplete': True, 'base_currency': 'USD', 'updated_at': None,
                 'return_basis': RETURN_BASIS, 'stale': True, 'refresh_interval_seconds': 10,
                 'refreshed': True, 'market_open': None, 'market_status': [], 'next_refresh_at': boundary}
    assert main._ranking_cache[main.market] == {'bucket': B1, 'payload': {k: r[k] for k in RANKING_KEYS[:8]}}
    # A valued equity without a USD figure is incomplete as well.
    main._ranking_cache.clear()
    monkeypatch.setattr(main, 'wallet_portfolio', lambda *args: {'return_pct': D(1), 'equity_usd': None})
    assert client.get('/api/ranking').json()['errors'] == [HOLD]
    monkeypatch.setattr(main, 'wallet_portfolio', no_revalue)
    again = client.get('/api/ranking').json()
    assert list(again) == RANKING_KEYS and again == r | {'refreshed': False}


def test_ranking_failure_with_a_cache_is_not_revalued_within_its_bucket(client, monkeypatch):
    main._ranking_cache.clear()
    at_bucket(monkeypatch, B1)
    register(client)
    first = client.get('/api/ranking').json()
    boundary = at_bucket(monkeypatch, B2)
    calls, real = [], main.wallet_portfolio
    monkeypatch.setattr(main, 'wallet_portfolio', lambda *args: calls.append(args) or {'return_pct': None})
    failed = client.get('/api/ranking').json()
    assert list(failed) == RANKING_KEYS
    assert failed == first | {'errors': [HOLD], 'incomplete': True, 'refreshed': False, 'stale': True, 'next_refresh_at': boundary}
    assert main._ranking_cache[main.market]['bucket'] == B1   # the last good snapshot keeps its own time
    # Later requests in the failed bucket get the same answer without valuing every account again.
    for _ in range(3):
        assert client.get('/api/ranking').json() == failed
    assert len(calls) == 1
    # The next bucket tries again; once prices are back it publishes a fresh snapshot.
    at_bucket(monkeypatch, B2 + timedelta(seconds=10))
    assert client.get('/api/ranking').json()['incomplete'] is True and len(calls) == 2
    boundary = at_bucket(monkeypatch, B2 + timedelta(seconds=20))
    monkeypatch.setattr(main, 'wallet_portfolio', real)
    recovered = client.get('/api/ranking').json()
    assert recovered['refreshed'] is True and recovered['errors'] == [] and recovered['incomplete'] is False
    assert recovered['next_refresh_at'] == boundary and recovered['rows'] == first['rows']
    assert client.get('/api/ranking').json() == recovered | {'refreshed': False}


def test_ranking_closed_market_serves_the_last_snapshot(client, monkeypatch):
    main._ranking_cache.clear()
    at_bucket(monkeypatch, B1)
    register(client)
    status = {'label': '휴장', 'timezone': 'Asia/Seoul', 'verified': True}
    main.market.providers = {'KR': Provider(status), 'US': Provider(status)}
    first = client.get('/api/ranking').json()
    labels = [status | {'market': 'KR'}, status | {'market': 'US'}]
    assert list(first) == RANKING_KEYS and first['refreshed'] is True
    assert first['market_open'] is False and first['market_status'] == labels
    boundary = at_bucket(monkeypatch, B2)
    monkeypatch.setattr(main, 'wallet_portfolio', no_revalue)
    second = client.get('/api/ranking').json()
    assert list(second) == RANKING_KEYS and second == first | {'refreshed': False, 'next_refresh_at': boundary}
    assert main._ranking_cache[main.market]['bucket'] == B1


def test_ranking_closed_market_without_a_good_snapshot_revalues(client, monkeypatch):
    main._ranking_cache.clear()
    at_bucket(monkeypatch, B1)
    register(client)
    real = main.wallet_portfolio
    monkeypatch.setattr(main, 'wallet_portfolio', lambda *args: {'return_pct': None})
    assert client.get('/api/ranking').json()['updated_at'] is None
    main.market.providers = {'KR': Provider({'label': '장마감'}), 'US': Provider({'label': '장마감'})}
    at_bucket(monkeypatch, B2)
    monkeypatch.setattr(main, 'wallet_portfolio', real)
    r = client.get('/api/ranking').json()
    assert r['refreshed'] is True and r['market_open'] is False and r['updated_at'] and r['rows'][0]['username'] == 'alice'
    assert main._ranking_cache[main.market]['bucket'] == B2


def test_ranking_interval_helpers():
    stamp = datetime(2026, 9, 24, 0, 0, 29, 999999, tzinfo=timezone.utc)
    assert main._ranking_bucket(stamp) == datetime(2026, 9, 24, 9, 0, 20, tzinfo=SEOUL)
    assert main._ranking_bucket(stamp).tzinfo == main.SEOUL
    assert main._next_ranking_boundary(stamp) == datetime(2026, 9, 24, 0, 0, 30, tzinfo=timezone.utc)
    assert main._next_ranking_boundary(stamp).tzinfo == timezone.utc
    assert main.RANKING_INTERVAL == timedelta(seconds=10)


# 시장 상태 ----------------------------------------------------------------

UNKNOWN = {'label': '장 상태 확인 불가', 'verified': False}

RANKING_STATE_CASES = [
    # (KR status, US status, open, unknown); None means no provider for that market
    ({'session': 'regular', 'label': '정규장', 'open': True}, {'session': 'closed', 'label': '장마감', 'open': False}, True, False),
    ({'session': 'closed', 'label': '장마감', 'open': False}, {'session': 'closed', 'label': '장마감', 'open': False}, False, False),
    ({'label': '휴장'}, {'label': '휴장'}, False, False),
    ({'label': '정규장'}, None, True, False),
    (None, {'label': '데이마켓'}, True, False),
    ({'label': '장후'}, {'label': '장마감'}, True, False),
    ({'label': '휴장'}, {'label': '장 상태 확인 불가'}, None, True),
    ({'label': '정규장'}, {'label': '장 상태 확인 불가'}, True, True),
    ({'session': 'unknown', 'open': True}, None, True, True),
    ({'session': 'unknown', 'label': '정규장'}, None, True, True),
    (RuntimeError('down'), {'label': '장마감'}, None, True),
    ({'session': None, 'label': '장 상태 확인 불가'}, None, False, False),  # explicit None session is not unknown
    ({'label': '점심'}, None, False, False),  # unrecognised label without a session counts as closed
    ({'label': '정규장', 'open': False}, None, False, False),
    ({'label': '장마감', 'open': True}, None, True, False),
    ({}, None, False, False),
]


def test_ranking_market_state_for_statuses_and_labels():
    assert not hasattr(main.market, 'providers')
    assert main._ranking_market_state() == {'open': None, 'unknown': True, 'labels': []}
    main.market.providers = None
    assert main._ranking_market_state() == {'open': None, 'unknown': True, 'labels': []}
    main.market.providers = {'KR': object(), 'US': object()}
    assert main._ranking_market_state() == {'open': None, 'unknown': True, 'labels': []}
    for kr, us, is_open, is_unknown in RANKING_STATE_CASES:
        main.market.providers = {code: Provider(s) for code, s in (('KR', kr), ('US', us)) if s is not None}
        labels = [(UNKNOWN if isinstance(s, Exception) else s) | {'market': code} for code, s in (('KR', kr), ('US', us)) if s is not None]
        assert main._ranking_market_state() == {'open': is_open, 'unknown': is_unknown, 'labels': labels}, (kr, us)
    source = {'label': '정규장'}
    main.market.providers = {'KR': Provider(source)}
    assert main._ranking_market_state()['labels'] == [{'label': '정규장', 'market': 'KR'}]
    assert source == {'label': '정규장'}  # the provider's dict is copied, not tagged
    main.market.providers = {'KR': Provider(None)}
    assert main._ranking_market_state() == {'open': False, 'unknown': False, 'labels': [{'market': 'KR'}]}


def unknown(name): return f'{name} 시장 상태를 확인할 수 없어 주문할 수 없습니다. 잠시 후 다시 시도해주세요.'
def holiday(name): return f'오늘은 {name} 주식시장 휴장일이므로 주문할 수 없습니다. 다음 거래일 장 운영 시간에 다시 주문해주세요.'
def closed(name, label): return f'현재 {name} 주식시장이 휴장 중({label})이므로 주문할 수 없습니다. 장 운영 시간에 다시 주문해주세요.'


CLOSED_MESSAGE_CASES = [
    ('US', {'session': 'regular', 'label': '정규장', 'open': True, 'tradable': True}, None),
    ('US', {'session': 'overnight', 'label': '데이마켓'}, None),
    ('KR', {'session': 'after_hours', 'label': '애프터장'}, None),
    ('US', {'session': 'closed', 'label': '장마감', 'open': False}, closed('미국', '장마감')),
    ('US', {'session': 'closed'}, closed('미국', '장마감')),
    ('KR', {'session': 'closed', 'label': '휴장'}, holiday('한국')),
    ('US', {'session': 'regular', 'label': '휴장'}, holiday('미국')),
    ('US', {'session': 'unknown', 'label': '휴장'}, unknown('미국')),
    ('KR', {'session': 'overnight', 'label': '데이마켓', 'open': True}, closed('한국', '데이마켓')),
    ('US', {'session': 'regular', 'label': '정규장', 'open': False}, closed('미국', '정규장')),
    ('US', {'session': 'after_hours', 'label': '애프터장', 'tradable': False}, '미국 애프터장은 열려 있지만 주문에 쓸 시세를 확인할 수 없어 주문할 수 없습니다.'),
    ('KR', {'session': 'regular', 'label': '정규장', 'open': False, 'tradable': False}, closed('한국', '정규장')),
    ('US', {'session': 'unknown', 'label': '장 상태 확인 불가'}, unknown('미국')),
    # Label-only (legacy) statuses
    ('KR', {'label': '장후'}, None),
    ('KR', {'label': '데이마켓'}, closed('한국', '데이마켓')),
    ('US', {'label': '데이마켓'}, None),
    ('US', {'label': '휴장'}, holiday('미국')),
    ('KR', {'label': '장마감'}, closed('한국', '장마감')),
    ('US', {'label': '점심'}, unknown('미국')),  # unrecognised label: unknown, unlike the ranking state
    ('US', {'session': None, 'label': '정규장'}, None),
    ('US', {'session': '', 'label': '장전'}, None),
    ('KR', None, unknown('한국')),
    ('US', {}, unknown('미국')),
    ('KR', MarketError('down'), unknown('한국')),
    ('US', KeyError('label'), unknown('미국')),
    ('US', TypeError('bad'), unknown('미국')),
]


def test_closed_market_message_for_statuses_and_labels():
    assert main.closed_market_message('AAPL') is None  # no providers attribute
    main.market.providers = None
    assert main.closed_market_message('AAPL') is None
    main.market.providers = {'US': object()}
    assert main.closed_market_message('AAPL') is None
    main.market.providers = {'US': Provider({'label': '휴장'})}
    assert main.closed_market_message('KR:005930') is None  # only the symbol's own market counts
    for code, status, message in CLOSED_MESSAGE_CASES:
        main.market.providers = {code: Provider(status), ('US' if code == 'KR' else 'KR'): Provider({'label': '휴장'})}
        assert main.closed_market_message('KR:005930' if code == 'KR' else 'AAPL') == message, (code, status)
    main.market.providers = {'US': Provider(RuntimeError('unexpected'))}
    with pytest.raises(RuntimeError):
        main.closed_market_message('AAPL')


# 세션 · 거래내역 · 공개 조회 ------------------------------------------------

SESSION_KEYS = ['quote_sse_enabled', 'active', 'quote_max_age', 'csrf', 'username', 'is_admin', 'market_configured', 'providers']


def test_session_payload(client, monkeypatch):
    monkeypatch.delenv('US_MAX_QUOTE_AGE', raising=False)
    monkeypatch.delenv('MAX_QUOTE_AGE', raising=False)
    s = client.get('/api/session').json()
    assert list(s) == SESSION_KEYS
    assert {k: v for k, v in s.items() if k != 'csrf'} == {
        'quote_sse_enabled': main.quote_sse_enabled(), 'active': False, 'quote_max_age': {'US': 1800, 'KR': 900},
        'username': None, 'is_admin': False, 'market_configured': True, 'providers': {'us': True, 'kr': True}}
    assert s['csrf'] and client.get('/api/session').json()['csrf'] == s['csrf']
    monkeypatch.setenv('US_MAX_QUOTE_AGE', '77')
    monkeypatch.setenv('MAX_QUOTE_AGE', '55')
    register(client)
    s = client.get('/api/session').json()
    assert list(s) == SESSION_KEYS and s['quote_max_age'] == {'US': 77, 'KR': 55}
    assert (s['active'], s['username'], s['is_admin']) == (True, 'alice', False)
    with Session.begin() as db: db.scalar(select(User).where(User.username == 'alice')).is_admin = True
    assert client.get('/api/session').json()['is_admin'] is True
    main.market = type('Bare', (), {'key': '', 'client': FakeMarket.client})()
    s = client.get('/api/session').json()
    assert s['market_configured'] is False and s['providers'] == {'us': False, 'kr': False}


TRANSACTION_KEYS = ['id', 'symbol', 'side', 'quantity', 'price', 'currency', 'native_price', 'fx_rate', 'fx_date',
                    'quote_time', 'created_at', 'gross_amount', 'fee', 'tax', 'net_amount', 'realized_pnl',
                    'accounting_version', 'order_requested_at', 'market_session', 'venue', 'quote_source',
                    'price_mode', 'quote_stale']


def test_transactions_projection_and_pages(client):
    register(client)
    uid = uid_of('alice')
    for _ in range(51): execute_order(uid, order(), FakeMarket())
    first = client.get('/api/transactions').json()
    assert len(first) == 50 and list(first[0]) == TRANSACTION_KEYS
    assert [t['id'] for t in first] == sorted((t['id'] for t in first), reverse=True)
    second = client.get('/api/transactions?page=2').json()
    assert len(second) == 1 and second[0]['id'] < first[-1]['id']
    assert client.get('/api/transactions?page=3').json() == []
    assert client.get('/api/transactions?page=0').status_code == 422


PUBLIC_KEYS = ['username', 'wallets', 'positions', 'equity', 'equity_usd', 'base_currency', 'pnl', 'return_pct',
               'return_basis', 'fx', 'errors', 'stale', 'initial_equity', 'initial_fx_date',
               'initial_fx_effect', 'other_pnl', 'profile', 'member_since', 'member_days']


def test_public_lookups_share_visibility_but_keep_their_messages(client):
    register(client, 'owner')
    p = client.get('/api/portfolios/owner')
    assert p.status_code == 200 and list(p.json()) == PUBLIC_KEYS
    assert client.get('/api/performance/owner').json()['username'] == 'owner'
    register(client, 'viewer')
    with Session.begin() as db:
        db.add(User(username='boss', password_hash='x', is_admin=True))
        db.add(User(username='gone', password_hash='x', active=False))
    for name in ('nobody', 'boss', 'gone', 'Nobody'):
        r = client.get(f'/api/portfolios/{name}')
        assert r.status_code == 404 and r.json() == {'detail': '공개 포트폴리오를 찾을 수 없습니다.'}, name
        r = client.get(f'/api/performance/{name}')
        assert r.status_code == 404 and r.json() == {'detail': '공개 성과 기록을 찾을 수 없습니다.'}, name


def test_public_account_return_explains_fx_difference_from_holding(client, monkeypatch):
    register(client, 'owner')
    uid = uid_of('owner')
    with Session.begin() as db:
        u = db.get(User, uid)
        u.initial_krw = D('136860000')
        u.initial_fx_date = '2026-09-24'
    class FX:
        def current_rate(self, *args):
            return {'rate': D('1355.05'), 'date': '2026-09-25', 'stale': False}
    class Prices(FakeMarket):
        price = D('148.7')
        def quote(self, symbol):
            return super().quote(symbol) | {'price': self.price, 'native_price': self.price, 'currency': 'USD'}
    prices = Prices()
    monkeypatch.setattr(main, 'fx', FX())
    monkeypatch.setattr(main, 'market', prices)
    monkeypatch.setenv('US_BUY_FEE_BPS', '0')
    execute_order(uid, order(quantity=672), prices)
    prices.price = D('148.6602')
    own = client.get('/api/portfolio').json()
    public = client.get('/api/portfolios/owner').json()
    for key in ('pnl', 'return_pct', 'initial_equity', 'initial_fx_date', 'initial_fx_effect', 'other_pnl'):
        assert public[key] == own[key]
    assert round(public['positions'][0]['return_pct'], 2) == -0.03
    assert round(public['return_pct'], 2) == -1.02
    assert public['initial_fx_effect'] == -1355000
    assert public['other_pnl'] == pytest.approx(-36241.62528)
    assert public['pnl'] == pytest.approx(public['initial_fx_effect'] + public['other_pnl'])


def test_public_lookups_ignore_username_case_like_login_and_avatars(client):
    # Usernames are stored lowercase (registration and login lowercase them), so any
    # casing of a name means that one account on every public endpoint.
    register(client, 'owner')
    with Session.begin() as db:
        owner = db.scalar(select(User).where(User.username == 'owner'))
        db.add(UserProfileImage(user_id=owner.id, content_type='image/webp', data=b'webp-bytes',
                                updated_at=datetime.now(timezone.utc)))
    exact = client.get('/api/portfolios/owner').json()
    for name in ('Owner', 'OWNER', 'oWnEr'):
        r = client.get(f'/api/portfolios/{name}')
        assert r.status_code == 200 and r.json() == exact, name
        r = client.get(f'/api/performance/{name}')
        assert r.status_code == 200 and r.json()['username'] == 'owner', name
        r = client.get(f'/api/users/{name}/avatar')
        assert r.status_code == 200 and r.content == b'webp-bytes', name


PERFORMANCE_KEYS = ['username', 'period', 'from', 'to', 'timezone', 'return_basis',
                    'snapshots', 'period_return_pct', 'period_start', 'period_end', 'baseline_changed', 'benchmarks']
BAD_RANGE = {'detail': '기간은 1W, 1M, 3M, 1Y, YTD, ALL 또는 YYYY-MM-DD 형식입니다.'}


def test_performance_date_range(client):
    register(client)
    today = datetime.now(timezone.utc).astimezone(SEOUL).date()
    def get(query):
        r = client.get(f'/api/performance/me?{query}')
        assert r.status_code == 200, (query, r.text)
        body = r.json()
        assert list(body) == PERFORMANCE_KEYS and body['timezone'] == 'Asia/Seoul'
        assert body['return_basis'] == 'KRW 평가액 · 외부 입출금 보정' and body['username'] == 'alice'
        return body['period'], body['from'], body['to']
    assert get('') == ('1M', str(today - timedelta(30)), str(today))
    assert get('period=1W') == ('1W', str(today - timedelta(7)), str(today))
    assert get('period=ALL') == ('ALL', None, str(today))
    assert get('period=YTD') == ('YTD', str(today.replace(month=1, day=1) - timedelta(1)), str(today))
    assert get('from=2026-01-02') == (None, '2026-01-02', str(today))
    assert get('to=2026-01-05&period=1W') == (None, None, '2026-01-05')
    assert get('from=2026-01-02&to=2026-01-05') == (None, '2026-01-02', '2026-01-05')
    assert get('from=2026-01-05&to=2026-01-05') == (None, '2026-01-05', '2026-01-05')
    assert get('from=&to=&period=3M') == ('3M', str(today - timedelta(91)), str(today))
    for query in ('from=x', 'to=2026-02-30', 'from=2026-01-02&to=junk'):
        r = client.get(f'/api/performance/me?{query}')
        assert r.status_code == 422 and r.json() == BAD_RANGE, query
    r = client.get('/api/performance/me?from=2026-01-06&to=2026-01-05')
    assert r.status_code == 422 and r.json() == {'detail': '시작일이 종료일보다 늦습니다.'}
    assert client.get('/api/performance/me?period=2W').status_code == 422


# 주문 재요청 · 내부 작업 --------------------------------------------------

def test_order_replays_a_queued_market_order_only_for_the_same_request(client):
    headers = {'x-csrf-token': register(client)}
    uid = uid_of('alice')
    rows = {}
    for key, order_type, status in (('market', 'market', 'pending'), ('filled', 'market', 'filled'), ('limit', 'limit', 'pending')):
        rows[key] = str(uuid4())
        with Session.begin() as db:
            db.add(LimitOrder(user_id=uid, request_id=rows[key], symbol='AAPL', side='buy', quantity=2, limit_price=D(1),
                              order_type=order_type, use_max=False, status=status, created_at=datetime.now(timezone.utc)))
    with Session() as db: ids = {k: db.scalar(select(LimitOrder.id).where(LimitOrder.request_id == v)) for k, v in rows.items()}
    body = {'symbol': 'AAPL', 'side': 'buy', 'quantity': 2}
    # The replay is answered before the market-hours check.
    main.market.providers = {'US': Provider({'label': '휴장'})}
    r = client.post('/api/orders', headers=headers, json=body | {'request_id': rows['market']})
    assert r.status_code == 200 and r.json() == {'id': ids['market'], 'pending': True, 'status': 'pending', 'replayed': True}
    assert list(r.json()) == ['id', 'pending', 'status', 'replayed']
    r = client.post('/api/orders', headers=headers, json=body | {'request_id': rows['filled']})
    assert r.json() == {'id': ids['filled'], 'pending': False, 'status': 'filled', 'replayed': True}
    for change in ({'quantity': 3}, {'side': 'sell'}, {'symbol': 'MSFT'}, {'use_max': True}):
        r = client.post('/api/orders', headers=headers, json=body | change | {'request_id': rows['market']})
        assert r.status_code == 409 and r.json() == {'detail': '동일 주문 ID에 다른 요청을 사용할 수 없습니다.'}, change
    r = client.post('/api/orders', headers=headers, json=body | {'request_id': rows['limit']})
    assert r.status_code == 409 and r.json() == {'detail': '동일 주문 ID에 다른 요청을 사용할 수 없습니다.'}
    r = client.post('/api/orders', headers=headers, json=body | {'request_id': str(uuid4())})
    assert r.status_code == 409 and r.json() == {'detail': holiday('미국')}


def test_filled_orders_replay_after_the_market_closes(client):
    headers = {'x-csrf-token': register(client)}
    post = lambda body: client.post('/api/orders', headers=headers, json=body)
    main.market.providers = {'US': Provider({'label': '정규장', 'session': 'regular', 'open': True, 'tradable': True})}
    buy = {'symbol': 'AAPL', 'side': 'buy', 'quantity': 3, 'request_id': str(uuid4())}
    sell = {'symbol': 'AAPL', 'side': 'sell', 'quantity': 1, 'request_id': str(uuid4())}
    sell_max = {'symbol': 'AAPL', 'side': 'sell', 'quantity': 1, 'use_max': True, 'request_id': str(uuid4())}
    filled = {}
    for body in (buy, sell, sell_max):
        r = post(body)
        assert r.status_code == 200 and r.json()['replayed'] is False
        filled[body['request_id']] = r.json()
    assert filled[sell_max['request_id']]['quantity'] == 2
    main.market.providers = {'US': Provider({'label': '장마감', 'session': 'closed', 'open': False})}
    for body in (buy, sell, sell_max):
        first = filled[body['request_id']]
        r = post(body)
        assert r.status_code == 200 and r.json() == {'id': first['id'], 'replayed': True, 'quantity': first['quantity']}
    # use_max replays whatever quantity the retry carries, exactly as while the market is open.
    assert post(sell_max | {'quantity': 7}).json()['replayed'] is True
    # The idempotency guard still refuses a different order under a used id.
    for change in ({'side': 'sell'}, {'quantity': 4}, {'symbol': 'MSFT'}):
        r = post(buy | change)
        assert r.status_code == 409 and r.json()['detail'] == '동일 주문 ID에 다른 주문을 사용할 수 없습니다.'
    # A new request id is a new order and still needs an open market.
    r = post(buy | {'request_id': str(uuid4())})
    assert r.status_code == 409 and r.json()['detail'].startswith('현재 미국 주식시장이 휴장 중(장마감)')
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(Transaction)) == 3


def test_internal_jobs_accept_the_worker_token(client, monkeypatch, caplog):
    token = hmac.new(main.secret.encode(), b'paper-worker', hashlib.sha256).hexdigest()
    monkeypatch.setenv('DAILY_SNAPSHOT_ENABLED', 'false')
    r = client.post('/internal/jobs', headers={'x-worker-token': token})
    assert r.status_code == 200 and list(r.json()) == ['filled', 'weekly', 'snapshots']
    assert r.json() == {'filled': 0, 'weekly': 'disabled', 'snapshots': 'disabled'}
    for bad in ('', token[:-1], token.upper()):
        r = client.post('/internal/jobs', headers={'x-worker-token': bad})
        assert r.status_code == 403 and r.json() == {'detail': 'Forbidden'}
    caplog.set_level(logging.INFO, logger='request')
    monkeypatch.setenv('DAILY_SNAPSHOT_ENABLED', 'true')
    monkeypatch.setenv('DAILY_SNAPSHOT_HOUR', '24')  # the capture raises; the job still answers
    assert client.post('/internal/jobs', headers={'x-worker-token': token}).json()['snapshots'] == 'error'
    assert [rec.getMessage() for rec in caplog.records if rec.name == 'request' and rec.levelno == logging.ERROR] == ['daily snapshot failed']


def test_worker_token_is_the_jobs_credential(client, monkeypatch):
    token = security.worker_token(main.secret)
    assert token == hmac.new(main.secret.encode(), b'paper-worker', hashlib.sha256).hexdigest()
    assert security.WORKER_TOKEN_HEADER == 'x-worker-token'
    monkeypatch.setenv('DAILY_SNAPSHOT_ENABLED', 'false')
    assert client.post('/internal/jobs', headers={security.WORKER_TOKEN_HEADER: token}).status_code == 200
    other = security.worker_token(main.secret + 'x')
    assert client.post('/internal/jobs', headers={security.WORKER_TOKEN_HEADER: other}).status_code == 403


# OpenAPI 노출 ---------------------------------------------------------------

def test_openapi_document_is_not_served_by_default(client):
    assert main.app.openapi_url is None
    for path in ('/openapi.json', '/docs', '/redoc'):
        assert client.get(path).status_code == 404, path
    # The schema itself is still built in-process, with its operation ids.
    ids = {op['operationId'] for item in main.app.openapi()['paths'].values() for op in item.values()}
    assert 'admin_info_api_admin_get' in ids and 'order_api_orders_post' in ids


def test_openapi_document_can_be_enabled_for_development():
    import os, subprocess, sys
    probe = ("from fastapi.testclient import TestClient\n"
             "from app import main\n"
             "r = TestClient(main.app).get('/openapi.json')\n"
             "print(main.app.openapi_url, r.status_code, '/api/admin' in r.json()['paths'])\n")
    result = subprocess.run([sys.executable, '-c', probe], env=os.environ | {'OPENAPI_ENABLED': 'true'},
                            capture_output=True, text=True, check=True)
    assert result.stdout.split() == ['/openapi.json', '200', 'True']
