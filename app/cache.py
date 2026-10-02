"""Bounded process cache: one web worker shares results across all browsers/jobs.

Each key loads under its own lock, so a slow provider call (a 2 s ranking) never
holds up other keys, and concurrent misses on one key load it once. With `stale`,
an expired value younger than ttl + stale is returned at once while a single
background thread refreshes it."""
import logging
import threading
import time
from threading import Lock, RLock

log = logging.getLogger('cache')


class TTLCache:
    def __init__(self, limit=2048):
        self.values = {}; self.lock = Lock(); self.limit = limit
        self.loading = {}      # key -> RLock held while that key loads (re-entrant for nested lookups)
        self.refreshing = set()

    def _store(self, key, result):
        with self.lock:
            if len(self.values) >= self.limit: self.values.clear(); self.loading.clear()
            self.values[key] = (time.monotonic(), result)

    def _refresh(self, key, load):
        try: self._store(key, load())
        except Exception as exc:  # the stale value stays until a caller has to wait for a fresh one
            log.warning('background cache refresh failed', extra={'path': str(key), 'status_code': type(exc).__name__})
        finally:
            with self.lock: self.refreshing.discard(key)

    def get(self, key, ttl, load, stale=0):
        with self.lock:
            item = self.values.get(key)
            age = time.monotonic() - item[0] if item else None
            if item and age < ttl: return item[1]
            if item and age < ttl + stale:
                if key not in self.refreshing:
                    self.refreshing.add(key)
                    threading.Thread(target=self._refresh, args=(key, load), daemon=True).start()
                return item[1]
            key_lock = self.loading.setdefault(key, RLock())
        with key_lock:
            with self.lock:  # another caller may have loaded it while this one waited
                item = self.values.get(key)
                if item and time.monotonic() - item[0] < ttl: return item[1]
            result = load()
            self._store(key, result)
            return result
