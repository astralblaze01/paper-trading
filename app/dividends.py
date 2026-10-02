"""Cash dividends: the schedule from KIS, and crediting each holder's wallet on the pay date.

Who is paid: whoever held the shares at the close of the last day a purchase could
still settle by the record date (KRX settles T+2, US markets T+1). That is two
weekdays before the record date in Korea and one weekday before it in New York;
the holding then comes from the account's own fills, so selling on the ex-date
still earns the dividend and buying on it does not. Exchange holidays are not
modelled.

When: Korean dividends on the pay date (Korea time); US dividends the day after
the US pay date, when Korean brokers credit them. The wallet of the listing's
currency receives the dividend after withholding tax (KR_DIVIDEND_TAX_BPS,
US_DIVIDEND_TAX_BPS). Each account is paid at most once per dividend.

Dividends paid before PAY_FROM (the day this feature started) are never credited
retroactively. The schedule is refreshed every SYNC_HOURS for listings someone
holds or traded recently, a few listings per scheduler pass so it never crowds out
live quotes on the shared KIS budget; crediting is checked on every pass.
"""
import logging
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from .db import Session, Settings, Position, Transaction, Wallet, DividendEvent, DividendPayment, lock_user
from .instruments import currency_of, instrument
from .market import MarketError
from .money import bps

log = logging.getLogger('dividends')
SEOUL, NEW_YORK = ZoneInfo('Asia/Seoul'), ZoneInfo('America/New_York')
PAY_FROM = date(2026, 10, 1)
SYNC_HOURS = 6
SYNC_KEY, CURSOR_KEY, ROUND_DONE = 'DIVIDENDS_SYNCED_AT', 'DIVIDENDS_CURSOR', '~'
SYNC_BATCH = 2   # listings per scheduler pass (one a minute): 2-4 KIS calls each for US listings
# A year back gives each listing's rhythm and annual total; Korean year-end dividends
# are paid about four months after the record date.
LOOKBACK_DAYS, LOOKAHEAD_DAYS = 400, 120
RECENT_TRADE_DAYS = 60
MAX_RIGHTS_PAGES = 10
PRODUCT_TYPES = {'NAS': '512', 'NYS': '513', 'AMS': '529'}   # KIS overseas product type by exchange


def _weekdays_before(day, count):
    while count:
        day -= timedelta(days=1)
        if day.weekday() < 5: count -= 1
    return day


def cutoff(symbol, record_date):
    """The moment holdings are counted: the end of the last day a purchase settles by the record date."""
    korean = symbol.startswith('KR:')
    last = _weekdays_before(record_date, 2 if korean else 1)
    zone = SEOUL if korean else NEW_YORK
    return datetime.combine(last + timedelta(days=1), time(0), zone).astimezone(timezone.utc)


def credit_day(symbol, pay_date):
    """The Korea-time day the wallet is credited."""
    return pay_date if symbol.startswith('KR:') else pay_date + timedelta(days=1)


def _day(value, fmt):
    value = str(value or '').strip()
    try: return datetime.strptime(value, fmt).date() if value else None
    except ValueError: return None


