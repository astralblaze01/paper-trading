"""One asynchronous Redis subscriber per process; bounded latest-state fan-out."""
import asyncio
import anyio
from collections import Counter
from contextlib import suppress
import hashlib
import json
import logging
import os
import re
import time
from uuid import uuid4

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from redis.asyncio import Redis
from redis.exceptions import RedisError
from itsdangerous import TimestampSigner, BadSignature
from .instruments import valid_symbol
from .quote_data import public_quote
from .redis_cache import (QUOTE_UPDATES, REFRESH_COALESCE, REFRESH_QUEUE_KEY, STREAM_INTEREST_KEY, SUBSCRIPTIONS_KEY,
                          price_key, refresh_request_key)

log = logging.getLogger('market-stream')
VERSION = re.compile(r'^[a-f0-9]{32}:[1-9][0-9]*$')


def enabled():
    return (os.getenv('QUOTE_SSE_ENABLED', 'false').lower() == 'true'
            and os.getenv('MARKET_CACHE_MODE', 'direct').lower() == 'worker'
            and bool(os.getenv('REDIS_URL')))


def heartbeat_seconds():
    """Stream check period; a lease lives four of them, so it survives missed renewals."""
    return max(5, min(30, int(os.getenv('QUOTE_SSE_HEARTBEAT', '15'))))


def newer(version, previous):
    if not previous:
        return True
    epoch, sequence = version.split(':')
    old_epoch, old_sequence = previous.split(':')
    return epoch == old_epoch and int(sequence) > int(old_sequence)


def version_gate(name, version, previous):
    """Name to send a snapshot/quote under after version `previous`, or None to drop it.

    Within an epoch a client only moves forward: older versions and a repeated
    quote are dropped, a repeated snapshot is sent again. Another epoch means
    Redis was reset, so that payload always passes, as a snapshot."""
    if not previous:
        return name
    same_epoch = version.split(':')[0] == previous.split(':')[0]
    if same_epoch and version != previous and not newer(version, previous):
        return None
    if name == 'quote' and version == previous:
        return None
    if not same_epoch:
        # Payload was re-read from authoritative Redis, never replayed.
        return 'snapshot'
    return name


def event(name, data):
    identifier = f"id: {data['version']}\n" if 'version' in data else ''
    return f'{identifier}event: {name}\ndata: {json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":"))}\n\n'


