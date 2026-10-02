"""Stock splits and reverse splits: the schedule from KIS, and restating holdings on the day.

On the effective date (local: Korea for KRX listings, New York for US ones) every
holding becomes quantity × ratio shares at average cost ÷ ratio, so its value and
profit are unchanged. A reverse split's fraction of a share is paid out in cash at
the current price (cash in lieu), like a broker does. Pending reservations of the
listing are restated the same way (price ÷ ratio); one left below one share is
cancelled. Each account's restatement is recorded once (SplitApplication).

Where the app replays fills (who held a dividend at its cut-off, the KRW/USD cost
basis) splits applied in between are applied in order too.

Splits effective before APPLY_FROM (the day this started) are not applied: no
holding then predated one, and a fill after a split is already in new shares.

Sources: KRX listings from KIS 예탁원 액면교체 일정 (face value before/after, trading
resumes on the new listing date); US listings from KIS period rights 14 (split) and
15 (reverse split), whose allocation rate is a percentage (2000 = one share became
twenty), effective on the local base date.
"""
import logging
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from math import floor
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .db import Session, Position, LimitOrder, Wallet, SplitEvent, SplitApplication, lock_user
from .instruments import currency_of

log = logging.getLogger('splits')
SEOUL, NEW_YORK = ZoneInfo('Asia/Seoul'), ZoneInfo('America/New_York')
APPLY_FROM = date(2026, 10, 2)
SPLIT_RIGHTS = ('14', '15')


def _day(value):
    value = str(value or '').strip()
    for fmt in ('%Y%m%d', '%Y/%m/%d', '%Y-%m-%d'):
        try: return datetime.strptime(value[:10], fmt).date()
        except ValueError: continue
    return None


def _number(value):
    try:
        n = Decimal(str(value).strip().lstrip('0') or '0')
        return n if n.is_finite() and n > 0 else None
    except (ArithmeticError, ValueError): return None


def moment(symbol, day):
    """The UTC instant a split takes effect: the start of its local effective date."""
    zone = SEOUL if symbol.startswith('KR:') else NEW_YORK
    return datetime.combine(day, time(0), zone).astimezone(timezone.utc)


def local_today(symbol, now):
    return now.astimezone(SEOUL if symbol.startswith('KR:') else NEW_YORK).date()


def schedule(symbol, kis, today, start, end):
    """[{effective_date, ratio, source}] for splits of this listing between start and end."""
    if symbol.startswith('KR:'):
        rows = kis.get('/uapi/domestic-stock/v1/ksdinfo/rev-split', 'HHKDB669105C0',
                       {'CTS': '', 'MARKET_GB': '0', 'F_DT': start.strftime('%Y%m%d'), 'T_DT': end.strftime('%Y%m%d'),
                        'SHT_CD': symbol[3:]}, 21600).get('output1') or []
        out = []
        for r in rows:
            before, after, listed = _number(r.get('inter_bf_face_amt')), _number(r.get('inter_af_face_amt')), _day(r.get('list_dt'))
            # Until the new listing date is set, trading has not resumed and nothing changes yet.
            if str(r.get('sht_cd', '')) == symbol[3:] and before and after and listed and before != after:
                out.append({'effective_date': listed, 'ratio': before / after, 'source': 'KIS 예탁원 액면교체'})
        return out
    from .dividends import _period_rights, PRODUCT_TYPES
    from .us_symbols import exchange_of
    product = PRODUCT_TYPES.get(exchange_of(symbol), '512')
    out = []
    for kind in SPLIT_RIGHTS:
        for r in _period_rights(kis, symbol, kind, start, end, product):
            effective, rate = _day(r.get('acpl_bass_dt')), _number(r.get('stck_alct_rt'))
            if effective and rate and rate != 100:
                out.append({'effective_date': effective, 'ratio': rate / 100, 'source': 'KIS 해외 권리'})
    return out


def store(db, symbol, events, start, end):
    """Upsert this listing's splits; an unapplied one no longer listed in the window is dropped."""
    fresh = {e['effective_date'] for e in events}
    for row in db.scalars(select(SplitEvent).where(SplitEvent.symbol == symbol, SplitEvent.applied_at.is_(None),
                                                   SplitEvent.effective_date >= start, SplitEvent.effective_date <= end)):
        if row.effective_date not in fresh: db.delete(row)
    db.flush()
    for e in events:
        row = db.scalar(select(SplitEvent).where(SplitEvent.symbol == symbol, SplitEvent.effective_date == e['effective_date']))
        if row is None: db.add(SplitEvent(symbol=symbol, **e))
        elif row.applied_at is None: row.ratio, row.source = e['ratio'], e['source']


def applied(db, symbol=None):
    """{symbol: [(moment, ratio), ...]} of applied splits, oldest first."""
    query = select(SplitEvent).where(SplitEvent.applied_at.is_not(None)).order_by(SplitEvent.effective_date)
    if symbol: query = query.where(SplitEvent.symbol == symbol)
    out = {}
    for e in db.scalars(query): out.setdefault(e.symbol, []).append((moment(e.symbol, e.effective_date), e.ratio))
    return out


