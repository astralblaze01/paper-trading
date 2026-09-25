"""Daily account and benchmark snapshots: raw data for later performance research.

Once a day (DAILY_SNAPSHOT_HOUR, Asia/Seoul) every active non-admin account is
valued with one shared set of prices and one reference FX rate, and the result
is stored with everything needed to reproduce it. Rules:
- Prices are real provider quotes, or the last verified price in report_prices
  (at most DAILY_SNAPSHOT_MAX_QUOTE_AGE_DAYS old). Nothing is estimated.
- An account whose holdings cannot all be priced is not stored that day; the
  reason goes to snapshot_runs and the run retries until the window closes.
- One row per account per day (unique constraint + advisory lock). Rerunning
  a day skips accounts already stored.
- The cumulative return is portfolio.performance_return, as everywhere else.
No past days are reconstructed: data starts at the first run.

    python -m app.performance_snapshots --run-now
"""
import argparse
import logging
import os
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select, text, func
from sqlalchemy.dialects.postgresql import insert

from .db import (Session, engine, User, Position, ReportPrice, PerformanceSnapshot, BenchmarkSnapshot,
                 SnapshotRun, SNAPSHOT_LOCK)
from .instruments import instrument
from .market import MarketError
from .money import wallets, native_cost_basis
from .portfolio import performance_return, krw_value

SEOUL = ZoneInfo('Asia/Seoul')
METHOD_VERSION = 1
log = logging.getLogger('performance-snapshots')


def enabled():
    return os.getenv('DAILY_SNAPSHOT_ENABLED', 'true').lower() == 'true'


def benchmark_symbols():
    return [s.strip() for s in os.getenv('BENCHMARK_SYMBOLS', 'SPY,QQQ,KR:069500').split(',') if s.strip()]


def scheduled_for(now):
    """The local day and its fixed capture time.

    Default 07:00 Asia/Seoul: Korea is closed (last close stands) and the US
    regular session has ended in both summer and winter time, so every
    weekday snapshot sees the same market state."""
    hour = int(os.getenv('DAILY_SNAPSHOT_HOUR', '7'))
    if not 0 <= hour <= 23:
        raise ValueError('DAILY_SNAPSHOT_HOUR must be 0..23')
    day = now.astimezone(SEOUL).date()
    return day, datetime.combine(day, dtime(hour), SEOUL).astimezone(timezone.utc)


def _iso(value):
    return value.isoformat() if value else None


def price_for(symbol, market, db, now, fallback=True):
    """One verified native price with the facts about the quote, or None."""
    max_age = timedelta(days=int(os.getenv('DAILY_SNAPSHOT_MAX_QUOTE_AGE_DAYS', '7')))
    currency = instrument(symbol)['currency']
    try:
        q = market.quote(symbol)
        stamp = datetime.fromtimestamp(float(q['timestamp']), timezone.utc)
        native = Decimal(str(q.get('native_price', q['price'])))
        if q.get('currency', currency) != currency or not native.is_finite() or native <= 0:
            raise ValueError('invalid quote')
        if timedelta(seconds=-60) <= now - stamp <= max_age:
            return {'native_price': native, 'currency': currency, 'quote_time': stamp,
                    'meta': {'timestamp': int(stamp.timestamp()), 'quote_time': stamp.isoformat(),
                             'source': q.get('source'), 'stale': bool(q.get('stale')),
                             'price_mode': q.get('price_mode'), 'session': q.get('session'),
                             'trade_session': q.get('trade_session'), 'origin': q.get('origin'),
                             'fallback': False}}
    except (MarketError, ValueError, KeyError, TypeError, ArithmeticError):
        pass
    if not fallback:
        return None
    stored = db.get(ReportPrice, symbol)
    # report_prices holds the last real quote the weekly job verified.
    native = (stored.native_price if currency == 'KRW' else stored.price) if stored else None
    if native is None or not timedelta(seconds=-60) <= now - stored.quote_time <= max_age:
        return None
    return {'native_price': Decimal(native), 'currency': currency, 'quote_time': stored.quote_time,
            'meta': {'timestamp': int(stored.quote_time.timestamp()), 'quote_time': stored.quote_time.isoformat(),
                     'source': 'report_prices', 'stale': True, 'price_mode': 'cached', 'session': None,
                     'trade_session': None, 'origin': 'rest', 'fallback': True}}


