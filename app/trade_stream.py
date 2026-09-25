"""The one KIS WebSocket connection for US and Korean trades; prices go to Redis only.

Limits verified for the configured app key (scripts/check_us_sessions.py):
- One WebSocket session per app key. A second connection is refused with
  'ALREADY IN USE appkey', so web processes never connect. This worker holds a
  Redis leader lock and is the only client.
- Three registrations per session; more are refused with OPSP0008
  'MAX SUBSCRIBE OVER'. Unsubscribing frees a slot. US_STREAM_MAX_SUBSCRIPTIONS
  sets the cap, and a refusal lowers it for the rest of the connection.
- US: tr_id HDFSCNT0 with tr_key 'D' + NAS/NYS/AMS + symbol for the primary
  exchange and 'R' + BAQ/BAY/BAA + symbol for the day market.
- Korea: tr_key = 6-digit code. NXT-listed symbols use H0UNCNT0 (unified
  KRX+NXT trades); others H0STCNT0 (KRX), matching the REST source choice in
  kr_quotes. NXT-only H0NXCNT0 is not needed.
- Korean and US subscriptions share the same three registrations. Korean
  hours (08:00-20:00 KST) and the US regular session do not overlap.
Subscriptions are keyed 'tr_id|tr_key'.

Slots go to the symbols people are looking at or ordering right now
(market:stream:interest). Every other symbol is served by REST in the
market-worker. Browsers never see this connection.
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
from .market import MarketError
from .redis_cache import redis_cache
from .us_quotes import DAY_VENUE, PRIMARY_SESSIONS, USQuotes
from .us_session import NEW_YORK, clock_session
from . import kr_session
from .kr_quotes import capability, valid_sessions

log = logging.getLogger('trade-stream')
FIELDS = ('RSYM', 'SYMB', 'ZDIV', 'TYMD', 'XYMD', 'XHMS', 'KYMD', 'KHMS', 'OPEN', 'HIGH', 'LOW', 'LAST',
          'SIGN', 'DIFF', 'RATE', 'PBID', 'PASK', 'VBID', 'VASK', 'EVOL', 'TVOL', 'TAMT', 'BIVL', 'ASVL',
          'STRN', 'MTYP')
KR_FIELDS = {'code': 0, 'hour': 1, 'price': 2, 'sign': 3, 'diff': 4, 'rate': 5, 'high': 8, 'low': 9,
             'volume': 12, 'total_volume': 13, 'turnover': 14, 'date': 33}   # H0STCNT0 layout
HEARTBEAT = Path('/tmp/trade-stream-heartbeat')
STATUS_KEY = 'market:stream:status'
LEADER_KEY = 'market:stream:leader'
MIN_HOLD = 30     # seconds a subscription is kept before another symbol may take the slot
SILENCE = 150     # KIS sends PINGPONG regularly; a socket silent this long is dead
ACK_TIMEOUT = 10  # an unanswered (un)subscribe request is forgotten and retried
KEEPALIVE = 30    # republish the last trade so a quiet symbol's snapshot never expires


class StreamRejected(Exception):
    pass


def market_enabled(market):
    return os.getenv('KR_TRADE_STREAM_ENABLED' if market == 'KR' else 'US_TRADE_STREAM_ENABLED', 'true').lower() == 'true'


def enabled():
    return market_enabled('US') or market_enabled('KR')


def env(name, legacy, default):
    return int(os.getenv(name) or os.getenv(legacy) or default)


def stream_key(symbol, exchange, session):
    if session == 'overnight':
        return 'HDFSCNT0|R' + DAY_VENUE[exchange] + symbol
    if session in PRIMARY_SESSIONS:
        return 'HDFSCNT0|D' + exchange + symbol
    return None


def kr_stream_key(symbol, session, cap):
    if session not in kr_session.OPEN or session not in valid_sessions(cap):
        return None
    return ('H0UNCNT0|' if cap.get('nxt') else 'H0STCNT0|') + symbol[3:]


def records(message):
    """'0|TR_ID|002|f^f^...' holds `count` records of equal width: (tr_id, [fields])."""
    try:
        _, tr_id, count, body = message.split('|', 3)
        count = int(count)
    except ValueError:
        return None, []
    values = body.split('^')
    if count <= 0:
        return tr_id, []
    width = len(values) // count
    return tr_id, [values[i * width:(i + 1) * width] for i in range(count)]


def parse_trades(message):
    tr_id, rows = records(message)
    if tr_id != 'HDFSCNT0':
        return []
    return [dict(zip(FIELDS, row)) for row in rows if len(row) >= len(FIELDS)]


def kr_trade_quote(values, symbol, conn, reference=None, now=None, venue='UNIFIED'):
    """A validated unified Korean trade (native KRW price), or None.

    The trade must carry this symbol, today's Korean business date and a time
    inside a Korean session. `reference` (the current snapshot price) guards
    against a misread field: a print more than 30% away is rejected."""
    now = time.time() if now is None else now
    try:
        f = {name: values[i] for name, i in KR_FIELDS.items()}
        if f['code'] != symbol[3:]:
            return None
        local = datetime.strptime(f['date'] + f['hour'], '%Y%m%d%H%M%S').replace(tzinfo=kr_session.SEOUL)
        stamp = local.timestamp()
        price = Decimal(f['price']).quantize(Decimal('.0001'))
        diff, rate = Decimal(f['diff']), Decimal(f['rate'])
        if (not price.is_finite() or price <= 0 or not now - 43200 < stamp <= now + 60
                or local.date() != datetime.fromtimestamp(now, kr_session.SEOUL).date()):
            return None
        if reference and abs(price / Decimal(str(reference)) - 1) > Decimal('0.3'):
            return None
    except (IndexError, ValueError, InvalidOperation, ArithmeticError):
        return None
    if kr_session.clock_session(local) not in kr_session.OPEN:
        return None
    if f['sign'] in ('4', '5'):
        diff = -abs(diff)
    return instrument(symbol) | {
        'symbol': symbol, 'native_price': price, 'currency': 'KRW', 'timestamp': int(stamp), 'stale': False,
        'change': diff, 'change_pct': rate, 'high': f['high'] or None, 'low': f['low'] or None,
        'volume': f['total_volume'] or None, 'turnover': f['turnover'] or None, 'market': 'KR', 'venue': venue,
        'source': 'KIS', 'origin': 'stream', 'valid_sessions': list(kr_session.OPEN), 'stream_conn': conn,
        'received_at': now, '_cached_at': now,
        'data_status': f"KIS {'통합(KRX+NXT)' if venue == 'UNIFIED' else 'KRX'} 실시간 체결"}


def trade_quote(fields, symbol, key, conn, now=None):
    """A validated quote for one trade, or None.

    The trade must belong to the keyed subscription and its own New York time
    must fall in a session that venue serves. A primary-exchange print can
    therefore never become a day-market price, or the reverse."""
    now = time.time() if now is None else now
    try:
        if fields['RSYM'] != key.split('|')[-1] or fields['SYMB'] != symbol:
            return None
        stamp = datetime.strptime(fields['XYMD'] + fields['XHMS'], '%Y%m%d%H%M%S').replace(tzinfo=NEW_YORK).timestamp()
        price = Decimal(fields['LAST']).quantize(Decimal('.0001'))
        diff, rate = Decimal(fields['DIFF']), Decimal(fields['RATE'])
        if not price.is_finite() or price <= 0 or not now - 86400 < stamp <= now + 60:
            return None
    except (KeyError, ValueError, InvalidOperation):
        return None
    key = key.split('|')[-1]
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
        'volume': fields.get('TVOL') or None, 'source': 'KIS', 'origin': 'stream', 'market': 'US', 'venue': 'US',
        'exchange': key[1:4], 'valid_sessions': sessions,
        'stream_conn': conn, 'received_at': now, '_cached_at': now,
        'data_status': f"KIS {key[1:4]} 실시간 체결{' · 데이마켓' if overnight else ''}"}


class TradeStream:
    def __init__(self, kis, cache=redis_cache):
        self.kis, self.cache = kis, cache
        self.lookup = USQuotes(None, kis)
        self.limit = env('MARKET_STREAM_MAX_SUBSCRIPTIONS', 'US_STREAM_MAX_SUBSCRIPTIONS', 3)
        from .multi_market import ReferenceFX
        self.fx = ReferenceFX()
        self.kr_day = (0.0, None)
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

    def approval_key(self):
        return self.cache.key('market:kis:ws-approval', self.kis.key + '\0' + self.kis.secret)

    def approval(self):
        def issue():
            response = self.kis.client.post('/oauth2/Approval', json={
                'grant_type': 'client_credentials', 'appkey': self.kis.key, 'secretkey': self.kis.secret})
            response.raise_for_status()
            key = response.json().get('approval_key')
            if not key:
                raise StreamRejected('approval key refused')
            return {'approval_key': key}
        return self.cache.get_or_load(self.approval_key(), 43200, issue)['approval_key']

    def store(self, symbol, q):
        if q.get('market') == 'KR':
            # Same USD conversion as REST Korean quotes (ECB daily reference).
            rate, day = self.fx.krw_to_usd()
            q = q | {'price': (q['native_price'] * rate).quantize(Decimal('.0001')), 'fx_rate': rate, 'fx_date': day}
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
        tr_id, tr_key = key.split('|', 1)
        await ws.send(json.dumps({'header': {'approval_key': approval, 'custtype': 'P', 'tr_type': '1' if subscribe else '2',
                                             'content-type': 'utf-8'},
                                  'body': {'input': {'tr_id': tr_id, 'tr_key': tr_key}}}))
        self.sent[key] = (symbol, 'subscribe' if subscribe else 'unsubscribe', time.monotonic())

    def reference(self, symbol):
        snapshot = self.cache.get_json(f'market:price:{symbol}') or {}
        return snapshot.get('native_price')

    def exchange(self, symbol):
        if time.monotonic() - self.no_exchange.get(symbol, -1e9) < 600:
            return None
        try:
            return self.lookup.exchange(symbol)
        except MarketError:
            self.no_exchange[symbol] = time.monotonic()
            return None

    def kr_trading_day(self):
        at, day = self.kr_day
        if time.monotonic() - at > 600:
            from .providers import KRProvider
            day = KRProvider(self.kis).trading_day()
            self.kr_day = (time.monotonic(), day)
        return day

    def key_for(self, symbol, session, kr):
        if symbol.startswith('KR:'):
            if not market_enabled('KR') or kr not in kr_session.OPEN:
                return None
            return kr_stream_key(symbol, kr, capability(self.kis, symbol))
        exchange = self.exchange(symbol) if market_enabled('US') else None
        return stream_key(symbol, exchange, session) if exchange else None

    async def reconcile(self, ws, approval, session=None, interest=None, kr=None):
        now = time.monotonic()
        session = session or clock_session()
        if kr is None:
            day = await asyncio.to_thread(self.kr_trading_day)
            kr = kr_session.clock_session(trading_day=bool(day))
        if interest is None:
            interest = await asyncio.to_thread(self.cache.stream_interest)
        interest = [s for s in interest if valid_symbol(s)]
        for key, (symbol, op, at) in list(self.sent.items()):
            if now - at > ACK_TIMEOUT:
                del self.sent[key]
        young = [s for s in self.active.values() if now - self.since.get(s, 0) < MIN_HOLD and s in interest]
        want = {}
        for symbol in young + [s for s in interest if s not in young]:
            if len(want) >= self.effective_limit:
                break
            key = await asyncio.to_thread(self.key_for, symbol, session, kr)
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
            tr_id, rows = records(message)
            for values in rows:
                key = f'{tr_id}|{values[0] if values else ""}'
                symbol = symbols.get(key)
                if not symbol:
                    continue
                if tr_id == 'HDFSCNT0':
                    q = trade_quote(dict(zip(FIELDS, values)), symbol, key, self.conn) if len(values) >= len(FIELDS) else None
                else:
                    reference = (self.latest.get(symbol) or {}).get('native_price') or await asyncio.to_thread(self.reference, symbol)
                    q = kr_trade_quote(values, symbol, self.conn, reference, venue='UNIFIED' if tr_id == 'H0UNCNT0' else 'KRX')
                if q:
                    log.debug('trade %s %s', symbol, q['native_price'])
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
        if code == 'OPSP0011' or 'invalid approval' in text.lower():
            # Issuing a new approval key (any client with this app key)
            # invalidates the cached one; get a fresh key on reconnect.
            await asyncio.to_thread(self.cache.delete, self.approval_key())
            raise StreamRejected('KIS WebSocket approval key rejected')
        key = f"{header.get('tr_id')}|{header.get('tr_key')}"
        entry = self.sent.pop(key, None)
        if not entry:
            return
        symbol, op, _ = entry
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
        maximum = env('MARKET_STREAM_RECONNECT_MAX_SECONDS', 'US_STREAM_RECONNECT_MAX_SECONDS', 60)
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
            # Jitter keeps a restarted fleet from reconnecting in lockstep.
            await asyncio.sleep(delay * random.uniform(.8, 1.2))
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
