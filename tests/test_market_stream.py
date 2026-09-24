"""Real isolated Redis, no provider credentials. Finite async scenarios."""
import asyncio
import json
import os
import time
from decimal import Decimal

import pytest
from app.market_stream import QuoteHub, event, newer
from app.quote_data import public_quote, normalize_quote
from app.redis_cache import RedisCache


@pytest.fixture
def cache(monkeypatch):
    url = os.getenv('SSE_TEST_REDIS_URL')
    if not url:
        pytest.skip('SSE_TEST_REDIS_URL must name a dedicated disposable Redis')
    monkeypatch.setenv('REDIS_URL', url)
    cache = RedisCache()
    cache.client.flushdb()
    yield cache
    cache.client.flushdb()
    cache.client.close()


def quote(**changes):
    return dict(symbol='AAPL', price=Decimal('100.123456789'), native_price=Decimal('100.123456789'),
                currency='USD', timestamp=int(time.time()), stale=False, fx_rate=Decimal(1),
                _cached_at=time.time(), secret='never-public') | changes


def test_public_decimal_and_age():
    q = public_quote('AAPL', quote(timestamp=int(time.time())-1801))
    assert q['price'] == '100.123456789' and q['stale']
    assert 'secret' not in q and '_cached_at' not in q
    assert q['cached_at'] > q['timestamp']
    assert event('heartbeat', {'time': 1}).endswith('\n\n')
    assert 'event: heartbeat\n' in event('heartbeat', {'time': 1})
    with pytest.raises(ValueError):
        normalize_quote('AAPL', quote(price='NaN'))
    assert public_quote('KR:005930', quote(symbol='KR:005930', currency='KRW'))['currency'] == 'KRW'


def test_atomic_publication_versions_and_rejection(cache):
    sub = cache.client.pubsub()
    sub.subscribe('market:quote:updates')
    assert sub.get_message(timeout=2)['type'] == 'subscribe'
    q = quote()
    assert cache.store_quote('AAPL', q, 30)
    first = json.loads(sub.get_message(timeout=2)['data'])['version']
    assert cache.get_json('market:price:AAPL')['_version'] == first
    assert not cache.store_quote('AAPL', q | {'timestamp': q['timestamp']-1}, 30)
    assert not cache.store_quote('AAPL', q | {'price': '-1'}, 30)
    assert sub.get_message(timeout=.05) is None
    assert cache.store_quote('AAPL', q | {'native_price': '101'}, 30)
    corrected = json.loads(sub.get_message(timeout=2)['data'])['version']
    assert newer(corrected, first) and not newer(first, corrected)
    cache.client.delete('market:price:AAPL')
    restarted = RedisCache()
    assert restarted.store_quote('AAPL', q, 30)
    third = json.loads(sub.get_message(timeout=2)['data'])['version']
    assert newer(third, corrected)
    cache.client.delete('market:price:AAPL')
    cache.client.lpush('market:price:AAPL', 'bad-type')
    assert not cache.store_quote('AAPL', q, 30)
    assert sub.get_message(timeout=.05) is None
    sub.close()
    restarted.client.close()


