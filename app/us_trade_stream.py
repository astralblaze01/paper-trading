"""The one KIS WebSocket connection for US trades; prices go to Redis only.

Limits verified for the configured app key (scripts/check_us_day_market.py):
- One WebSocket session per app key. A second connection is refused with
  'ALREADY IN USE appkey', so web processes never connect. This worker holds a
  Redis leader lock and is the only client.
- Three registrations per session; more are refused with OPSP0008
  'MAX SUBSCRIBE OVER'. Unsubscribing frees a slot. US_STREAM_MAX_SUBSCRIPTIONS
  sets the cap, and a refusal lowers it for the rest of the connection.
- tr_id HDFSCNT0 with tr_key 'D' + NAS/NYS/AMS + symbol for the primary
  exchange and 'R' + BAQ/BAY/BAA + symbol for the day market.

Slots go to the symbols people are looking at or ordering right now
(market:stream:interest). Every other symbol is served by REST in the
market-worker. Browsers never see this connection.
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from .instruments import instrument, valid_symbol
from .logging_config import configure_logging
from .market import MarketError
from .redis_cache import redis_cache
from .us_quotes import DAY_VENUE, PRIMARY_SESSIONS, USQuotes
from .us_session import NEW_YORK, clock_session

log = logging.getLogger('us-trade-stream')
FIELDS = ('RSYM', 'SYMB', 'ZDIV', 'TYMD', 'XYMD', 'XHMS', 'KYMD', 'KHMS', 'OPEN', 'HIGH', 'LOW', 'LAST',
          'SIGN', 'DIFF', 'RATE', 'PBID', 'PASK', 'VBID', 'VASK', 'EVOL', 'TVOL', 'TAMT', 'BIVL', 'ASVL',
          'STRN', 'MTYP')
HEARTBEAT = Path('/tmp/us-trade-stream-heartbeat')
STATUS_KEY = 'market:stream:status'
LEADER_KEY = 'market:stream:leader'
MIN_HOLD = 30     # seconds a subscription is kept before another symbol may take the slot
SILENCE = 150     # KIS sends PINGPONG regularly; a socket silent this long is dead
ACK_TIMEOUT = 10  # an unanswered (un)subscribe request is forgotten and retried
KEEPALIVE = 30    # republish the last trade so a quiet symbol's snapshot never expires


class StreamRejected(Exception):
    pass


def enabled():
    return os.getenv('US_TRADE_STREAM_ENABLED', 'true').lower() == 'true'


def stream_key(symbol, exchange, session):
    if session == 'overnight':
        return 'R' + DAY_VENUE[exchange] + symbol
    if session in PRIMARY_SESSIONS:
        return 'D' + exchange + symbol
    return None


def parse_trades(message):
    """'0|HDFSCNT0|002|f^f^...' holds `count` records of equal width."""
    try:
        _, tr_id, count, body = message.split('|', 3)
        count = int(count)
    except ValueError:
        return []
    values = body.split('^')
    if tr_id != 'HDFSCNT0' or count <= 0 or len(values) // count < len(FIELDS):
        return []
    width = len(values) // count
    return [dict(zip(FIELDS, values[i * width:(i + 1) * width])) for i in range(count)]


def trade_quote(fields, symbol, key, conn, now=None):
    """A validated quote for one trade, or None.

    The trade must belong to the keyed subscription and its own New York time
    must fall in a session that venue serves. A primary-exchange print can
    therefore never become a day-market price, or the reverse."""
    now = time.time() if now is None else now
    try:
        if fields['RSYM'] != key or fields['SYMB'] != symbol:
            return None
        stamp = datetime.strptime(fields['XYMD'] + fields['XHMS'], '%Y%m%d%H%M%S').replace(tzinfo=NEW_YORK).timestamp()
        price = Decimal(fields['LAST']).quantize(Decimal('.0001'))
        diff, rate = Decimal(fields['DIFF']), Decimal(fields['RATE'])
        if not price.is_finite() or price <= 0 or not now - 86400 < stamp <= now + 60:
            return None
    except (KeyError, ValueError, InvalidOperation):
        return None
    overnight = key.startswith('R')
    sessions = ['overnight'] if overnight else PRIMARY_SESSIONS
    if clock_session(datetime.fromtimestamp(stamp, timezone.utc)) not in sessions:
        return None
    if fields.get('SIGN') in ('4', '5'):  # KIS sends the difference unsigned
        diff = -abs(diff)
    return instrument(symbol) | {
        'symbol': symbol, 'price': price, 'native_price': price, 'currency': 'USD',
        'fx_rate': Decimal(1), 'fx_date': None, 'timestamp': int(stamp), 'stale': False,
        'change': diff, 'change_pct': rate, 'high': fields.get('HIGH') or None, 'low': fields.get('LOW') or None,
        'volume': fields.get('TVOL') or None, 'source': 'KIS', 'origin': 'stream',
        'venue': 'overnight' if overnight else 'primary', 'exchange': key[1:4], 'valid_sessions': sessions,
        'stream_conn': conn, 'received_at': now, '_cached_at': now,
        'data_status': f"KIS {key[1:4]} 실시간 체결{' · 데이마켓' if overnight else ''}"}


class TradeStream:
    def __init__(self, kis, cache=redis_cache):
        self.kis, self.cache = kis, cache
        self.lookup = USQuotes(None, kis)
        self.limit = int(os.getenv('US_STREAM_MAX_SUBSCRIPTIONS', '3'))
        self.token = uuid4().hex
        self.state, self.reconnects, self.last_error = 'starting', 0, None
        self.no_exchange = {}
        self.reset()

    def reset(self):
        self.conn = None
        self.active = {}     # key -> symbol, confirmed by KIS
        self.since = {}      # symbol -> monotonic time subscribed
        self.sent = {}       # key -> (symbol, 'subscribe'|'unsubscribe', monotonic)
        self.latest = {}     # symbol -> last validated trade quote
        self.published = {}  # symbol -> (monotonic, timestamp) of the last market:price write
        self.queued = []
        self.last_message = None
        self.effective_limit = self.limit

    # Redis --------------------------------------------------------------

    def lead(self):
        client = self.cache.client
        if not client:
            return False
        if client.set(LEADER_KEY, self.token, nx=True, ex=30) or client.get(LEADER_KEY) == self.token:
            client.expire(LEADER_KEY, 30)
            return True
        return False

    def write_status(self, connected):
        self.cache.set_json(STATUS_KEY, {
            'state': self.state, 'connected': connected, 'conn': self.conn if connected else None,
            'heartbeat': time.time(), 'last_message': self.last_message,
            'subscribed': sorted(self.active.values()), 'limit': self.effective_limit,
            'queued': self.queued, 'reconnects': self.reconnects, 'last_error': self.last_error}, 30)

    def approval(self):
        def issue():
            response = self.kis.client.post('/oauth2/Approval', json={
                'grant_type': 'client_credentials', 'appkey': self.kis.key, 'secretkey': self.kis.secret})
            response.raise_for_status()
            key = response.json().get('approval_key')
            if not key:
                raise StreamRejected('approval key refused')
            return {'approval_key': key}
        return self.cache.get_or_load(self.cache.key('market:kis:ws-approval', self.kis.key + '\0' + self.kis.secret),
                                      43200, issue)['approval_key']

    def store(self, symbol, q):
        self.cache.set_json(f'market:trade:{symbol}', q, 43200)
        self.latest[symbol] = q
        at, _ = self.published.get(symbol, (0, 0))
        # Busy symbols print many times a second; the snapshot and its SSE
        # notification are limited to two per second. flush() sends the rest.
        if time.monotonic() - at >= .5:
            self.publish(symbol, q)

    def publish(self, symbol, q):
        if self.cache.store_quote(symbol, q, 120):
            self.published[symbol] = (time.monotonic(), q['timestamp'])

    def flush(self):
        now = time.monotonic()
        for symbol, q in list(self.latest.items()):
            at, stamp = self.published.get(symbol, (0, 0))
            if symbol in self.active.values() and (stamp != q['timestamp'] or now - at >= KEEPALIVE):
                self.publish(symbol, q)

    # Subscriptions ------------------------------------------------------

    async def send(self, ws, approval, symbol, key, subscribe):
        await ws.send(json.dumps({'header': {'approval_key': approval, 'custtype': 'P', 'tr_type': '1' if subscribe else '2',
                                             'content-type': 'utf-8'},
                                  'body': {'input': {'tr_id': 'HDFSCNT0', 'tr_key': key}}}))
        self.sent[key] = (symbol, 'subscribe' if subscribe else 'unsubscribe', time.monotonic())

    def exchange(self, symbol):
        if time.monotonic() - self.no_exchange.get(symbol, -1e9) < 600:
            return None
        try:
            return self.lookup.exchange(symbol)
        except MarketError:
            self.no_exchange[symbol] = time.monotonic()
            return None

    async def reconcile(self, ws, approval, session=None, interest=None):
        now = time.monotonic()
        session = session or clock_session()
        if interest is None:
            interest = await asyncio.to_thread(self.cache.stream_interest)
        interest = [s for s in interest if valid_symbol(s) and not s.startswith('KR:')]
        for key, (symbol, op, at) in list(self.sent.items()):
            if now - at > ACK_TIMEOUT:
                del self.sent[key]
        young = [s for s in self.active.values() if now - self.since.get(s, 0) < MIN_HOLD and s in interest]
        want = {}
        for symbol in young + [s for s in interest if s not in young]:
            if len(want) >= self.effective_limit:
                break
            exchange = await asyncio.to_thread(self.exchange, symbol)
            key = stream_key(symbol, exchange, session) if exchange else None
            if key:
                want[symbol] = key
        self.queued = [s for s in interest if s not in want][:20]
        for key, symbol in list(self.active.items()):
            if want.get(symbol) != key and key not in self.sent:
                await self.send(ws, approval, symbol, key, False)
        pending = sum(1 for _, op, _ in self.sent.values() if op == 'subscribe')
        for symbol, key in want.items():
            if key in self.active or key in self.sent:
                continue
            if len(self.active) + pending >= self.effective_limit:
                break
            await self.send(ws, approval, symbol, key, True)
            pending += 1

    async def handle(self, ws, message):
        if message[:1] in ('0', '1'):
            symbols = self.active | {k: v[0] for k, v in self.sent.items() if v[1] == 'subscribe'}
            for fields in parse_trades(message):
                symbol = symbols.get(fields.get('RSYM'))
                q = trade_quote(fields, symbol, fields.get('RSYM'), self.conn) if symbol else None
                if q:
                    log.debug('trade %s %s', symbol, q['price'])
                    await asyncio.to_thread(self.store, symbol, q)
            return
        try:
            data = json.loads(message)
        except ValueError:
            return
        header, body = data.get('header') or {}, data.get('body') or {}
        if header.get('tr_id') == 'PINGPONG':
            await ws.send(message)
            return
        text, code = (body.get('msg1') or '').strip(), body.get('msg_cd')
        if 'ALREADY IN USE' in text:
            raise StreamRejected('KIS app key already has a WebSocket session')
        entry = self.sent.pop(header.get('tr_key'), None)
        if not entry:
            return
        symbol, op, _ = entry
        key = header['tr_key']
        if body.get('rt_cd') == '0':
            if op == 'subscribe':
                self.active[key] = symbol
                self.since[symbol] = time.monotonic()
                log.info('symbol subscribed', extra={'path': key})
            else:
                self.active.pop(key, None)
                self.since.pop(symbol, None)
                self.latest.pop(symbol, None)
                log.info('symbol unsubscribed', extra={'path': key})
        elif code == 'OPSP0008':
            self.effective_limit = len(self.active)
            log.warning('subscription limit reached at %s symbols', self.effective_limit, extra={'path': key})
        else:
            self.last_error = code or 'subscribe_failed'
            log.warning('subscription rejected', extra={'path': key, 'status_code': code})

    # Connection ---------------------------------------------------------

    async def manage(self, ws, approval):
        while True:
            HEARTBEAT.touch()
            if not await asyncio.to_thread(self.lead):
                log.warning('trade stream leadership lost')
                await ws.close()
                return
            if time.time() - (self.last_message or 0) > SILENCE:
                log.warning('trade stream silent; reconnecting')
                await ws.close()
                return
            await self.reconcile(ws, approval)
            await asyncio.to_thread(self.flush)
            await asyncio.to_thread(self.write_status, True)
            await asyncio.sleep(2)

    async def connect(self):
        from websockets.asyncio.client import connect
        approval = await asyncio.to_thread(self.approval)
        url = os.getenv('KIS_WS_URL', 'ws://ops.koreainvestment.com:21000')
        async with connect(url, ping_interval=None, open_timeout=10, close_timeout=3, max_size=2 ** 20) as ws:
            self.conn, self.state, self.last_message = uuid4().hex, 'connected', time.time()
            log.info('trade stream connected' + ('; stream restored' if self.reconnects else ''))
            manager = asyncio.create_task(self.manage(ws, approval))
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

    async def run(self):
        maximum = int(os.getenv('US_STREAM_RECONNECT_MAX_SECONDS', '60'))
        delay = 1
        while True:
            HEARTBEAT.touch()
            if not enabled() or not self.kis.configured:
                self.state = 'disabled' if not enabled() else 'not_configured'
                await asyncio.to_thread(self.write_status, False)
                await asyncio.sleep(10)
                continue
            try:
                leader = await asyncio.to_thread(self.lead)
            except Exception:
                leader = False
            if not leader:
                # Another instance holds the only session the app key allows.
                self.state = 'standby'
                await asyncio.sleep(10)
                continue
            started = time.monotonic()
            try:
                await self.connect()
                self.last_error = None
            except Exception as exc:  # network, auth, protocol: never fatal
                self.last_error = type(exc).__name__
                log.warning('trade stream error', extra={'status_code': type(exc).__name__})
            if self.conn:
                log.info('trade stream disconnected; REST fallback activated')
            if time.monotonic() - started > 60:
                delay = 1
            self.reset()
            self.reconnects += 1
            self.state = 'reconnecting'
            try:
                await asyncio.to_thread(self.write_status, False)
            except Exception:
                pass
            log.info('trade stream reconnecting in %ss', delay)
            await asyncio.sleep(delay)
            delay = min(maximum, delay * 2)


def main():
    configure_logging()
    from .multi_market import KoreaPrices
    kis = KoreaPrices()
    if os.getenv('KOREA_MARKET_PROVIDER', 'kis') == 'disabled':
        kis.configured = False
    try:
        asyncio.run(TradeStream(kis).run())
    finally:
        kis.client.close()


if __name__ == '__main__':
    main()
