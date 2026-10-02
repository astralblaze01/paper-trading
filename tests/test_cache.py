"""The process cache: one slow load must not hold up other keys, and a key loads once at a time."""
import threading
import time

from app.cache import TTLCache


def test_a_slow_load_does_not_block_other_keys():
    # Regression: one lock covered every key during load(), so a 2.25 s US ranking
    # fetch held up quotes and charts that were already cached.
    cache = TTLCache()
    cache.get('quote', 60, lambda: 1)
    started, release = threading.Event(), threading.Event()
    def slow():
        started.set(); release.wait(5); return 'ranking'
    worker = threading.Thread(target=cache.get, args=('ranking', 60, slow)); worker.start()
    assert started.wait(5)
    began = time.monotonic()
    assert cache.get('quote', 60, lambda: 2) == 1          # a hit answers at once
    assert cache.get('other', 60, lambda: 3) == 3          # so does a miss on another key
    assert time.monotonic() - began < 1
    release.set(); worker.join(5)
    assert cache.get('ranking', 60, lambda: 'again') == 'ranking'


def test_concurrent_misses_on_one_key_load_once():
    cache, calls = TTLCache(), []
    def load():
        calls.append(1); time.sleep(.2); return 'value'
    results = []
    threads = [threading.Thread(target=lambda: results.append(cache.get('k', 60, load))) for _ in range(5)]
    for t in threads: t.start()
    for t in threads: t.join(5)
    assert results == ['value'] * 5 and len(calls) == 1


def test_stale_values_are_served_while_one_refresh_runs_behind():
    cache, calls = TTLCache(), []
    cache.get('rank', 0.05, lambda: 'old')
    time.sleep(.1)
    release = threading.Event()
    def load():
        calls.append(1); release.wait(5); return 'new'
    began = time.monotonic()
    assert cache.get('rank', 0.05, load, stale=10) == 'old'      # no wait for the provider
    assert cache.get('rank', 0.05, load, stale=10) == 'old'      # one refresh, not two
    assert time.monotonic() - began < 1
    release.set()
    for _ in range(50):
        if cache.get('rank', 60, lambda: 'unused') == 'new': break
        time.sleep(.05)
    assert cache.get('rank', 60, lambda: 'unused') == 'new' and len(calls) == 1
    # Past the stale window the caller waits for a fresh value, as before.
    time.sleep(.1)
    assert cache.get('rank', 0.05, lambda: 'fresh', stale=0.01) == 'fresh'


def test_a_failed_load_is_raised_and_not_cached():
    cache = TTLCache()
    def fail(): raise ValueError('provider down')
    try: cache.get('k', 60, fail)
    except ValueError: pass
    else: raise AssertionError('expected the error')
    assert cache.get('k', 60, lambda: 'ok') == 'ok'