def snapshot_user(uid, day, prices, fxq, now, scheduled):
    """Store one account's snapshot for `day`. Returns 'stored', 'exists' or an error text."""
    rate = Decimal(str(fxq['rate']))
    with Session.begin() as db:
        # Same lock as order execution: wallets and positions are read together.
        user = db.scalar(select(User).where(User.id == uid).with_for_update())
        if db.scalar(select(PerformanceSnapshot.id).where(PerformanceSnapshot.user_id == uid,
                                                          PerformanceSnapshot.snapshot_date == day)):
            return 'exists'
        if user.initial_krw is None:  # same initialisation as portfolio.initialize_equity
            user.initial_krw = (user.initial_usd * rate).quantize(Decimal('.0001'))
            user.initial_fx_date = fxq['date']
        ws = wallets(db, user)
        cash = {c: w.balance for c, w in ws.items()}
        holdings = list(db.scalars(select(Position).where(Position.user_id == uid).order_by(Position.symbol)))
        missing = [p.symbol for p in holdings if p.symbol not in prices]
        if missing:
            return '시세 없음: ' + ', '.join(missing)
        equity = cash['KRW'] + krw_value('USD', cash['USD'], rate)
        positions, quotes = {}, {}
        for p in holdings:
            price = prices[p.symbol]
            value = price['native_price'] * p.quantity
            equity += krw_value(price['currency'], value, rate)
            average = native_cost_basis(p)
            positions[p.symbol] = {'quantity': p.quantity, 'native_price': str(price['native_price']),
                                   'currency': price['currency'], 'market_value_native': str(value),
                                   'market_value_krw': str(krw_value(price['currency'], value, rate).quantize(Decimal('.0001'))),
                                   'average_cost': str(average)}
            quotes[p.symbol] = price['meta']
        stamps = [prices[p.symbol]['quote_time'] for p in holdings]
        stale = any(q['stale'] for q in quotes.values())
        total = performance_return(equity, user.initial_krw, user.net_contributions_krw)
        db.execute(insert(PerformanceSnapshot).values(
            user_id=uid, snapshot_date=day, snapshot_at=now, scheduled_for=scheduled,
            equity_krw=equity.quantize(Decimal('.0001')), equity_usd=(equity / rate).quantize(Decimal('.0001')),
            cash_krw=cash['KRW'], cash_usd=cash['USD'], net_contributions_krw=user.net_contributions_krw,
            initial_equity_krw=user.initial_krw, cumulative_return_pct=total.quantize(Decimal('.00000001')),
            fx_rate=rate, fx_date=fxq['date'], positions=positions, quote_metadata=quotes,
            quality={'complete': True, 'stale': stale, 'oldest_quote_at': _iso(min(stamps, default=None)),
                     'missing_symbols': [], 'fallback_symbols': [s for s, q in quotes.items() if q['fallback']]},
            stale=stale, oldest_quote_at=min(stamps, default=None),
            baseline={'initial_equity_krw': str(user.initial_krw), 'initial_fx_date': user.initial_fx_date,
                      'performance_since': _iso(user.performance_since), 'records_since': _iso(user.records_since),
                      'baseline_note': user.baseline_note},
            method_version=METHOD_VERSION, created_at=now,
        ).on_conflict_do_nothing(index_elements=['user_id', 'snapshot_date']))
        return 'stored'


def snapshot_benchmark(symbol, day, price, fxq, now):
    with Session.begin() as db:
        meta = price['meta']
        db.execute(insert(BenchmarkSnapshot).values(
            symbol=symbol, snapshot_date=day, price=price['native_price'], currency=price['currency'],
            quote_time=price['quote_time'], source=meta['source'], price_mode=meta['price_mode'],
            market_session=meta['session'], stale=meta['stale'], fx_rate=Decimal(str(fxq['rate'])),
            fx_date=fxq['date'], created_at=now).on_conflict_do_nothing(index_elements=['symbol', 'snapshot_date']))


def capture_daily_snapshots(market, fx, now=None, force=False):
    """Run (or continue) today's capture. Safe to call every minute from any process."""
    now = now or datetime.now(timezone.utc)
    if not enabled() and not force:
        return 'disabled'
    day, scheduled = scheduled_for(now)
    if now < scheduled and not force:
        return 'waiting'
    with engine.connect() as lock:
        if not lock.scalar(text('SELECT pg_try_advisory_lock(:k)'), {'k': SNAPSHOT_LOCK}):
            return 'busy'
        try:
            return snapshot_all_users(market, fx, day, scheduled, now, force)
        finally:
            lock.execute(text('SELECT pg_advisory_unlock(:k)'), {'k': SNAPSHOT_LOCK})
            lock.commit()


