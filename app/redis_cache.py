import hashlib
import json
import os
import time
from decimal import Decimal
from uuid import uuid4
from .quote_data import normalize_quote

try:
    from redis import Redis
    from redis.exceptions import RedisError
except ImportError:  # Tests can still import the application before dependencies are rebuilt.
    Redis = None
    RedisError = Exception

# Redis names shared by the web, market-worker and market-stream processes.
QUOTE_UPDATES = 'market:quote:updates'          # pub/sub: a new snapshot version was stored
SUBSCRIPTIONS_KEY = 'market:subscriptions'      # zset: symbols the market-worker keeps fresh
STREAM_INTEREST_KEY = 'market:stream:interest'  # zset: symbols competing for trade-stream slots
STREAM_STATUS_KEY = 'market:stream:status'      # the trade stream's heartbeat and subscriptions
REFRESH_QUEUE_KEY = 'market:refresh'            # list: symbols without a snapshot, for the worker's next pass
REFRESH_COALESCE = 10  # seconds during which further refresh requests for a symbol are not queued


def price_key(symbol):
    return f'market:price:{symbol}'


def trade_key(symbol):
    return f'market:trade:{symbol}'


def refresh_request_key(symbol):
    return f'market:refresh-request:{symbol}'


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

    def store_quote(self, symbol, value, ttl):
        """WATCH + MULTI commits authoritative snapshot, durable version and notification.

        Metadata survives price expiry and worker restarts. A Redis reset creates
        a new epoch; stream snapshots explicitly reset the client's version gate.
        """
        if not self.client:
            return False
        try:
            quote = normalize_quote(symbol, value)
            ttl = max(1, int(ttl))
            key, meta_key = price_key(symbol), f'market:quote-version:{symbol}'
            from redis.exceptions import WatchError
            for _ in range(8):
                try:
                    with self.client.pipeline() as pipe:
                        pipe.watch(key, meta_key)
                        # GET also rejects unexpected Redis key types before MULTI.
                        old, meta = pipe.get(key), pipe.get(meta_key)
                        old = json.loads(old) if old else {}
                        meta = json.loads(meta) if meta else {}
                        previous_stamp = max(float(meta.get('timestamp', 0)), float(old.get('timestamp', 0)))
                        if float(quote['timestamp']) < previous_stamp:
                            return False
                        epoch = meta.get('epoch') or uuid4().hex
                        sequence = int(meta.get('sequence', 0)) + 1
                        version = f'{epoch}:{sequence}'
                        quote['_version'] = version
                        payload = json.dumps(quote, default=_json_default, allow_nan=False, separators=(',', ':'))
                        metadata = json.dumps({'epoch': epoch, 'sequence': sequence, 'timestamp': quote['timestamp']})
                        event = json.dumps({'schema_version': 1, 'symbol': symbol, 'version': version})
                        pipe.multi()
                        pipe.setex(key, ttl, payload)
                        pipe.set(meta_key, metadata)
                        pipe.publish(QUOTE_UPDATES, event)
                        pipe.execute()
                        return True
                except WatchError:
                    continue
        except (RedisError, ValueError, TypeError, KeyError):
            pass
        return False

    def delete(self, key):
        if not self.client:
            return False
        try:
            self.client.delete(key)
            return True
        except RedisError:
            return False

    def delete_if(self, key, matches):
        if not self.client:
            return False
        try:
            with self.client.pipeline() as pipe:
                # WATCH makes the delete fail if another process replaced the
                # value between the read and the delete.
                pipe.watch(key)
                value = pipe.get(key)
                if not value or not matches(json.loads(value)):
                    return False
                pipe.multi()
                pipe.delete(key)
                pipe.execute()
                return True
        except (RedisError, ValueError, TypeError, AttributeError):
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
            self.client.zadd(SUBSCRIPTIONS_KEY, {symbol: now})
            # Many browser polls can observe the same cache miss. Queue only one
            # immediate refresh per symbol during a short coalescing window.
            if force and self.client.set(refresh_request_key(symbol), '1', nx=True, ex=REFRESH_COALESCE):
                self.client.lpush(REFRESH_QUEUE_KEY, symbol)
        except RedisError:
            pass

    def request_stream(self, symbol):
        """Mark a symbol someone is looking at or ordering right now.

        The trade stream has very few slots, so only these requests (detail
        page, order preview, order) compete for them; portfolio valuation
        and rankings do not."""
        if not self.client:
            return
        try:
            self.client.zadd(STREAM_INTEREST_KEY, {symbol: time.time()})
        except RedisError:
            pass

    def stream_interest(self, active_seconds=150):
        """Recently requested stream symbols, most recent first."""
        if not self.client:
            return []
        try:
            now = time.time()
            self.client.zremrangebyscore(STREAM_INTEREST_KEY, 0, now - active_seconds)
            return self.client.zrevrangebyscore(STREAM_INTEREST_KEY, '+inf', now - active_seconds)
        except RedisError:
            return []

    def stream_status(self):
        return self.get_json(STREAM_STATUS_KEY)

    def requested_symbols(self, active_seconds=600):
        if not self.client:
            return []
        try:
            cutoff = time.time() - active_seconds
            self.client.zremrangebyscore(SUBSCRIPTIONS_KEY, 0, cutoff)
            return self.client.zrangebyscore(SUBSCRIPTIONS_KEY, cutoff, '+inf')
        except RedisError:
            return []

    def next_refresh(self, timeout=1):
        if not self.client:
            return None
        try:
            item = self.client.brpop(REFRESH_QUEUE_KEY, timeout=timeout)
            return item[1] if item else None
        except RedisError:
            return None


redis_cache = RedisCache()
