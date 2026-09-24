"""Durable weekly website publication; never sends external messages."""
import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Event, Thread
from zoneinfo import ZoneInfo
from sqlalchemy import select, text, func
from sqlalchemy.orm import aliased
from .db import Session, User, Position, WeeklyState, WeeklyReport, ReportPrice, Wallet
from .market import MarketError
from .portfolio import performance_return, RETURN_BASIS

SEOUL = ZoneInfo('Asia/Seoul')
log = logging.getLogger(__name__)


def schedule():
    weekday = int(os.getenv('WEEKLY_DAY', '5'))  # Monday=0, Saturday=5
    hour = int(os.getenv('WEEKLY_HOUR', '9'))
    if not 0 <= weekday <= 6 or not 0 <= hour <= 23:
        raise ValueError('WEEKLY_DAY must be 0..6 and WEEKLY_HOUR 0..23')
    return weekday, hour


def next_run(now):
    weekday, hour = schedule()
    local = now.astimezone(SEOUL)
    target = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    target += timedelta(days=(weekday - local.weekday()) % 7)
    if target <= local: target += timedelta(days=7)
    return target.astimezone(timezone.utc)


def standings(baseline, accounts):
    rows, excluded = [], 0
    for uid, account in accounts.items():
        start = Decimal(baseline[uid]['equity']) if uid in baseline else None
        if start is None or start <= 0:
            excluded += 1
            continue
        end = Decimal(account['equity'])
        flow=Decimal(account.get('contributions','0'))-Decimal(baseline[uid].get('contributions','0'))
        weekly = (((end-flow) / start - 1) * 100).quantize(Decimal('.000001'))
        total = performance_return(end, account.get('initial_equity', 100000), account.get('contributions', '0'))
        rows.append({'username': account['username'], 'start_equity': str(start), 'equity': str(end),
                     'pnl': str(end - start-flow), 'return_pct': str(weekly),
                     'total_return_pct': str(total.quantize(Decimal('.000001')) if total is not None else Decimal(0))})
    rows.sort(key=lambda r: (-Decimal(r['return_pct']), r['username']))
    previous, rank = None, 0
    for index, row in enumerate(rows, 1):
        if row['return_pct'] != previous: rank = index
        row['rank'] = rank
        previous = row['return_pct']
    return rows, excluded