def replay_quantity(trades, splits, until):
    """Shares held just before `until`, replaying fills and the splits between them in time order."""
    steps = [(t.created_at, 1, t) for t in trades if t.created_at < until] + [(m, 0, r) for m, r in splits if m < until]
    quantity = 0
    for _, kind, item in sorted(steps, key=lambda s: (s[0], s[1])):
        if kind == 0: quantity = floor(quantity * item)
        else: quantity += item.quantity if item.side == 'buy' else -item.quantity
    return quantity


def apply_due(market, now=None):
    """Restate holdings for every split whose effective date has come; returns accounts restated."""
    now = now or datetime.now(timezone.utc)
    with Session() as db:
        due = [e.id for e in db.scalars(select(SplitEvent).where(SplitEvent.applied_at.is_(None), SplitEvent.effective_date >= APPLY_FROM))
               if e.effective_date <= local_today(e.symbol, now)]
    restated = 0
    for split_id in due:
        with Session() as db:
            event = db.get(SplitEvent, split_id)
            holders = [(uid, qty) for uid, qty in db.execute(select(Position.user_id, Position.quantity).where(Position.symbol == event.symbol))]
        price = None
        if any((Decimal(q) * event.ratio) % 1 for _, q in holders):
            # A fraction is paid at today's price; without one, wait for the next pass.
            try:
                quote = market.quote(event.symbol)
                price = Decimal(str(quote.get('native_price', quote['price'])))
            except Exception as exc:
                log.warning('split of %s waits for a price: %s', event.symbol, exc); continue
        with Session.begin() as db:
            event = db.get(SplitEvent, split_id, with_for_update=True)
            if event is None or event.applied_at is not None: continue
            currency = currency_of(event.symbol)
            for user_id, _ in holders:
                user = lock_user(db, user_id)
                if user is None: continue
                position = db.get(Position, (user_id, event.symbol), with_for_update=True)
                if position is None or db.scalar(select(SplitApplication.id).where(SplitApplication.user_id == user_id, SplitApplication.split_id == event.id)): continue
                exact = Decimal(position.quantity) * event.ratio
                whole = int(exact.to_integral_value(rounding=ROUND_DOWN))
                cash = ((exact - whole) * price).quantize(Decimal('1') if currency == 'KRW' else Decimal('.0001'), rounding=ROUND_DOWN) if exact != whole else Decimal(0)
                before = position.quantity
                if position.native_average_cost is not None: position.native_average_cost /= event.ratio
                position.average_cost /= event.ratio
                position.quantity = whole
                if whole == 0: db.delete(position)
                if cash:
                    wallet = db.get(Wallet, (user_id, currency), with_for_update=True)
                    wallet.balance += cash
                    if currency == 'USD': user.cash = wallet.balance
                db.add(SplitApplication(user_id=user_id, split_id=event.id, symbol=event.symbol, ratio=event.ratio, before_quantity=before,
                                        after_quantity=whole, cash=cash, currency=currency, created_at=now))
                restated += 1
            for order in db.scalars(select(LimitOrder).where(LimitOrder.symbol == event.symbol, LimitOrder.status == 'pending').with_for_update()):
                order.limit_price = (order.limit_price / event.ratio).quantize(Decimal('.0001'))
                if not order.use_max:
                    order.quantity = int((Decimal(order.quantity) * event.ratio).to_integral_value(rounding=ROUND_DOWN))
                    if order.quantity == 0: order.status, order.reason = 'cancelled', '주식 병합으로 주문 수량이 1주 미만이 되어 취소했습니다.'
            event.applied_at = now
    return restated


def summary(user_id, now=None):
    """This account's restated holdings and the coming splits of what it holds."""
    from .instruments import instrument
    now = now or datetime.now(timezone.utc)
    with Session() as db:
        done = db.execute(select(SplitApplication, SplitEvent.effective_date).join(SplitEvent, SplitEvent.id == SplitApplication.split_id)
                          .where(SplitApplication.user_id == user_id).order_by(SplitApplication.id.desc()).limit(50)).all()
        held = set(db.scalars(select(Position.symbol).where(Position.user_id == user_id, Position.quantity > 0)))
        coming = [e for e in db.scalars(select(SplitEvent).where(SplitEvent.applied_at.is_(None), SplitEvent.symbol.in_(list(held) or [''])).order_by(SplitEvent.effective_date))
                  if e.effective_date >= APPLY_FROM]
    return {'applied': [{'symbol': a.symbol, 'name': instrument(a.symbol)['name'], 'effective_date': day, 'ratio': a.ratio,
                         'before_quantity': a.before_quantity, 'after_quantity': a.after_quantity, 'cash': a.cash, 'currency': a.currency,
                         'created_at': a.created_at} for a, day in done],
            'upcoming': [{'symbol': e.symbol, 'name': instrument(e.symbol)['name'], 'effective_date': e.effective_date, 'ratio': e.ratio} for e in coming]}