def snapshot_all_users(market, fx, day, scheduled, now, force=False):
    window = timedelta(minutes=int(os.getenv('DAILY_SNAPSHOT_RETRY_MINUTES', '120')))
    with Session.begin() as db:
        run = db.get(SnapshotRun, day)
        if run is None:
            run = SnapshotRun(snapshot_date=day, scheduled_for=scheduled, started_at=now, attempts=0,
                              errors={}, benchmarks={})
            db.add(run)
        if run.finished_at and not force:
            return 'done'
        if not force and run.last_attempt and now - run.last_attempt < timedelta(minutes=5):
            return 'retry-wait'
        if not force and now > scheduled + window:
            # Past the window the market state is no longer comparable; leave the gap.
            run.finished_at, run.outcome = now, 'missed' if not run.succeeded else 'partial'
            return run.outcome
        run.last_attempt, run.attempts = now, run.attempts + 1
    try:
        fxq = fx.current_rate('USD', 'KRW')
    except MarketError as exc:
        with Session.begin() as db:
            db.get(SnapshotRun, day).errors = {'_fx': str(exc)}
        return 'unavailable'
    with Session() as db:
        eligible = dict(db.execute(select(User.id, User.username).where(User.active.is_(True), User.is_admin.is_(False))).all())
        stored = set(db.scalars(select(PerformanceSnapshot.user_id).where(PerformanceSnapshot.snapshot_date == day)))
        stored_benchmarks = set(db.scalars(select(BenchmarkSnapshot.symbol).where(BenchmarkSnapshot.snapshot_date == day)))
        todo = [uid for uid in eligible if uid not in stored]
        symbols = set(db.scalars(select(Position.symbol).where(Position.user_id.in_(todo)))) if todo else set()
        benchmarks = [s for s in benchmark_symbols() if s not in stored_benchmarks]
        wanted = sorted(symbols | set(benchmarks))
        from .redis_cache import redis_cache
        for symbol in wanted:  # let the market-worker fetch them in one pass
            redis_cache.request_quote(symbol, force=True)
        prices, pending = {}, list(wanted)
        # A cold cache needs a few KIS round trips (1.1 s apart) per symbol:
        # keep asking for live quotes before using a stored last price.
        deadline = time.monotonic() + (int(os.getenv('DAILY_SNAPSHOT_QUOTE_WAIT_SECONDS', '60')) if redis_cache.configured else 0)
        while True:
            for symbol in list(pending):
                price = price_for(symbol, market, db, now, fallback=False)
                if price:
                    prices[symbol] = price
                    pending.remove(symbol)
            if not pending or time.monotonic() >= deadline:
                break
            time.sleep(2)
        for symbol in pending:
            price = price_for(symbol, market, db, now)
            if price:
                prices[symbol] = price
    errors = {}
    for uid in todo:
        try:
            result = snapshot_user(uid, day, prices, fxq, now, scheduled)
        except Exception as exc:  # one account must not stop the others
            log.exception('snapshot failed', extra={'user_id': uid})
            result = type(exc).__name__
        if result not in ('stored', 'exists'):
            errors[eligible[uid]] = result
    bench_status = {}
    for symbol in benchmark_symbols():
        if symbol in stored_benchmarks:
            bench_status[symbol] = 'stored'
        elif symbol in prices:
            snapshot_benchmark(symbol, day, prices[symbol], fxq, now)
            bench_status[symbol] = 'stored'
        else:
            bench_status[symbol] = 'missing'
    with Session.begin() as db:
        run = db.get(SnapshotRun, day)
        run.eligible = len(eligible)
        run.succeeded = db.scalar(select(func.count()).select_from(PerformanceSnapshot).where(PerformanceSnapshot.snapshot_date == day))
        run.failed, run.errors, run.benchmarks = len(errors), errors, bench_status
        if not errors and all(v == 'stored' for v in bench_status.values()):
            run.finished_at, run.outcome = now, 'complete'
            return 'complete'
        if force:
            run.outcome = 'partial'  # a manual run replaces an earlier 'missed'
        return 'partial'


