"""The one Finnhub WebSocket connection for US regular-session trades; prices go to Redis only.

Limits verified for the configured key on 2026-10-05, in the regular session:
- One connection per key. Opening a second connection dropped both, so web
  processes never connect. This worker holds a Redis leader lock and is the
  only client; nothing else may open a socket with this key.
- About 50 symbols per connection. Subscribing one by one, the 52nd was
  answered {"type": "error", "msg": "Subscribing to too many symbols"}, which
  does not name the symbol. Unsubscribing frees a slot. The default cap stays
  below the limit; an error lowers it for the rest of the connection.
- A trade arrives about 0.1 s after the print with symbol, price, millisecond
  time, size and condition codes, but no day change, high, low or volume.
  Those are carried over from the current snapshot (see day_fields).
- Pre-market prints are delivered too. Like Finnhub REST, this source is used
  for the regular session only, so each print's own New York time must fall in
  the regular session.

Slots go first to the symbols people are looking at or ordering right now
(market:stream:interest), then to the other US symbols the market-worker keeps
fresh. The market-worker refreshes streamed symbols over REST much less often.
"""
import asyncio
import json
import logging
import os
import random
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from .instruments import instrument, valid_symbol
from .logging_config import configure_logging
from .redis_cache import FINNHUB_STREAM_STATUS_KEY, price_key, redis_cache, trade_key
from .us_session import NEW_YORK, clock_session

log = logging.getLogger('finnhub-stream')
HEARTBEAT = Path('/tmp/finnhub-stream-heartbeat')
LEADER_KEY = 'market:finnhub-stream:leader'
LEADER_TTL = 30        # a dead leader frees the key's only connection after this
STATUS_TTL = 30        # the status disappears this long after writes stop
SILENCE = 150          # Finnhub pings about once a minute; a socket silent this long is dead
FLUSH = .5             # seconds between Redis writes of a symbol's latest trade
MANAGE = 2             # seconds between leadership, session and subscription checks
IDLE_POLL = 10         # seconds between checks while disabled, outside the session or on standby
STABLE_CONNECTION = 60 # a connection that lasted this long resets the reconnect backoff
TRADE_MAX_AGE = 86400  # seconds; a print must also fall in the regular session
CLOCK_SKEW = 60        # a print may be stamped this far ahead of our clock
TRADE_TTL = 43200      # market:trade:*, which the market-worker prefers over an older REST bar
SNAPSHOT_TTL = 120
PRICE_STEP = Decimal('.0001')
SESSIONS = ['regular']


def enabled():
    return os.getenv('FINNHUB_STREAM_ENABLED', 'true').lower() == 'true' and bool(os.getenv('FINNHUB_API_KEY'))


def trade_quote(trade, symbol, conn, reference=None, now=None):
    """A validated regular-session quote for one Finnhub trade, or None.

    `reference` (the last known price) guards against a misread or erroneous
    print: one more than 30% away is rejected."""
    now = time.time() if now is None else now
    try:
        if trade['s'] != symbol:
            return None
        price = Decimal(str(trade['p'])).quantize(PRICE_STEP)
        stamp = float(trade['t']) / 1000
        if not price.is_finite() or price <= 0 or not now - TRADE_MAX_AGE < stamp <= now + CLOCK_SKEW:
            return None
        if reference and abs(price / Decimal(str(reference)) - 1) > Decimal('0.3'):
            return None
    except (KeyError, TypeError, ValueError, InvalidOperation, ArithmeticError):
        return None
    if clock_session(datetime.fromtimestamp(stamp, timezone.utc)) not in SESSIONS:
        return None
    return instrument(symbol) | {
        'symbol': symbol, 'price': price, 'native_price': price, 'currency': 'USD',
        'fx_rate': Decimal(1), 'fx_date': None, 'timestamp': int(stamp), 'stale': False,
        'change': None, 'change_pct': None, 'high': None, 'low': None, 'volume': None,
        'source': 'Finnhub', 'origin': 'stream', 'stream': 'finnhub', 'market': 'US', 'venue': 'US',
        'valid_sessions': SESSIONS, 'stream_conn': conn, 'received_at': now, '_cached_at': now,
        'data_status': 'Finnhub 실시간 체결 · 정규장'}


