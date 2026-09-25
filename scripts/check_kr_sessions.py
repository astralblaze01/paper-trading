"""Check which Korean quote sources (KRX / NXT / unified) carry trades for the current session.

Read-only diagnostic. Run inside a container so it shares the Redis-cached KIS
token instead of issuing a new one:

    docker compose run --rm --no-deps -v ./scripts:/srv/scripts market-worker \
        python scripts/check_kr_sessions.py [--at HHMMSS] [005930 ...]

For each symbol and market code (J = KRX, NX = NXT, UN = unified) it prints
the latest minute bar *with volume* (a zero-volume bar only repeats the last
price and is not a trade), its session and age, the symbol's capability
(NXT listed, ETP) and whether that print could fill an order now.
`--at` asks for the bars up to another time of the latest trading day, e.g.
--at 084500 (NXT pre-market) or --at 183000 (after-hours) on a holiday.
Calls are spaced 1.5 s apart to stay inside the KIS rate limit.
"""
import argparse
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.market import MarketError  # noqa: E402
from app.multi_market import KoreaPrices  # noqa: E402
from app.providers import KRProvider  # noqa: E402
from app.kr_quotes import capability, valid_sessions, PATH  # noqa: E402
from app.kr_session import SEOUL, clock_session  # noqa: E402
from app.quote_policy import max_age  # noqa: E402

MARKETS = {'J': 'KRX', 'NX': 'NXT', 'UN': 'UNIFIED'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('symbols', nargs='*', default=['005930', '000660', '035420', '035720', '069500', '114260'])
    parser.add_argument('--at', help='HHMMSS: bars up to this time of the latest trading day')
    args = parser.parse_args()
    kis = KoreaPrices()
    day = KRProvider(kis).trading_day()
    now = datetime.now(timezone.utc)
    session = clock_session(now, trading_day=bool(day))
    print(f"now: {now.astimezone(SEOUL).isoformat()}  trading day: {day}  session: {session}\n")
    hour = args.at or now.astimezone(SEOUL).strftime('%H%M%S')
    for code in args.symbols:
        symbol = 'KR:' + code
        time.sleep(1.5)
        cap = capability(kis, symbol)
        print(f"{symbol}  nxt_listed={cap['nxt']} etp={cap['etp']} known={cap['known']}  valid_sessions={valid_sessions(cap)}")
        for market, venue in MARKETS.items():
            time.sleep(1.5)
            try:
                d = kis.get(PATH + 'inquire-time-itemchartprice', 'FHKST03010200',
                            {'FID_COND_MRKT_DIV_CODE': market, 'FID_INPUT_ISCD': code, 'FID_INPUT_HOUR_1': hour,
                             'FID_PW_DATA_INCU_YN': 'N', 'FID_ETC_CLS_CODE': ''}, 0)
            except MarketError as exc:
                print(f"  {venue:8} error: {exc}")
                continue
            bars = [b for b in d.get('output2') or [] if b.get('stck_bsop_date')]
            traded = [b for b in bars if Decimal(str(b.get('cntg_vol') or 0)) > 0]
            if not traded:
                print(f"  {venue:8} no traded bar in {len(bars)} bars (zero-volume bars only)")
                continue
            b = max(traded, key=lambda x: x['stck_bsop_date'] + x['stck_cntg_hour'])
            stamp = datetime.strptime(b['stck_bsop_date'] + b['stck_cntg_hour'], '%Y%m%d%H%M%S').replace(tzinfo=SEOUL)
            traded_session = clock_session(stamp)
            age = time.time() - stamp.timestamp()
            usable = (venue == 'UNIFIED' and traded_session == session and session in valid_sessions(cap)
                      and age <= max_age('KR'))
            print(f"  {venue:8} price={b['stck_prpr']:>9} vol={b['cntg_vol']:>7} time={stamp.isoformat()} "
                  f"trade_session={traded_session:11} age={age:>9.0f}s usable_now={'yes' if usable else 'no'}")


if __name__ == '__main__':
    main()