def status(now=None):
    """Admin view of collection health."""
    now = now or datetime.now(timezone.utc)
    day, scheduled = scheduled_for(now)
    with Session() as db:
        run = db.get(SnapshotRun, day)
        last = db.scalar(select(func.max(PerformanceSnapshot.snapshot_at)))
        started = db.scalar(select(func.min(PerformanceSnapshot.snapshot_date)))
        return {'enabled': enabled(), 'today': day, 'scheduled_for': scheduled, 'timezone': 'Asia/Seoul',
                'collection_started': started, 'last_snapshot_at': last,
                'total_snapshots': db.scalar(select(func.count()).select_from(PerformanceSnapshot)),
                'today_run': None if run is None else {
                    'outcome': run.outcome, 'attempts': run.attempts, 'eligible': run.eligible,
                    'succeeded': run.succeeded, 'failed': run.failed, 'errors': run.errors,
                    'benchmarks': run.benchmarks, 'last_attempt': run.last_attempt, 'finished_at': run.finished_at},
                'benchmark_symbols': benchmark_symbols()}


# Period returns ----------------------------------------------------------------

PERIOD_DAYS = {'1W': 7, '1M': 30, '3M': 91, '1Y': 365}


def period_range(period, today):
    if period in PERIOD_DAYS:
        return today - timedelta(days=PERIOD_DAYS[period]), today
    if period == 'YTD':
        return date(today.year, 1, 1) - timedelta(days=1), today  # includes last year's final snapshot
    if period == 'ALL':
        return date.min, today
    raise ValueError('period')


def _baseline_key(row):
    return (row.baseline or {}).get('initial_equity_krw'), (row.baseline or {}).get('performance_since')


def flow_adjusted(end, start):
    """Same rule as the weekly ranking: (end - external flow) / start - 1."""
    flow = end.net_contributions_krw - start.net_contributions_krw
    return ((end.equity_krw - flow) / start.equity_krw - 1) * 100 if start.equity_krw > 0 else None


def series(uid, start, end):
    with Session() as db:
        rows = list(db.scalars(select(PerformanceSnapshot).where(
            PerformanceSnapshot.user_id == uid, PerformanceSnapshot.snapshot_date >= start,
            PerformanceSnapshot.snapshot_date <= end).order_by(PerformanceSnapshot.snapshot_date)))
        bench = list(db.scalars(select(BenchmarkSnapshot).where(
            BenchmarkSnapshot.snapshot_date >= start, BenchmarkSnapshot.snapshot_date <= end)))
    points, segment_start, rebased = [], 0, False
    for i, row in enumerate(rows):
        daily = None
        if i and _baseline_key(row) == _baseline_key(rows[i - 1]):
            daily = flow_adjusted(row, rows[i - 1])
        elif i:
            # Reset or rebase: the jump is not investment performance.
            segment_start, rebased = i, True
        points.append({'date': row.snapshot_date, 'equity_krw': row.equity_krw, 'equity_usd': row.equity_usd,
                       'cumulative_return_pct': row.cumulative_return_pct, 'daily_return_pct': daily,
                       'stale': row.stale})
    result = {'snapshots': points, 'period_return_pct': None, 'period_start': None, 'period_end': None,
              'baseline_changed': rebased, 'benchmarks': {}}
    if len(rows) - segment_start >= 2:
        first, last = rows[segment_start], rows[-1]
        period = flow_adjusted(last, first)
        result.update(period_return_pct=period, period_start=first.snapshot_date, period_end=last.snapshot_date)
        by_day = {(b.symbol, b.snapshot_date): b for b in bench}
        for symbol in benchmark_symbols():
            a, b = by_day.get((symbol, first.snapshot_date)), by_day.get((symbol, last.snapshot_date))
            if not a or not b:
                continue
            native = (b.price / a.price - 1) * 100
            # Accounts are valued in KRW, so the comparable benchmark return is in KRW too.
            krw = (krw_value(b.currency, b.price, b.fx_rate) / krw_value(a.currency, a.price, a.fx_rate) - 1) * 100
            result['benchmarks'][symbol] = {'return_pct': native, 'return_krw_pct': krw,
                                            'excess_return_pct': period - krw if period is not None else None}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--run-now', action='store_true', help="capture today's snapshots now")
    args = parser.parse_args()
    if not args.run_now:
        parser.error('nothing to do: pass --run-now')
    from .logging_config import configure_logging
    from .multi_market import MultiMarket
    from .fx import FxService
    configure_logging()
    market = MultiMarket()
    try:
        print(capture_daily_snapshots(market, FxService(market.fx), force=True))
        print(status())
    finally:
        market.close()


if __name__ == '__main__':
    main()
