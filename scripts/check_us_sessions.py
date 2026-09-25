"""Check which US quote sources carry current trades for the current session.

Read-only diagnostic. Run inside the market-worker container so it shares the
Redis-cached KIS token instead of issuing a new one:

    docker compose run --rm --no-deps -v ./scripts:/srv/scripts market-worker \
        python scripts/check_us_sessions.py [--seconds 30] [SYMBOL ...]

For every symbol it prints each source's price, trade timestamp and age, and
whether that price could be used as a trade price in the current session.
A source is only "usable" when it reports a trade time, that time falls in
the current session, and it is fresh. A price without a trade time (such as
the KIS current-price endpoint) is never usable on its own.
"""
import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.market import Finnhub, MarketError  # noqa: E402
from app.multi_market import KoreaPrices  # noqa: E402
from app.redis_cache import redis_cache  # noqa: E402
from app.us_session import NEW_YORK, clock_session, resolve_session  # noqa: E402

SEOUL = ZoneInfo('Asia/Seoul')
DAY = {'NAS': 'BAQ', 'NYS': 'BAY', 'AMS': 'BAA'}
PATH = '/uapi/overseas-price/v1/quotations/'
FRESH = 120  # seconds; a diagnostic threshold only


def exchange_of(symbol):
    for row in redis_cache.get_json('market:symbols:us') or []:
        if row.get('symbol') == symbol:
            return row.get('exchange')
    return None


def stamp_of(date_text, time_text, zone):
    try:
        return datetime.strptime(date_text + time_text, '%Y%m%d%H%M%S').replace(tzinfo=zone).timestamp()
    except (TypeError, ValueError):
        return None


def seoul_clock(hms):
    """inquire-ccnl gives a Seoul time without a date: take the latest past one."""
    now = datetime.now(SEOUL)
    stamp = stamp_of(now.strftime('%Y%m%d'), hms, SEOUL)
    return stamp - 86400 if stamp and stamp > time.time() + 60 else stamp


def row(symbol, provider, api, price, stamp, session, note=''):
    age = time.time() - stamp if stamp else None
    stamp_session = clock_session(datetime.fromtimestamp(stamp, timezone.utc)) if stamp else None
    usable = bool(price) and age is not None and age <= FRESH and stamp_session == session
    return {'symbol': symbol, 'provider': provider, 'api': api, 'price': price,
            'timestamp': datetime.fromtimestamp(stamp, NEW_YORK).isoformat() if stamp else None,
            'trade_session': stamp_session, 'age_seconds': round(age, 1) if age is not None else None,
            'stale': age is None or age > FRESH, 'usable_for_session': usable, 'note': note}


def rest_checks(kis, finnhub, symbol, exchange, session):
    out = []
    for code in (exchange, DAY.get(exchange)):
        if not code:
            continue
        try:
            d = kis.get(PATH + 'price', 'HHDFS00000300', {'AUTH': '', 'EXCD': code, 'SYMB': symbol}, 0)
            o = d.get('output') or {}
            out.append(row(symbol, 'KIS', f'REST price {code}', o.get('last') or None, None, session,
                           f"base={o.get('base')} ordy={o.get('ordy')} (no trade time in response)"))
        except MarketError as exc:
            out.append(row(symbol, 'KIS', f'REST price {code}', None, None, session, str(exc)))
        try:
            d = kis.get(PATH + 'inquire-ccnl', 'HHDFS76200300',
                        {'EXCD': code, 'AUTH': '', 'KEYB': '', 'TDAY': '1', 'SYMB': symbol}, 0)
            ticks = d.get('output2') or []
            t = ticks[0] if ticks else {}
            stamp = seoul_clock(t.get('khms', '')) if t else None
            out.append(row(symbol, 'KIS', f'REST inquire-ccnl {code}', t.get('last') or None, stamp, session,
                           f"{len(ticks)} ticks, KST time only, mtyp={t.get('mtyp')}" if ticks else 'no ticks'))
        except MarketError as exc:
            out.append(row(symbol, 'KIS', f'REST inquire-ccnl {code}', None, None, session, str(exc)))
        try:
            d = kis.get(PATH + 'inquire-time-itemchartprice', 'HHDFS76950200',
                        {'AUTH': '', 'EXCD': code, 'SYMB': symbol, 'NMIN': '1', 'PINC': '1', 'NEXT': '',
                         'NREC': '5', 'FILL': '', 'KEYB': ''}, 0)
            bars = d.get('output2') or []
            b = bars[0] if bars else {}
            stamp = stamp_of(b.get('xymd', ''), b.get('xhms', ''), NEW_YORK) if b else None
            window = d.get('output1') or {}
            out.append(row(symbol, 'KIS', f'REST minute bar {code}', b.get('last') or None, stamp, session,
                           f"bar start; KIS session {window.get('stim')}-{window.get('etim')} ET" if b else 'no bars'))
        except MarketError as exc:
            out.append(row(symbol, 'KIS', f'REST minute bar {code}', None, None, session, str(exc)))
    try:
        d = finnhub.get('/quote', {'symbol': symbol}, 0)
        out.append(row(symbol, 'Finnhub', 'REST /quote', d.get('c') or None, d.get('t') or None, session,
                       'free plan: last regular/extended print'))
    except MarketError as exc:
        out.append(row(symbol, 'Finnhub', 'REST /quote', None, None, session, str(exc)))
    return out


