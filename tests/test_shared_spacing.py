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


class FakeKIS:
    """KIS over HTTP: each answer takes 0.3 s; records when every quotation call started."""
    def __init__(self): self.starts = []
    def post(self, path, json=None):
        return Answer({'access_token': 't', 'expires_in': 86400})
    def get(self, path, headers=None, params=None):
        self.starts.append((path.split('/')[2], time.monotonic())); time.sleep(.3)
        return Answer({'rt_cd': '0', 'output': {}})


class Answer:
    status_code, headers = 200, {}
    def __init__(self, body): self.body = body
    def json(self): return self.body
    def raise_for_status(self): pass


def test_a_domestic_call_does_not_wait_behind_overseas_calls(monkeypatch):
    """One lock covered both kinds of call, through the 1.1 s overseas spacing and the request
    itself: live, a 0.18 s domestic quote took 1.25 s while two overseas calls ran."""
    import threading
    from app.multi_market import KoreaPrices
    monkeypatch.setenv('KIS_APP_KEY', 'k'); monkeypatch.setenv('KIS_APP_SECRET', 's')
    kis = KoreaPrices(); kis.client = FakeKIS()
    overseas = lambda n: kis.get('/uapi/overseas-price/v1/quotations/price', 'T', {'n': n}, 0)
    domestic = lambda n: kis.get('/uapi/domestic-stock/v1/quotations/inquire-price', 'T', {'n': n}, 0)
    # The domestic call arrives while the second overseas call waits out its 1.1 s spacing.
    us = threading.Thread(target=lambda: [overseas(1), overseas(2)]); us.start(); time.sleep(.45)
    started = time.monotonic(); domestic(1); took = time.monotonic() - started
    us.join(); domestic(2)
    assert took < .6, took                                   # its own request only
    by = lambda kind: [t for k, t in kis.client.starts if k == kind]
    assert by('overseas-price')[1] - by('overseas-price')[0] >= 1.1    # each kind keeps its spacing
    assert by('domestic-stock')[1] - by('domestic-stock')[0] >= .2
