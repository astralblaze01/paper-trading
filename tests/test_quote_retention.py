"""Regression scenarios for last-price loss during the Friday/weekend transition."""
import asyncio
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from app import quote_policy
from app.fx import FxService
from app.fx_schedule import refresh_seconds, FRANKFURT
from app.market import MarketError
from app.market_stream import QuoteHub, newer
from app.multi_market import ReferenceFX, MultiMarket
from app.quote_data import normalize_quote
from app.redis_cache import RedisCache
from app.trading import validate_quote
from test_market_stream import cache, quote


def test_price_retained_and_stale_without_refresh(cache):
    q = quote()
    assert cache.store_quote('AAPL', q, 1)
    assert cache.client.ttl('market:price:AAPL') == -1
    saved = cache.get_json('market:price:AAPL')
    expired = normalize_quote('AAPL', saved, now=q['timestamp']+2000)
    assert expired['price'] == q['price'] and expired['stale']
    assert expired['timestamp'] == q['timestamp']
    assert expired['cached_at'] == q['_cached_at']
    with pytest.raises(HTTPException):
        validate_quote('AAPL', expired)


def test_migrate_orphan_metadata_display_only_then_recover(cache):
    q = quote()
    assert cache.store_quote('AAPL', q, 120)
    previous = cache.get_json('market:price:AAPL')['_version']
    cache.client.delete('market:price:AAPL')  # reproduces the old 120s expiry
    older = q | {'timestamp': q['timestamp']-30, 'price': '99', 'native_price': '99'}
    assert cache.store_quote('AAPL', older, 120)
    saved = cache.get_json('market:price:AAPL')
    assert saved['display_only'] and saved['timestamp'] == older['timestamp']
    assert newer(saved['_version'], previous)
    assert cache.get_json('market:quote-version:AAPL')['timestamp'] == q['timestamp']
    assessed = quote_policy.assess(saved, 'regular')
    assert assessed['stale'] and not assessed['tradeable']
    with pytest.raises(HTTPException):
        validate_quote('AAPL', assessed)
    # A newer print clears recovery mode without resetting sequence/high-water.
    assert cache.store_quote('AAPL', q, 120)
    assert not cache.get_json('market:price:AAPL')['display_only']


def test_closed_market_provider_regression_keeps_newer(cache, monkeypatch):
    from app.market_worker import collect_quote
    monkeypatch.setattr('app.market_worker.redis_cache', cache)
    q = quote()
    assert cache.store_quote('AAPL', q, 120)
    provider = SimpleNamespace(quote_direct=lambda _: q | {'timestamp': q['timestamp']-3600})
    _, state = collect_quote(provider, 'AAPL', 120)
    assert state == 'retained_newer'
    assert cache.get_json('market:price:AAPL')['timestamp'] == q['timestamp']
    assert cache.get_json('market:collection:AAPL')['state'] == state


def test_rest_and_sse_share_retained_snapshot(cache, monkeypatch):
    q = quote(timestamp=int(time.time())-7200)
    assert cache.store_quote('AAPL', q, 120)
    cache.set_json('market:collection:AAPL', {'state': 'failed'}, 60)
    monkeypatch.setenv('MARKET_CACHE_MODE', 'worker')
    monkeypatch.setattr('app.multi_market.redis_cache', cache)
    market = MultiMarket()
    monkeypatch.setattr(market, 'assess', lambda symbol, value: value)
    monkeypatch.setattr(market, 'quote_direct', lambda _: pytest.fail('unexpected provider access'))
    try:
        rest = market.quote('AAPL')
        assert rest['stale'] and rest['refresh_failed']
        async def snapshot():
            hub = QuoteHub(cache.url)
            await hub.start()
            try:
                result = await hub.snapshot('AAPL')
                assert result['quote']['price'] == str(rest['price'])
                assert result['quote']['timestamp'] == rest['timestamp']
                assert result['quote']['refresh_failed']
            finally:
                await hub.stop()
        asyncio.run(snapshot())
    finally:
        market.close()


@pytest.mark.parametrize('now,rate_day,next_day', [
    ('2026-09-26T12:00', '2026-09-25', '2026-09-28T16:15'),
    ('2026-09-25T18:00', '2026-09-25', '2026-09-28T16:15'),
    ('2026-04-03T12:00', '2026-04-02', '2026-04-07T16:15'),
    ('2026-12-25T12:00', '2026-12-24', '2026-12-28T16:15'),
])
def test_ecb_weekend_target_holidays(now, rate_day, next_day):
    start = datetime.fromisoformat(now).replace(tzinfo=FRANKFURT)
    expected = datetime.fromisoformat(next_day).replace(tzinfo=FRANKFURT)
    seconds = refresh_seconds(date.fromisoformat(rate_day), start)
    assert seconds == int(expected.timestamp()-start.timestamp())


def test_ecb_missing_friday_publication_retried():
    assert refresh_seconds(date(2026, 9, 24), datetime(2026, 9, 26, 12, tzinfo=FRANKFURT)) == 1800


def fx_fixture(monkeypatch, cache, handler):
    monkeypatch.setattr('app.multi_market.redis_cache', cache)
    fx = ReferenceFX()
    fx.client.close()
    fx.client = httpx.Client(base_url='https://fixture', transport=httpx.MockTransport(handler))
    return fx


def test_fx_outage_reuses_valid_saved_value_across_processes(cache, monkeypatch):
    today = datetime.now(timezone.utc).date().isoformat()
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={'base': 'USD', 'quote': 'KRW', 'rate': '1355.05', 'date': today}) if len(calls) == 1 else httpx.Response(503)
    fx = fx_fixture(monkeypatch, cache, handler)
    second = fx_fixture(monkeypatch, cache, handler)
    try:
        original = fx.krw_to_usd()
        assert cache.get_json('market:fx:USD:KRW:last-good')['rate'] == '1355.05'
        cache.client.delete('market:fx:USD:KRW:v2')
        fx.expires = 0
        assert fx.krw_to_usd() == original and fx.refresh_failed
        assert second.krw_to_usd() == original and second.refresh_failed
        assert len(calls) == 2  # shared outage backoff
        assert FxService(second).current_rate()['refresh_failed']
    finally:
        fx.client.close()
        second.client.close()


def test_fx_does_not_extend_seven_day_limit(cache, monkeypatch):
    day = (datetime.now(timezone.utc).date()-timedelta(days=8)).isoformat()
    cache.set_json('market:fx:USD:KRW:last-good', {'base': 'USD', 'quote': 'KRW', 'rate': 1350, 'date': day}, 86400)
    fx = fx_fixture(monkeypatch, cache, lambda _: httpx.Response(503))
    fx.cached = (Decimal('0.0007'), day)
    fx.expires = time.monotonic()+86400
    try:
        with pytest.raises(MarketError):
            fx.krw_to_usd()
    finally:
        fx.client.close()


def test_quote_health_reports_missing_and_failed_prices(cache):
    cache.request_quote('AAPL')
    assert cache.quote_health()['state'] == 'degraded'
    cache.store_quote('AAPL', quote(), 120)
    assert cache.quote_health()['available'] == 1
    cache.set_json('market:collection:AAPL', {'state': 'failed'}, 60)
    assert cache.quote_health()['failed'] == 1