# Atomic shared user/IP admission + renewable TTL leases. Failure is closed.
LEASE = """
local now = tonumber(ARGV[1])
for i=1,2 do
  local t = redis.call('TYPE', KEYS[i]).ok
  if t ~= 'none' and t ~= 'zset' then return -1 end
end
for i=1,2 do redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', now) end
if ARGV[6] == 'renew' then
  for i=1,2 do if not redis.call('ZSCORE', KEYS[i], ARGV[2]) then return 0 end end
else
  if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[4]) or
     redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[5]) then return 0 end
end
for i=1,2 do
  redis.call('ZADD', KEYS[i], now + tonumber(ARGV[3]), ARGV[2])
  redis.call('EXPIRE', KEYS[i], tonumber(ARGV[3]) * 2)
end
return 1
"""
ATTEMPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], 60) end
return count
"""


class QuoteHub:
    def __init__(self, url=None, assess=None):
        self.url = url or os.getenv('REDIS_URL', '')
        # Session tradeability depends on the time of reading, not of writing.
        self.assess = assess
        self.redis = None
        self.listeners = {}
        self.ready = asyncio.Event()
        self.tasks = []
        self.metrics = Counter()

    async def start(self):
        self.redis = Redis.from_url(self.url, decode_responses=True, socket_connect_timeout=2, socket_timeout=5, health_check_interval=15)
        self.tasks = [asyncio.create_task(self._subscriber()), asyncio.create_task(self._keep_interest())]

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        self.ready.clear()
        for symbol in list(self.listeners):
            self.fanout(symbol, ('status', {'state': 'restart'}))
        if self.redis:
            await self.redis.aclose()
        self.listeners.clear()

    def listen(self, symbol):
        queue = asyncio.Queue(maxsize=1)
        self.listeners.setdefault(symbol, set()).add(queue)
        self.metrics['active'] += 1
        return queue

    def unlisten(self, symbol, queue):
        queues = self.listeners.get(symbol)
        if queues and queue in queues:
            queues.remove(queue)
            self.metrics['active'] -= 1
            if not queues:
                del self.listeners[symbol]

    def fanout(self, symbol, item):
        for queue in tuple(self.listeners.get(symbol, ())):
            if queue.full():
                queue.get_nowait()
                self.metrics['coalesced'] += 1
            queue.put_nowait(item)

    async def interest(self, symbol, missing=False):
        """Async twin of RedisCache.request_quote(symbol, force=missing) plus request_stream."""
        await self.redis.zadd(SUBSCRIPTIONS_KEY, {symbol: time.time()})
        await self.redis.zadd(STREAM_INTEREST_KEY, {symbol: time.time()})
        if missing and await self.redis.set(refresh_request_key(symbol), '1', nx=True, ex=REFRESH_COALESCE):
            await self.redis.lpush(REFRESH_QUEUE_KEY, symbol)

    async def snapshot(self, symbol):
        raw = await self.redis.get(price_key(symbol))
        if not raw:
            return None
        try:
            q = json.loads(raw)
            version = q.get('_version', '')
            # Pre-upgrade snapshots have no version: wait for worker publication.
            if not VERSION.fullmatch(version):
                return None
            quote = await asyncio.to_thread(public_quote, symbol, q, self.assess) if self.assess else public_quote(symbol, q)
            return {'schema_version': 1, 'symbol': symbol, 'version': version, 'quote': quote}
        except (ValueError, TypeError, KeyError, AttributeError):
            self.metrics['invalid'] += 1
            return None

    async def _keep_interest(self):
        while True:
            try:
                for symbol in list(self.listeners):
                    await self.interest(symbol)
            except RedisError:
                self.metrics['interest_errors'] += 1
            log.info('quote hub metrics %s', dict(self.metrics) | {'subscriber_connected': self.ready.is_set()})
            await asyncio.sleep(60)

    async def _subscriber(self):
        delay = 1
        while True:
            try:
                async with self.redis.pubsub() as sub:
                    await sub.subscribe(QUOTE_UPDATES)
                    # Wait for the subscription ACK; sending SUBSCRIBE isn't enough.
                    while True:
                        ack = await sub.get_message(timeout=3)
                        if ack and ack['type'] == 'subscribe':
                            break
                    self.ready.set()
                    self.metrics['reconnects'] += 1
                    log.info('quote subscriber connected')
                    # Also runs on redis-py's automatic reconnect subscription ACK.
                    await self._resnapshot()
                    delay = 1
                    while True:
                        message = await sub.get_message(timeout=1)
                        if not message:
                            continue
                        if message['type'] == 'subscribe':
                            await self._resnapshot()
                        elif message['type'] == 'message':
                            await self._update(message['data'])
            except (RedisError, OSError):
                self.ready.clear()
                self.metrics['subscriber_errors'] += 1
                log.warning('quote subscriber unavailable')
                for symbol in list(self.listeners):
                    self.fanout(symbol, ('status', {'state': 'unavailable'}))
                await asyncio.sleep(delay)
                delay = min(30, delay * 2)

    async def _resnapshot(self):
        for symbol in list(self.listeners):
            payload = await self.snapshot(symbol)
            self.fanout(symbol, ('snapshot', payload) if payload else ('status', {'state': 'waiting'}))
            await self.interest(symbol, missing=payload is None)

    async def _update(self, raw):
        try:
            message = json.loads(raw)
            symbol, version = message['symbol'], message['version']
            if message.get('schema_version') != 1 or not isinstance(symbol, str) or not isinstance(version, str) or not valid_symbol(symbol) or not VERSION.fullmatch(version):
                raise ValueError()
            if symbol not in self.listeners:
                return
            payload = await self.snapshot(symbol)
            if payload:
                self.fanout(symbol, ('quote', payload))
                self.metrics['updates'] += 1
        except (ValueError, TypeError, KeyError, AttributeError):
            self.metrics['invalid'] += 1

    async def lease(self, keys, token, renew=False):
        heartbeat = heartbeat_seconds()
        return await self.redis.eval(LEASE, 2, *keys, time.time(), token, heartbeat * 4,
                                     int(os.getenv('QUOTE_SSE_USER_LIMIT', '5')),
                                     int(os.getenv('QUOTE_SSE_IP_LIMIT', '30')), 'renew' if renew else 'new') == 1

    async def release(self, keys, token):
        with suppress(RedisError):
            async with self.redis.pipeline(transaction=True) as pipe:
                for key in keys:
                    pipe.zrem(key, token)
                await pipe.execute()

    def _validate(self, request, symbol):
        """Refusals decided without Redis: feature gate, symbol, then Origin."""
        if not enabled() or not self.redis:
            raise HTTPException(503, '시세 스트림이 비활성 상태입니다.')
        if not valid_symbol(symbol):
            raise HTTPException(422, '잘못된 종목 코드입니다.')
        origin = request.headers.get('origin')
        if origin and origin.split('://', 1)[-1] != request.headers.get('host'):
            raise HTTPException(403, '허용되지 않은 출처입니다.')

    async def _admit(self, request, uid):
        """Count the attempt, lease a user/IP slot and wait for the subscriber; returns (keys, token)."""
        ip = request.headers.get('x-real-ip') or (request.client.host if request.client else 'unknown')
        ip = hashlib.sha256(ip.encode()).hexdigest()
        keys = [f'market:sse:user:{uid}', f'market:sse:ip:{ip}']
        token = uuid4().hex
        try:
            for identity, limit in ((f'user:{uid}', 30), (f'ip:{ip}', 120)):
                if await self.redis.eval(ATTEMPT, 1, f'market:sse:attempt:{identity}') > limit:
                    raise HTTPException(429, '연결 시도가 너무 많습니다.', headers={'Retry-After': '60'})
            if not await self.lease(keys, token):
                raise HTTPException(429, '시세 연결 수 제한입니다.', headers={'Retry-After': '60'})
            try:
                await asyncio.wait_for(self.ready.wait(), 3)
            except BaseException:
                await self.release(keys, token)
                raise
        except (RedisError, asyncio.TimeoutError) as exc:
            raise HTTPException(503, '시세 연결을 준비 중입니다.') from exc
        return keys, token

    async def response(self, request, symbol, uid, authenticate):
        self._validate(request, symbol)
        keys, token = await self._admit(request, uid)
        queue = self.listen(symbol)
        heartbeat = heartbeat_seconds()
        cookie = request.cookies.get('paper_session', '')
        signer = TimestampSigner(os.environ['SESSION_SECRET'])

        async def cleanup():
            self.unlisten(symbol, queue)
            # ASGI disconnect cancellation must not skip Redis lease release.
            with anyio.move_on_after(3, shield=True):
                await self.release(keys, token)

        async def stream():
            previous = None
            started = time.monotonic()
            next_check = started + heartbeat
            try:
                payload = await self.snapshot(symbol)
                await self.interest(symbol, missing=payload is None)
                if payload:
                    previous = payload['version']
                    yield event('snapshot', payload)
                else:
                    yield event('status', {'state': 'waiting'})
                while True:
                    # Timed checks cannot be starved by a busy quote queue.
                    if time.monotonic() >= next_check:
                        try:
                            signer.unsign(cookie, max_age=43200)
                            await asyncio.to_thread(authenticate, request)
                        except (BadSignature, HTTPException):
                            yield event('status', {'state': 'auth_required'})
                            return
                        if time.monotonic() - started >= 1800:
                            yield event('status', {'state': 'restart'})
                            return
                        if not await self.lease(keys, token, renew=True):
                            yield event('status', {'state': 'unavailable'})
                            return
                        yield event('heartbeat', {'time': time.time(), 'connected': self.ready.is_set()})
                        next_check = time.monotonic() + heartbeat
                    try:
                        name, data = await asyncio.wait_for(queue.get(), max(.01, next_check - time.monotonic()))
                    except asyncio.TimeoutError:
                        continue
                    if name in ('snapshot', 'quote'):
                        name = version_gate(name, data['version'], previous)
                        if name is None:
                            continue
                        previous = data['version']
                    yield event(name, data)
                    if name == 'status' and data['state'] == 'restart':
                        return
            except (RedisError, ValueError):
                yield event('status', {'state': 'unavailable'})
            finally:
                await cleanup()
        return StreamingResponse(stream(), background=BackgroundTask(cleanup), media_type='text/event-stream', headers={
            'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})
