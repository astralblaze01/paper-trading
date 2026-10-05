"""KIS call spacing is shared by every process through Redis."""
import shutil
import socket
import subprocess
import time
import pytest
from app.redis_cache import RedisCache


@pytest.fixture
def redis_url():
    if not shutil.which('redis-server'): pytest.skip('redis-server is not installed')
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]
    server = subprocess.Popen(['redis-server', '--port', str(port), '--save', '', '--appendonly', 'no'], stdout=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try: socket.create_connection(('127.0.0.1', port), .1).close(); break
            except OSError: time.sleep(.1)
        yield f'redis://127.0.0.1:{port}/0'
    finally:
        server.terminate(); server.wait()


def cache(url, monkeypatch):
    monkeypatch.setenv('REDIS_URL', url)
    return RedisCache()


def test_two_processes_queue_behind_one_shared_slot(redis_url, monkeypatch):
    web, worker = cache(redis_url, monkeypatch), cache(redis_url, monkeypatch)
    assert web.reserve_slot('kis:overseas', 1.1) == 0
    assert 1.0 < worker.reserve_slot('kis:overseas', 1.1) <= 1.1
    assert 2.1 < web.reserve_slot('kis:overseas', 1.1) <= 2.2
    assert web.reserve_slot('kis:domestic', .2) == 0  # separate budgets


def test_without_redis_the_caller_keeps_its_own_spacing(monkeypatch):
    monkeypatch.setenv('REDIS_URL', '')
    assert RedisCache().reserve_slot('kis:overseas', 1.1) is None
    monkeypatch.setenv('REDIS_URL', 'redis://127.0.0.1:1/0')  # unreachable
    assert RedisCache().reserve_slot('kis:overseas', 1.1) is None


def test_rate_limits_are_shared_across_processes(redis_url, monkeypatch):
    from app import redis_cache as module
    from app.security import RateLimiter
    monkeypatch.setattr(module, 'redis_cache', cache(redis_url, monkeypatch))
    web1, web2 = RateLimiter(), RateLimiter()  # two web processes
    assert [web1.allow(('1.2.3.4', 'auth'), 3), web2.allow(('1.2.3.4', 'auth'), 3), web1.allow(('1.2.3.4', 'auth'), 3)] == [True] * 3
    assert web2.allow(('1.2.3.4', 'auth'), 3) is False and web1.allow(('1.2.3.4', 'auth'), 3) is False
    assert web2.allow(('5.6.7.8', 'auth'), 3) is True  # per key
    web1.clear()
    assert web2.allow(('1.2.3.4', 'auth'), 3) is True


def test_ranking_snapshot_round_trips_with_its_types(redis_url, monkeypatch):
    from datetime import datetime, timezone
    from decimal import Decimal
    shared = cache(redis_url, monkeypatch)
    entry = {'bucket': datetime(2026, 10, 5, 9, 0, 10, tzinfo=timezone.utc), 'written': 1.5,
             'payload': {'rows': [{'equity_usd': Decimal('101234.5600'), 'fx': {'rate': Decimal('1390.5')}, 'rank': 1}]}}
    assert shared.set_typed('ranking:snapshot', entry, 60)
    assert cache(redis_url, monkeypatch).get_typed('ranking:snapshot') == entry