def tick(market, now=None, fx=None):
    now = now or datetime.now(timezone.utc)
    with Session.begin() as db:
        # One publisher across threads/processes; crash automatically releases lock.
        if not db.scalar(text('SELECT pg_try_advisory_xact_lock(74923102)')):
            return 'busy'
        state = db.get(WeeklyState, 1)
        if state is None:
            state = WeeklyState(id=1, baseline={}, next_due=next_run(now))
            db.add(state)
        # Poll every 15 minutes; the worker wakes every minute at the due time.
        due = now >= state.next_due
        if not due and state.last_attempt and now - state.last_attempt < timedelta(minutes=15):
            return 'waiting'
        if due and state.error and state.last_attempt and now - state.last_attempt < timedelta(minutes=5):
            return 'retry-wait'
        state.last_attempt = now
        base_currency='KRW' if fx else 'USD'
        try: fxq=fx.current_rate('USD','KRW') if fx else {'rate':Decimal(1),'date':now.date().isoformat()}
        except MarketError:
            state.error='기준환율을 확인할 수 없어 주간 집계를 보류합니다.'
            return 'unavailable'
        if fx:
            db.execute(text('UPDATE users SET initial_krw=initial_usd*:rate, initial_fx_date=:day WHERE initial_krw IS NULL'), {'rate':fxq['rate'],'day':fxq['date']})
        usd,krw=aliased(Wallet),aliased(Wallet)
        snapshot=db.execute(select(User.id,User.username,func.coalesce(usd.balance,User.cash).label('cash'),func.coalesce(krw.balance,0).label('krw'),User.initial_usd,User.initial_krw,User.net_contributions_krw,Position.symbol,Position.quantity)
                            .outerjoin(Position,Position.user_id==User.id)
                            .outerjoin(usd,(usd.user_id==User.id)&(usd.currency=='USD'))
                            .outerjoin(krw,(krw.user_id==User.id)&(krw.currency=='KRW')).where(User.active.is_(True),User.is_admin.is_(False))).all()
        symbols = {r.symbol for r in snapshot if r.symbol}
        prices, metadata, missing = {}, {}, []
        for symbol in sorted(symbols):
            stored = db.get(ReportPrice, symbol)
            try:
                q = market.quote(symbol)
                stamp = datetime.fromtimestamp(q['timestamp'], timezone.utc)
                price = Decimal(str(q['price']))
                if not price.is_finite() or price <= 0 or stamp > now + timedelta(seconds=60):
                    raise MarketError('invalid quote')
                if stored is None:
                    stored = ReportPrice(symbol=symbol, price=price, native_price=q.get('native_price',price), quote_time=stamp, fetched_at=now, fx_date=q.get('fx_date'))
                    db.add(stored)
                elif stamp >= stored.quote_time:
                    stored.native_price=q.get('native_price',price)
                    stored.price, stored.quote_time, stored.fetched_at, stored.fx_date = price, stamp, now, q.get('fx_date')
            except (MarketError, ValueError, KeyError, TypeError):
                pass
            # Weekend/holiday report valuations may use the last real quote, never an invented price.
            if stored is None or not timedelta(seconds=-60) <= now - stored.quote_time <= timedelta(days=7):
                missing.append(symbol)
                continue
            if stored.fx_date:
                fx_age = (now.date() - datetime.fromisoformat(stored.fx_date).date()).days
                if not 0 <= fx_age <= 7:
                    missing.append(symbol)
                    continue
            if fx and symbol.startswith('KR:') and stored.native_price is None:
                missing.append(symbol); continue
            prices[symbol] = (stored.native_price if symbol.startswith('KR:') else stored.price*fxq['rate']) if fx else stored.price
            metadata[symbol] = {'quote_time': stored.quote_time.isoformat(), 'fetched_at': stored.fetched_at.isoformat(), 'fx_date': stored.fx_date}
        if missing:
            state.error = ('일부 시세를 평가할 수 없어 집계를 보류합니다: ' + ', '.join(missing))[:500]
            return 'unavailable'
        accounts = {}
        for r in snapshot:
            uid = str(r.id)
            if uid not in accounts: accounts[uid] = {'username':r.username,'equity':r.cash*fxq['rate']+r.krw if fx else r.cash,'initial_equity':str(r.initial_krw or r.initial_usd*fxq['rate']),'contributions':str(r.net_contributions_krw if fx else r.net_contributions_krw/fxq['rate'])}
            if r.symbol: accounts[uid]['equity'] += prices[r.symbol] * r.quantity
        for account in accounts.values(): account['equity'] = str(account['equity'])
        state.error = None
        if state.baseline_at is None or state.currency!=base_currency:
            if not accounts: return 'empty'
            state.currency=base_currency
            state.baseline, state.baseline_at = accounts, now
            state.next_due = next_run(now)
            return 'baseline'
        if not due: return 'cached'
        rows, excluded = standings(state.baseline, accounts)
        report = WeeklyReport(scheduled_for=state.next_due, period_start=state.baseline_at,
                              period_end=now, rows=rows, notes={'excluded_new_or_zero': excluded,
                              'base_currency':base_currency, 'return_basis':RETURN_BASIS,
                              'fx_rate':str(fxq['rate']), 'fx_date':fxq['date'], 'quotes': metadata, 'late': now - state.next_due > timedelta(minutes=10)})
        db.add(report)
        state.baseline, state.baseline_at, state.next_due = accounts, now, next_run(now)
        return 'published'


class WeeklyWorker:
    def __init__(self, market):
        self.market = market
        self.stop_event = Event()
        self.thread = Thread(target=self.run, name='weekly-publisher', daemon=True)

    def start(self):
        schedule()  # Fail early for invalid configuration.
        self.thread.start()

    def run(self):
        while not self.stop_event.is_set():
            try:
                result = tick(self.market)
                if result in ('published', 'baseline'): log.info('Weekly publisher: %s', result)
            except Exception:
                log.exception('Weekly publisher failed; retrying on next tick')
            self.stop_event.wait(60)

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=10)


def report_list(page=1):
    with Session() as db:
        state = db.get(WeeklyState, 1)
        reports = list(db.scalars(select(WeeklyReport).order_by(WeeklyReport.period_end.desc()).offset((page - 1) * 10).limit(10)))
        day, hour = schedule()
        admin_names=set(db.scalars(select(User.username).where(User.is_admin.is_(True))))
        def visible_rows(rows):
            filtered=[dict(row) for row in rows if row['username'] not in admin_names]
            last=None; rank=0
            for i,row in enumerate(filtered,1):
                if row['return_pct']!=last: rank=i
                row['rank']=rank; last=row['return_pct']
            return filtered
        return {'enabled': os.getenv('WEEKLY_ENABLED', 'true').lower() == 'true',
                'weekday': day, 'hour': hour, 'timezone': 'Asia/Seoul',
                'next_due': state.next_due if state else None,
                'baseline_at': state.baseline_at if state else None,
                'error': '시세 확인이 완료되지 않아 집계를 기다리고 있습니다.' if state and state.error else None,
                'reports': [{'id': r.id, 'scheduled_for': r.scheduled_for, 'period_start': r.period_start,
                             'period_end': r.period_end, 'rows': visible_rows(r.rows), 'fx_rate':r.notes.get('fx_rate'), 'base_currency':r.notes.get('base_currency','USD'), 'return_basis':r.notes.get('return_basis',RETURN_BASIS),
                             'excluded_new_or_zero': r.notes['excluded_new_or_zero'], 'late': r.notes['late'],
                             'oldest_quote': min((q['quote_time'] for q in r.notes['quotes'].values()), default=None)} for r in reports]}
