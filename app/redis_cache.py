import hashlib
import json
import os
import time
from decimal import Decimal

try:
    from redis import Redis
    from redis.exceptions import RedisError
except ImportError:  # Tests can still import the application before dependencies are rebuilt.
    Redis = None
    RedisError = Exception


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f'Unsupported cache value: {type(value)!r}')


class RedisCache:
    def __init__(self):
        self.url = os.getenv('REDIS_URL', '')
        self.client = Redis.from_url(self.url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2) if self.url and Redis else None

    @property
    def configured(self):
        return self.client is not None

    def ping(self):
        try:
            return bool(self.client and self.client.ping())
        except RedisError:
            return False

    def key(self, namespace, value):
        digest = hashlib.sha256(value.encode()).hexdigest()
        return f'{namespace}:{digest}'

    def get_json(self, key):
        if not self.client:
            return None
        try:
            value = self.client.get(key)
            return json.loads(value) if value else None
        except (RedisError, ValueError, TypeError):
            return None

    def set_json(self, key, value, ttl):
        if not self.client:
            return False
        try:
            self.client.setex(key, max(1, int(ttl)), json.dumps(value, default=_json_default, separators=(',', ':')))
            return True
        except (RedisError, TypeError):
            return False

    def delete(self, key):
        if not self.client:
            return False
        try:
            self.client.delete(key)
            return True
        except RedisError:
            return False

    def get_or_load(self, key, ttl, load):
        cached = self.get_json(key)
        if cached is not None:
            return cached
        if not self.client:
            return load()
        lock = self.client.lock(key + ':lock', timeout=15, blocking_timeout=3)
        try:
            with lock:
                cached = self.get_json(key)
                if cached is not None:
                    return cached
                value = load()
                self.set_json(key, value, ttl)
                return value
        except RedisError:
            return load()

    def allow_rate(self, name, limit, seconds=60):
        if not self.client:
            return True
        bucket = int(time.time()) // seconds
        key = f'rate:{name}:{bucket}'
        try:
            with self.client.pipeline() as pipe:
                pipe.incr(key)
                pipe.expire(key, seconds * 2)
                count, _ = pipe.execute()
            return int(count) <= int(limit)
        except RedisError:
            return True

    def request_quote(self, symbol, force=False):
        if not self.client:
            return
        try:
            now = time.time()
            self.client.zadd('market:subscriptions', {symbol: now})
            # Many browser polls can observe the same cache miss. Queue only one
            # immediate refresh per symbol during a short coalescing window.
            if force and self.client.set(f'market:refresh-request:{symbol}', '1', nx=True, ex=10):
                self.client.lpush('market:refresh', symbol)
        except RedisError:
            pass

    def requested_symbols(self, active_seconds=600):
        if not self.client:
            return []
        try:
            cutoff = time.time() - active_seconds
            self.client.zremrangebyscore('market:subscriptions', 0, cutoff)
            return self.client.zrangebyscore('market:subscriptions', cutoff, '+inf')
        except RedisError:
            return []

    def next_refresh(self, timeout=1):
        if not self.client:
            return None
        try:
            item = self.client.brpop('market:refresh', timeout=timeout)
            return item[1] if item else None
        except RedisError:
            return None


redis_cache = RedisCache()