def _amount(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number > 0 else None
    except (ArithmeticError, ValueError): return None


def schedule(symbol, kis, today, lookback=LOOKBACK_DAYS):
    """[{record_date, pay_date, per_share, currency, source}] for cash dividends around today."""
    start, end = today - timedelta(days=lookback), today + timedelta(days=LOOKAHEAD_DAYS)
    if symbol.startswith('KR:'):
        rows = kis.get('/uapi/domestic-stock/v1/ksdinfo/dividend', 'HHKDB669102C0',
                       {'CTS': '', 'GB1': '0', 'F_DT': start.strftime('%Y%m%d'), 'T_DT': end.strftime('%Y%m%d'),
                        'SHT_CD': symbol[3:], 'HIGH_GB': ''}, 21600).get('output1') or []
        events = []
        for r in rows:
            record, per_share = _day(r.get('record_date'), '%Y%m%d'), _amount(r.get('per_sto_divi_amt'))
            # An announced dividend has an amount; its pay date may follow later.
            if record and per_share and str(r.get('sht_cd', symbol[3:])) == symbol[3:]:
                events.append({'record_date': record, 'pay_date': _day(r.get('divi_pay_dt'), '%Y/%m/%d'),
                               'per_share': per_share, 'currency': 'KRW', 'source': 'KIS 예탁원 배당'})
        return events
    # The product type must name the listing's exchange: with another one KIS answers
    # with a different security's rights, or none (NYSE KO under NASDAQ's code).
    from .us_symbols import exchange_of
    product = PRODUCT_TYPES.get(exchange_of(symbol), '512')
    # Period rights match the code loosely (QQQ also returns QQQM, QQQS, ...) and come
    # 100 rows a page, so every page is read and rows are kept for this exact symbol.
    params = {'RGHT_TYPE_CD': '03', 'INQR_DVSN_CD': '02', 'INQR_STRT_DT': start.strftime('%Y%m%d'),
              'INQR_END_DT': end.strftime('%Y%m%d'), 'PDNO': symbol, 'PRDT_TYPE_CD': product,
              'CTX_AREA_NK50': '', 'CTX_AREA_FK50': ''}
    rights = []
    for page in range(MAX_RIGHTS_PAGES):
        data = kis.get('/uapi/overseas-price/v1/quotations/period-rights', 'CTRGT011R', params, 21600, **({'tr_cont': 'N'} if page else {}))
        rights += data.get('output') or []
        if data.get('_tr_cont') not in ('M', 'F'): break
        params = params | {'CTX_AREA_NK50': data.get('ctx_area_nk50', ''), 'CTX_AREA_FK50': data.get('ctx_area_fk50', '')}
    else:
        raise MarketError(f'{symbol} 권리 조회가 {MAX_RIGHTS_PAGES}쪽을 넘었습니다.')
    pays = kis.get('/uapi/overseas-price/v1/quotations/rights-by-ice', 'HHDFS78330900',
                   {'NCOD': 'US', 'SYMB': symbol, 'ST_YMD': start.strftime('%Y%m%d'), 'ED_YMD': end.strftime('%Y%m%d')},
                   21600).get('output1') or []
    # Only a right on the cash-dividend list is a dividend (a split or other right also
    # appears among period rights). The list can repeat a record date with several pay
    # dates; the earliest on or after the record date is taken.
    pay_by_record = {}
    for p in pays:
        if '현금배당' not in str(p.get('ca_title', '')): continue
        record, pay = _day(p.get('record_dt'), '%Y%m%d'), _day(p.get('pay_dt'), '%Y%m%d')
        if record is None: continue
        if pay is not None and pay < record: pay = None
        known = pay_by_record.get(record, None)
        pay_by_record[record] = pay if known is None else min(known, pay) if pay else known
    amounts = {}
    for r in rights:
        record, per_share = _day(r.get('acpl_bass_dt'), '%Y%m%d'), _amount(r.get('alct_frcr_unpr'))
        if record in pay_by_record and per_share and str(r.get('pdno', '')).upper() == symbol and r.get('crcy_cd', 'USD') == 'USD':
            amounts[record] = amounts.get(record, Decimal(0)) + per_share   # e.g. a regular dividend and a special one
    return [{'record_date': record, 'pay_date': pay_by_record[record], 'per_share': amount, 'currency': 'USD', 'source': 'KIS 해외 권리'}
            for record, amount in sorted(amounts.items())]


def tracked_symbols(db, now):
    held = db.scalars(select(Position.symbol).where(Position.quantity > 0).distinct())
    traded = db.scalars(select(Transaction.symbol).where(Transaction.created_at >= now - timedelta(days=RECENT_TRADE_DAYS)).distinct())
    return sorted(set(held) | set(traded))


def sync(kis, symbols, now=None):
    """Refresh the dividend schedule of `symbols` in order; returns the symbols read.

    Stops at a request-limit refusal: KIS shares one request budget with live quotes,
    so that means waiting for the next pass. Any other error skips just that listing."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(SEOUL).date()
    done = []
    for symbol in symbols:
        try: events = schedule(symbol, kis, today)
        except Exception as exc:
            log.warning('dividend schedule unavailable for %s: %s', symbol, exc)
            if '한도' in str(exc): break
            done.append(symbol); continue
        done.append(symbol)
        start, end = today - timedelta(days=LOOKBACK_DAYS), today + timedelta(days=LOOKAHEAD_DAYS)
        with Session.begin() as db:
            # An unpaid dividend the provider no longer lists in the window read was a
            # mistake or was withdrawn; paid ones stay as history.
            fresh = {e['record_date'] for e in events}
            for row in db.scalars(select(DividendEvent).where(DividendEvent.symbol == symbol, DividendEvent.paid_at.is_(None),
                                                              DividendEvent.record_date >= start, DividendEvent.record_date <= end)):
                if row.record_date not in fresh: db.delete(row)
            db.flush()
            for e in events:
                row = db.scalar(select(DividendEvent).where(DividendEvent.symbol == symbol, DividendEvent.record_date == e['record_date']))
                if row is None:
                    db.add(DividendEvent(symbol=symbol, **e))
                elif row.paid_at is None:  # announcements fill in or correct the amount and pay date
                    row.pay_date, row.per_share, row.currency, row.source = e['pay_date'], e['per_share'], e['currency'], e['source']
    return done


def holding_at(db, user_id, symbol, moment):
    bought = func.coalesce(func.sum(Transaction.quantity).filter(Transaction.side == 'buy'), 0)
    sold = func.coalesce(func.sum(Transaction.quantity).filter(Transaction.side == 'sell'), 0)
    return int(db.execute(select(bought - sold).where(Transaction.user_id == user_id, Transaction.symbol == symbol,
                                                      Transaction.created_at < moment)).scalar() or 0)


def withholding(symbol, gross, currency):
    rate = bps('KR_DIVIDEND_TAX_BPS' if symbol.startswith('KR:') else 'US_DIVIDEND_TAX_BPS')
    tax = (gross * rate / 10000).quantize(Decimal('1') if currency == 'KRW' else Decimal('.01'), rounding=ROUND_DOWN)
    return tax, rate


def pay_due(now=None):
    """Credit every dividend whose credit day has come; returns how many accounts were paid."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(SEOUL).date()
    paid = 0
    with Session() as db:
        due = [e.id for e in db.scalars(select(DividendEvent).where(DividendEvent.paid_at.is_(None), DividendEvent.pay_date.is_not(None),
                                                                     DividendEvent.pay_date >= PAY_FROM))
               if credit_day(e.symbol, e.pay_date) <= today]
    for event_id in due:
        with Session.begin() as db:
            event = db.get(DividendEvent, event_id, with_for_update=True)
            if event is None or event.paid_at is not None: continue
            moment = cutoff(event.symbol, event.record_date)
            holders = db.scalars(select(Transaction.user_id).where(Transaction.symbol == event.symbol, Transaction.created_at < moment).distinct()).all()
            for user_id in holders:
                if db.scalar(select(DividendPayment.id).where(DividendPayment.user_id == user_id, DividendPayment.event_id == event.id)): continue
                user = lock_user(db, user_id)
                if user is None: continue
                quantity = holding_at(db, user_id, event.symbol, moment)
                if quantity <= 0: continue
                currency = event.currency
                gross = (event.per_share * quantity).quantize(Decimal('1') if currency == 'KRW' else Decimal('.0001'), rounding=ROUND_HALF_UP)
                tax, rate = withholding(event.symbol, gross, currency)
                net = gross - tax
                wallet = db.get(Wallet, (user_id, currency), with_for_update=True)
                if wallet is None: wallet = Wallet(user_id=user_id, currency=currency, balance=Decimal(0)); db.add(wallet)
                wallet.balance += net
                if currency == 'USD': user.cash = wallet.balance  # legacy mirror; wallets are authoritative
                db.add(DividendPayment(user_id=user_id, event_id=event.id, symbol=event.symbol, quantity=quantity, per_share=event.per_share,
                                       currency=currency, gross=gross, tax=tax, tax_bps=rate, net=net, created_at=now))
                paid += 1
            event.paid_at = now
    return paid


def run(kis, now=None):
    """The scheduler's dividend step: read a few listings' schedules, then pay what is due.

    One pass reads at most SYNC_BATCH listings, in symbol order after the cursor, so a
    full round spreads over several minutes; a new round starts SYNC_HOURS after the
    last one began."""
    now = now or datetime.now(timezone.utc)
    with Session.begin() as db:
        started, cursor = db.get(Settings, SYNC_KEY, with_for_update=True), db.get(Settings, CURSOR_KEY, with_for_update=True)
        if started is None or (cursor is not None and cursor.value == ROUND_DONE
                               and datetime.fromisoformat(started.value) <= now - timedelta(hours=SYNC_HOURS)):
            db.merge(Settings(key=SYNC_KEY, value=now.isoformat())); db.merge(Settings(key=CURSOR_KEY, value=''))
            after = ''
        else:
            after = cursor.value if cursor is not None else ''
        pending = [] if after == ROUND_DONE else [s for s in tracked_symbols(db, now) if s > after][:SYNC_BATCH]
    synced = sync(kis, pending, now) if pending else []
    if synced or not pending:
        with Session.begin() as db:
            finished = not pending or (len(synced) == len(pending) and len(pending) < SYNC_BATCH)
            if after != ROUND_DONE: db.merge(Settings(key=CURSOR_KEY, value=ROUND_DONE if finished else synced[-1]))
    return {'synced': len(synced), 'paid': pay_due(now)}


def summary(user_id, now=None):
    """This account's dividends: payments received, totals per currency, and upcoming ones."""
    now = now or datetime.now(timezone.utc)
    with Session() as db:
        payments = db.execute(select(DividendPayment, DividendEvent.record_date, DividendEvent.pay_date)
                              .join(DividendEvent, DividendEvent.id == DividendPayment.event_id)
                              .where(DividendPayment.user_id == user_id).order_by(DividendPayment.id.desc()).limit(100)).all()
        zero = Decimal(0)
        totals = {c: {'gross': zero, 'tax': zero, 'net': zero, 'count': 0} for c in ('USD', 'KRW')}
        for c, gross, tax, net, count in db.execute(select(DividendPayment.currency, func.sum(DividendPayment.gross), func.sum(DividendPayment.tax),
                                                           func.sum(DividendPayment.net), func.count())
                                                    .where(DividendPayment.user_id == user_id).group_by(DividendPayment.currency)):
            totals[c] = {'gross': gross, 'tax': tax, 'net': net, 'count': count}
        held = dict(db.execute(select(Position.symbol, Position.quantity).where(Position.user_id == user_id, Position.quantity > 0)).all())
        upcoming = []
        today = now.astimezone(SEOUL).date()
        for e in db.scalars(select(DividendEvent).where(DividendEvent.paid_at.is_(None), DividendEvent.symbol.in_(list(held) or [''])).order_by(DividendEvent.record_date)):
            moment = cutoff(e.symbol, e.record_date)
            if e.pay_date is not None and e.pay_date < PAY_FROM: continue
            if moment <= now:
                quantity, status = holding_at(db, user_id, e.symbol, moment), '지급 예정'
            else:
                quantity, status = held[e.symbol], '보유 시 지급'
            if quantity <= 0: continue
            gross = e.per_share * quantity
            tax, rate = withholding(e.symbol, gross, e.currency)
            upcoming.append({'symbol': e.symbol, 'name': instrument(e.symbol)['name'], 'record_date': e.record_date, 'pay_date': e.pay_date,
                             'credit_date': credit_day(e.symbol, e.pay_date) if e.pay_date else None,
                             'buy_by': moment.astimezone(SEOUL if e.symbol.startswith('KR:') else NEW_YORK).date() - timedelta(days=1),
                             'per_share': e.per_share, 'currency': e.currency, 'quantity': quantity, 'expected_net': gross - tax, 'status': status})
        holdings = [_holding_outlook(db, user_id, symbol, quantity, [u for u in upcoming if u['symbol'] == symbol], today)
                    for symbol, quantity in sorted(held.items())]
    rows = [{'symbol': p.symbol, 'name': instrument(p.symbol)['name'], 'record_date': record, 'pay_date': pay, 'quantity': p.quantity,
             'per_share': p.per_share, 'currency': p.currency, 'gross': p.gross, 'tax': p.tax, 'net': p.net, 'created_at': p.created_at}
            for p, record, pay in payments]
    annual = {c: sum((h['annual_net'] for h in holdings if h['currency'] == c), Decimal(0)) for c in ('USD', 'KRW')}
    return {'payments': rows, 'totals': totals, 'upcoming': upcoming, 'holdings': holdings, 'annual_net': annual,
            'rates': {'KR_DIVIDEND_TAX_BPS': bps('KR_DIVIDEND_TAX_BPS'), 'US_DIVIDEND_TAX_BPS': bps('US_DIVIDEND_TAX_BPS')}}


FREQUENCY = {12: '월배당', 4: '분기배당', 2: '반기배당', 1: '연배당'}


def _after_tax(symbol, per_share, quantity, currency):
    gross = per_share * quantity
    return gross - withholding(symbol, gross, currency)[0]


def _holding_outlook(db, user_id, symbol, quantity, upcoming, today):
    """One holding's next dividend (announced, or estimated from its rhythm) and a year's worth, after tax.

    The rhythm is the median gap between recent record dates, read as monthly, quarterly,
    half-yearly or yearly. An estimate repeats the latest dividend: its amount and its
    record-to-pay gap. A year's worth is the latest amount times the payments a year."""
    currency = currency_of(symbol)
    events = list(db.scalars(select(DividendEvent).where(DividendEvent.symbol == symbol).order_by(DividendEvent.record_date)))
    past = [e for e in events if e.record_date <= today]
    recent = past[-5:]
    gaps = sorted(g for g in ((b.record_date - a.record_date).days for a, b in zip(recent, recent[1:])) if g > 0)
    step = gaps[len(gaps) // 2] if gaps else None
    per_year = min(FREQUENCY, key=lambda n: abs(365 / n - step)) if step else 1
    latest = upcoming[0]['per_share'] if upcoming else past[-1].per_share if past else Decimal(0)
    row = {'symbol': symbol, 'name': instrument(symbol)['name'], 'currency': currency, 'quantity': quantity,
           'frequency': FREQUENCY[per_year] if step else None, 'annual_per_share': latest * per_year,
           'annual_net': _after_tax(symbol, latest * per_year, quantity, currency), 'next': None, 'known': bool(events)}
    if upcoming:
        u = upcoming[0]
        row['next'] = {'record_date': u['record_date'], 'credit_date': u['credit_date'], 'buy_by': u['buy_by'],
                       'per_share': u['per_share'], 'quantity': u['quantity'], 'net': u['expected_net'],
                       'confirmed': True, 'entitled': u['status'] == '지급 예정'}
    elif past:
        last = past[-1]
        record = last.record_date
        while record <= today: record += timedelta(days=step or 365)
        lag = (last.pay_date - last.record_date) if last.pay_date else timedelta(days=30)
        moment = cutoff(symbol, record)
        row['next'] = {'record_date': record, 'credit_date': credit_day(symbol, record + lag),
                       'buy_by': moment.astimezone(SEOUL if symbol.startswith('KR:') else NEW_YORK).date() - timedelta(days=1),
                       'per_share': last.per_share, 'quantity': quantity,
                       'net': _after_tax(symbol, last.per_share, quantity, currency), 'confirmed': False, 'entitled': False}
    return row


def trailing_yield(symbol, kis, price, today):
    """US annual yield from KIS dividends with record dates in the last 12 months, in percent."""
    events = [e for e in schedule(symbol, kis, today, 366) if today - timedelta(days=365) < e['record_date'] <= today]
    total = sum((e['per_share'] for e in events), Decimal(0))
    return (total / price * 100).quantize(Decimal('.01')) if total > 0 and price > 0 else None