async def websocket_checks(kis, targets, seconds, session):
    """Subscribe HDFSCNT0 with both the regular (D) and day-market (R) keys."""
    try:
        import websockets
    except ImportError:
        return [{'provider': 'KIS', 'api': 'websocket HDFSCNT0', 'note': 'websockets package missing'}]
    # Reuse the stream worker's cached key: issuing a new one would invalidate it.
    from app.trade_stream import TradeStream
    approval = TradeStream(kis).approval()
    if not approval:
        return [{'provider': 'KIS', 'api': 'websocket HDFSCNT0', 'note': 'approval key rejected'}]
    keys = {}
    for symbol, exchange in targets:
        keys['D' + exchange + symbol] = (symbol, 'D' + exchange)
        if exchange in DAY:
            keys['R' + DAY[exchange] + symbol] = (symbol, 'R' + DAY[exchange])
    latest, answers = {}, {}
    async with websockets.connect('ws://ops.koreainvestment.com:21000', ping_interval=None) as ws:
        for key in keys:
            # Stop the market-stream service first: the app key allows one session.
            await ws.send(json.dumps({'header': {'approval_key': approval, 'custtype': 'P', 'tr_type': '1',
                                                 'content-type': 'utf-8'},
                                      'body': {'input': {'tr_id': 'HDFSCNT0', 'tr_key': key}}}))
            await asyncio.sleep(.2)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                message = await asyncio.wait_for(ws.recv(), max(.1, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
            if message[:1] in ('0', '1'):
                _, tr_id, count, body = message.split('|', 3)
                fields = body.split('^')
                width = len(fields) // max(1, int(count))
                for i in range(int(count)):
                    f = fields[i * width:(i + 1) * width]
                    # RSYM, SYMB, ZDIV, TYMD, XYMD, XHMS, KYMD, KHMS, OPEN, HIGH, LOW, LAST ...
                    latest[f[0]] = (f[11], stamp_of(f[4], f[5], NEW_YORK), f[6] + f[7])
                continue
            data = json.loads(message)
            if data.get('header', {}).get('tr_id') == 'PINGPONG':
                await ws.send(message)
                continue
            key = data.get('header', {}).get('tr_key')
            answers[key] = data.get('body', {}).get('msg1', '').strip()
    out = []
    for key, (symbol, prefix) in keys.items():
        price, stamp, seoul = latest.get(key, (None, None, None))
        out.append(row(symbol, 'KIS', f'websocket HDFSCNT0 {prefix}', price, stamp, session,
                       f"subscribe: {answers.get(key, 'no reply')}; " + (f'KST {seoul}' if price else 'no trade received')))
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('symbols', nargs='*', default=['AAPL', 'TSLA', 'NVDA', 'MSFT', 'AMZN', 'QQQ', 'SPY'])
    parser.add_argument('--seconds', type=int, default=30, help='websocket listen time')
    args = parser.parse_args()
    kis, finnhub = KoreaPrices(), Finnhub()
    try:
        status = finnhub.get('/stock/market-status', {'exchange': 'US'}, 0)
    except MarketError:
        status = None
    session = resolve_session(status)
    now = datetime.now(timezone.utc)
    print(f'now: {now.astimezone(NEW_YORK).isoformat()} / {now.astimezone(SEOUL).isoformat()}')
    print(f'session: {session} (clock {clock_session(now)}, finnhub {status and status.get("session")})\n')
    targets = [(s, exchange_of(s) or 'NAS') for s in args.symbols]
    rows = []
    for symbol, exchange in targets:
        rows += rest_checks(kis, finnhub, symbol, exchange, session)
    if args.seconds > 0 and kis.configured:
        rows += asyncio.run(websocket_checks(kis, targets, args.seconds, session))
    for symbol, _ in targets:
        print(symbol)
        for r in rows:
            if r.get('symbol') == symbol:
                print(f"  {r['provider']:7} {r['api']:30} price={r['price']!s:10} time={r['timestamp']!s:25} "
                      f"age={r['age_seconds']!s:>9}s trade_session={r['trade_session']!s:11} "
                      f"usable={'yes' if r['usable_for_session'] else 'no':3} {r['note']}")
    for r in rows:
        if 'symbol' not in r:
            print(r)


if __name__ == '__main__':
    main()