def _number(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def day_fields(q, snapshot, high=None, low=None):
    """`q` with day change, high, low and volume carried over from `snapshot`.

    A trade carries only its price. The previous close is the snapshot's price
    minus its change, which holds for every source (Finnhub REST, KIS and this
    stream), but only when the snapshot is from the same New York trading day."""
    q = dict(q)
    if not snapshot or not snapshot.get('timestamp'):
        return q
    day = lambda stamp: datetime.fromtimestamp(float(stamp), NEW_YORK).date()
    if day(snapshot['timestamp']) != day(q['timestamp']):
        return q
    price = q['native_price']
    base, change = _number(snapshot.get('native_price', snapshot.get('price'))), _number(snapshot.get('change'))
    if base is not None and change is not None and base - change > 0:
        close = base - change
        q['change'] = (price - close).quantize(PRICE_STEP)
        q['change_pct'] = (q['change'] / close * 100).quantize(PRICE_STEP)
    highs = [v for v in (_number(snapshot.get('high')), _number(q.get('high')), high, price) if v is not None]
    lows = [v for v in (_number(snapshot.get('low')), _number(q.get('low')), low, price) if v is not None]
    q['high'], q['low'] = max(highs), min(lows)
    if q.get('volume') is None:
        q['volume'] = snapshot.get('volume')
    return q


class FinnhubStream:
    def __init__(self, key, cache=redis_cache):
        self.key, self.cache = key, cache
        self.limit = int(os.getenv('FINNHUB_STREAM_MAX_SUBSCRIPTIONS', '50'))
        self.token = uuid4().hex
        self.state, self.reconnects, self.last_error = 'starting', 0, None
        self.reset()

    def reset(self):
        self.conn = None
        self.active = []     # subscribed symbols, in subscription order
        self.latest = {}     # symbol -> last validated trade quote
        self.extremes = {}   # symbol -> (high, low) of this connection's prints
        self.dirty = set()   # symbols whose latest trade is not in Redis yet
        self.queued = []
        self.last_message = None
        self.effective_limit = self.limit

    # Redis --------------------------------------------------------------

    def lead(self):
        client = self.cache.client
        if not client:
            return False
        if client.set(LEADER_KEY, self.token, nx=True, ex=LEADER_TTL) or client.get(LEADER_KEY) == self.token:
            client.expire(LEADER_KEY, LEADER_TTL)
            return True
        return False

    def write_status(self, connected):
        self.cache.set_json(FINNHUB_STREAM_STATUS_KEY, {
            'state': self.state, 'connected': connected, 'conn': self.conn if connected else None,
            'heartbeat': time.time(), 'last_message': self.last_message,
            'subscribed': list(self.active), 'limit': self.effective_limit,
            'queued': self.queued, 'reconnects': self.reconnects, 'last_error': self.last_error}, STATUS_TTL)

    def wanted(self):
        """US symbols in slot order: being viewed or ordered, then kept fresh by the worker."""
        interest = self.cache.stream_interest()
        requested = list(reversed(self.cache.requested_symbols()))  # most recently requested first
        us = lambda symbols: [s for s in symbols if valid_symbol(s) and not s.startswith('KR:')]
        return list(dict.fromkeys(us(interest))), list(dict.fromkeys(us(requested)))

    def flush(self):
        """Write each changed symbol's latest trade, with day fields from its snapshot."""
        symbols = [s for s in self.dirty if s in self.latest]
        self.dirty.clear()
        for symbol in symbols:
            high, low = self.extremes.get(symbol, (None, None))
            q = day_fields(self.latest[symbol], self.cache.get_json(price_key(symbol)), high, low)
            self.cache.set_json(trade_key(symbol), q, TRADE_TTL)
            self.cache.store_quote(symbol, q, SNAPSHOT_TTL)

    # Subscriptions ------------------------------------------------------

    async def reconcile(self, ws, interest=None, requested=None):
        if interest is None:
            interest, requested = await asyncio.to_thread(self.wanted)
        requested = requested or []
        # Viewed symbols always get a slot; then held slots stay put, so the
        # worker's set does not churn subscriptions; then the rest.
        order = interest + [s for s in self.active if s in requested] + requested
        want = list(dict.fromkeys(order))[:self.effective_limit]
        self.queued = [s for s in dict.fromkeys(interest + requested) if s not in want][:20]
        for symbol in [s for s in self.active if s not in want]:
            await ws.send(json.dumps({'type': 'unsubscribe', 'symbol': symbol}))
            self.active.remove(symbol)
            self.latest.pop(symbol, None)
            self.extremes.pop(symbol, None)
        for symbol in [s for s in want if s not in self.active]:
            await ws.send(json.dumps({'type': 'subscribe', 'symbol': symbol}))
            self.active.append(symbol)

    async def handle(self, ws, raw):
        try:
            message = json.loads(raw)
        except ValueError:
            return
        kind = message.get('type')
        if kind == 'trade':
            self._on_trades(message.get('data') or [])
        elif kind == 'error':
            text = str(message.get('msg', ''))
            if 'too many symbols' in text.lower() and self.active:
                # The refusal does not name the symbol; the last one asked for is the one over.
                refused = self.active.pop()
                await ws.send(json.dumps({'type': 'unsubscribe', 'symbol': refused}))
                self.effective_limit = len(self.active)
                log.warning('subscription limit reached at %s symbols', self.effective_limit)
            else:
                self.last_error = text[:120] or 'error'
                log.warning('finnhub stream error message', extra={'status_code': self.last_error})

    def _on_trades(self, trades):
        now = time.time()
        for trade in trades:
            symbol = trade.get('s') if isinstance(trade, dict) else None
            if symbol not in self.active:
                continue
            reference = (self.latest.get(symbol) or {}).get('native_price')
            q = trade_quote(trade, symbol, self.conn, reference, now)
            if not q:
                continue
            last = self.latest.get(symbol)
            if last and q['timestamp'] < last['timestamp']:
                continue
            high, low = self.extremes.get(symbol, (q['native_price'], q['native_price']))
            self.extremes[symbol] = (max(high, q['native_price']), min(low, q['native_price']))
            self.latest[symbol] = q
            self.dirty.add(symbol)

    # Connection ---------------------------------------------------------

    async def manage(self, ws):
        checked = 0.0
        while True:
            HEARTBEAT.touch()
            if time.monotonic() - checked >= MANAGE:
                checked = time.monotonic()
                if not await asyncio.to_thread(self.lead):
                    log.warning('finnhub stream leadership lost')
                    await ws.close()
                    return
                if clock_session() not in SESSIONS:
                    log.info('regular session over; closing the finnhub stream')
                    await ws.close()
                    return
                if time.time() - (self.last_message or 0) > SILENCE:
                    log.warning('finnhub stream silent; reconnecting')
                    await ws.close()
                    return
                await self.reconcile(ws)
                await asyncio.to_thread(self.write_status, True)
            await asyncio.to_thread(self.flush)
            await asyncio.sleep(FLUSH)

    async def connect(self):
        from websockets.asyncio.client import connect
        url = os.getenv('FINNHUB_WS_URL', 'wss://ws.finnhub.io') + '?token=' + self.key
        async with connect(url, open_timeout=10, close_timeout=3, max_size=2 ** 20) as ws:
            self.conn, self.state, self.last_message = uuid4().hex, 'connected', time.time()
            log.info('finnhub stream connected')
            manager = asyncio.create_task(self.manage(ws))
            try:
                async for message in ws:
                    self.last_message = time.time()
                    await self.handle(ws, message)
                    if manager.done():
                        break
            finally:
                manager.cancel()
                await asyncio.gather(manager, return_exceptions=True)
        if manager.done() and not manager.cancelled() and manager.exception():
            raise manager.exception()

    async def idle(self, state):
        self.state = state
        await asyncio.to_thread(self.write_status, False)
        await asyncio.sleep(IDLE_POLL)

    async def run(self):
        maximum = int(os.getenv('FINNHUB_STREAM_RECONNECT_MAX_SECONDS', '60'))
        delay = 1
        while True:
            HEARTBEAT.touch()
            if not enabled():
                await self.idle('disabled')
                continue
            if clock_session() not in SESSIONS:
                await self.idle('outside_session')
                continue
            try:
                leader = await asyncio.to_thread(self.lead)
            except Exception:
                leader = False
            if not leader:
                # Another instance holds the only connection the key allows.
                self.state = 'standby'
                await asyncio.sleep(IDLE_POLL)
                continue
            started = time.monotonic()
            try:
                await self.connect()
                self.last_error = None
            except Exception as exc:  # network, auth, protocol: never fatal
                self.last_error = type(exc).__name__
                log.warning('finnhub stream error', extra={'status_code': type(exc).__name__})
            if time.monotonic() - started > STABLE_CONNECTION:
                delay = 1
            self.reset()
            if clock_session() not in SESSIONS:
                continue  # closed at the end of the session, not a failure
            self.reconnects += 1
            self.state = 'reconnecting'
            try:
                await asyncio.to_thread(self.write_status, False)
            except Exception:
                pass
            log.info('finnhub stream reconnecting in %ss', delay)
            await asyncio.sleep(delay * random.uniform(.8, 1.2))
            delay = min(maximum, delay * 2)


def main():
    configure_logging()
    asyncio.run(FinnhubStream(os.getenv('FINNHUB_API_KEY', '')).run())


if __name__ == '__main__':
    main()