def test_hubs_fanout_snapshot_race_bounded_cleanup(cache):
    async def scenario():
        hubs = [QuoteHub(cache.url), QuoteHub(cache.url)]
        for hub in hubs:
            await hub.start()
            await asyncio.wait_for(hub.ready.wait(), 3)
        queues = [hubs[0].listen('AAPL') for _ in range(100)] + [hubs[1].listen('AAPL')]
        try:
            # Register first, publish during snapshot read, then dedupe buffered event.
            assert cache.store_quote('AAPL', quote(), 30)
            snapshot = await hubs[0].snapshot('AAPL')
            messages = await asyncio.wait_for(asyncio.gather(*(q.get() for q in queues)), 3)
            assert all(item[1]['version'] == snapshot['version'] for item in messages)
            assert not newer(messages[0][1]['version'], snapshot['version'])
            for i in range(10):
                assert cache.store_quote('AAPL', quote(price=100+i), 30)
            await asyncio.sleep(.2)
            assert all(q.qsize() == 1 for q in queues)
            assert hubs[0].metrics['coalesced'] > 0
            # Pub/Sub connection loss: resubscription converges even without new publish.
            await hubs[0].redis.execute_command('CLIENT', 'KILL', 'TYPE', 'pubsub')
            assert cache.store_quote('AAPL', quote(price=999), 30)
            deadline = time.monotonic()+5
            while time.monotonic() < deadline:
                item = await asyncio.wait_for(queues[0].get(), 5)
                if item[1].get('quote', {}).get('price') == '999':
                    break
            else:
                pytest.fail('no recovered snapshot')
            await hubs[0]._update('{bad json')
            assert hubs[0].metrics['invalid']
        finally:
            for q in queues[:-1]:
                hubs[0].unlisten('AAPL', q)
            hubs[1].unlisten('AAPL', queues[-1])
            for hub in hubs:
                await hub.stop()
                assert not hub.listeners and not hub.tasks and hub.metrics['active'] == 0
    asyncio.run(scenario())


def test_interest_and_shared_leases(cache, monkeypatch):
    async def scenario():
        hubs = [QuoteHub(cache.url), QuoteHub(cache.url)]
        for hub in hubs:
            await hub.start()
        try:
            # Two processes share interest; N clients produce a single refresh request.
            for _ in range(100):
                await hubs[0].interest('AAPL', missing=True)
            await hubs[1].interest('AAPL', missing=True)
            assert cache.client.llen('market:refresh') == 1
            future = time.time()+601
            monkeypatch.setattr('app.market_stream.time.time', lambda: future)
            await hubs[0].interest('AAPL')
            assert cache.requested_symbols() == ['AAPL']
            keys = ['market:sse:user:1', 'market:sse:ip:test']
            for i in range(5):
                assert await hubs[i % 2].lease(keys, str(i))
            assert not await hubs[0].lease(keys, 'six')
            assert await hubs[1].lease(keys, '0', renew=True)
            await hubs[0].release(keys, '0')
            assert await hubs[1].lease(keys, 'six')
            assert not await hubs[0].lease(keys, '0', renew=True)
        finally:
            for hub in hubs:
                await hub.stop()
    asyncio.run(scenario())


def test_reset_epoch(cache):
    assert cache.store_quote('AAPL', quote(), 30)
    old = cache.get_json('market:price:AAPL')['_version']
    cache.client.flushdb()
    assert cache.store_quote('AAPL', quote(), 30)
    new = cache.get_json('market:price:AAPL')['_version']
    assert old.split(':')[0] != new.split(':')[0]
    assert not newer(new, old)  # Only an explicit snapshot may reset epochs.


def test_two_independent_web_processes(cache):
    import subprocess
    import sys
    code = '''
import asyncio,sys
from app.market_stream import QuoteHub
async def run():
    hub=QuoteHub(sys.argv[1]);await hub.start()
    try:
        await asyncio.wait_for(hub.ready.wait(),3)
        queue=hub.listen('MSFT');print('ready',flush=True)
        item=await asyncio.wait_for(queue.get(),5)
        print(item[1]['quote']['price'],flush=True)
    finally:
        await hub.stop()
asyncio.run(run())
'''
    processes = [subprocess.Popen([sys.executable, '-c', code, cache.url], stdout=subprocess.PIPE, text=True) for _ in range(2)]
    try:
        for process in processes:
            assert process.stdout.readline().strip() == 'ready'
        assert cache.store_quote('MSFT', quote(symbol='MSFT'), 30)
        for process in processes:
            output, _ = process.communicate(timeout=8)
            assert process.returncode == 0 and output.strip() == '100.123456789'
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=3)
