import asyncio
import os
import time
from decimal import Decimal

import pytest
from fastapi import HTTPException
from itsdangerous import TimestampSigner
from starlette.requests import Request
from sqlalchemy import select
from test_service import database, client, register, seed, order
from test_market_stream import cache, quote
from app import main
from app.db import Session, User, Transaction
from app.market_stream import QuoteHub
from app.multi_market import MultiMarket
from app.trading import execute_order


def test_stream_auth_and_flag(client, monkeypatch):
    assert client.get('/api/market-stream/AAPL').status_code == 401
    register(client)
    assert client.get('/api/market-stream/AAPL').status_code == 503
    monkeypatch.setenv('QUOTE_SSE_ENABLED', 'true')
    monkeypatch.setenv('MARKET_CACHE_MODE', 'worker')
    monkeypatch.setenv('REDIS_URL', 'redis://unused')
    main.app.state.quote_hub.redis = object()
    assert client.get('/api/market-stream/bad!').status_code == 422
    with Session.begin() as db:
        db.scalar(select(User).where(User.username == 'alice')).active = False
    assert client.get('/api/market-stream/AAPL').status_code == 403
    main.app.state.quote_hub.redis = None


def test_order_reads_current_server_cache(cache, monkeypatch):
    monkeypatch.setenv('MARKET_CACHE_MODE', 'worker')
    monkeypatch.setattr('app.multi_market.redis_cache', cache)
    market = MultiMarket()
    try:
        uid = seed()
        assert cache.store_quote('AAPL', quote(price='100', native_price='100'), 30)
        shown = market.quote('AAPL')
        assert cache.store_quote('AAPL', quote(price='123', native_price='123'), 30)
        result = execute_order(uid, order(), market)
        with Session() as db:
            trade = db.get(Transaction, result['id'])
            assert trade.native_price == Decimal('123') and shown['native_price'] == 100
        assert cache.store_quote('AAPL', quote(timestamp=int(time.time())+59), 30)
    finally:
        market.close()


def test_revalidates_after_lock_wait(monkeypatch):
    from app import trading
    from test_service import FakeMarket
    original = trading.validate_quote
    calls = []
    def check(symbol, q, allow_stale=False):
        calls.append(1)
        if len(calls) == 2:
            q = q | {'timestamp': int(time.time())-1900}
        return original(symbol, q, allow_stale)
    monkeypatch.setattr(trading, 'validate_quote', check)
    with pytest.raises(HTTPException) as failure:
        execute_order(seed(), order(), FakeMarket())
    assert failure.value.status_code == 409 and len(calls) == 2
    with Session() as db:
        assert db.scalar(select(Transaction)) is None


def test_finite_stream_heartbeat_auth_cleanup(cache, monkeypatch):
    monkeypatch.setenv('QUOTE_SSE_ENABLED', 'true')
    monkeypatch.setenv('MARKET_CACHE_MODE', 'worker')
    monkeypatch.setenv('QUOTE_SSE_HEARTBEAT', '5')
    cookie = TimestampSigner(os.environ['SESSION_SECRET']).sign('fixture').decode()
    request = Request({'type': 'http', 'headers': [(b'cookie', f'paper_session={cookie}'.encode()), (b'host', b'test')],
                       'client': ('test', 1), 'method': 'GET', 'path': '/api/market-stream/AAPL'})
    async def scenario():
        hub = QuoteHub(cache.url)
        await hub.start()
        await asyncio.wait_for(hub.ready.wait(), 3)
        checks = []
        def authenticate(request):
            checks.append(1)
            if len(checks) > 1:
                raise HTTPException(403)
        try:
            assert cache.store_quote('AAPL', quote(), 30)
            response = await hub.response(request, 'AAPL', 1, authenticate)
            iterator = response.body_iterator
            assert 'event: snapshot' in await anext(iterator)
            assert 'event: heartbeat' in await asyncio.wait_for(anext(iterator), 6)
            assert 'auth_required' in await asyncio.wait_for(anext(iterator), 6)
            with pytest.raises(StopAsyncIteration):
                await anext(iterator)
            assert not hub.listeners and hub.metrics['active'] == 0
            assert cache.client.zcard('market:sse:user:1') == 0
            response = await hub.response(request, 'AAPL', 1, lambda _: None)
            await anext(response.body_iterator)
            await response.body_iterator.aclose()
            assert not hub.listeners
        finally:
            await hub.stop()
    asyncio.run(scenario())


def test_shared_admission_and_origin(cache, monkeypatch):
    monkeypatch.setenv('QUOTE_SSE_ENABLED', 'true')
    monkeypatch.setenv('MARKET_CACHE_MODE', 'worker')
    async def scenario():
        hub = QuoteHub(cache.url)
        await hub.start()
        await asyncio.wait_for(hub.ready.wait(), 3)
        def request(origin=None):
            headers = [(b'host', b'test')]
            if origin:
                headers.append((b'origin', origin.encode()))
            return Request({'type': 'http', 'headers': headers, 'client': ('test', 1), 'method': 'GET', 'path': '/'})
        try:
            with pytest.raises(HTTPException) as denied:
                await hub.response(request('https://other'), 'AAPL', 1, lambda _: None)
            assert denied.value.status_code == 403
            keys = ['market:sse:user:1', 'market:sse:ip:fixture']
            for n in range(5):
                assert await hub.lease(keys, str(n))
            with pytest.raises(HTTPException) as limited:
                await hub.response(request(), 'AAPL', 1, lambda _: None)
            assert limited.value.status_code == 429
            assert not hub.listeners
            cache.client.set('market:sse:attempt:user:2', 30)
            with pytest.raises(HTTPException) as attempts:
                await hub.response(request(), 'AAPL', 2, lambda _: None)
            assert attempts.value.status_code == 429
        finally:
            await hub.stop()
    asyncio.run(scenario())


def test_worker_mode_missing_cache_never_calls_provider(cache, monkeypatch):
    from app.market import MarketError
    monkeypatch.setenv('MARKET_CACHE_MODE', 'worker')
    monkeypatch.setattr('app.multi_market.redis_cache', cache)
    market = MultiMarket()
    monkeypatch.setattr(market, 'quote_direct', lambda _: pytest.fail('worker direct fallback'))
    try:
        with pytest.raises(MarketError):
            market.quote('AAPL')
        assert cache.client.llen('market:refresh') == 1
    finally:
        market.close()
