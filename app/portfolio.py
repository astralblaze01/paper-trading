from decimal import Decimal
from sqlalchemy import select, func
from .db import Session, Position, Transaction, User, Wallet, lock_user
from .money import wallets, native_cost_basis
from .instruments import instrument
from .market import MarketError


RETURN_BASIS = '초기 KRW 평가액 대비 (외부 입출금 반영)'


class AccountGone(LookupError):
    """The account was deleted (e.g. withdrawn) after the caller chose to value it."""


def flow_adjusted_return(end, start, flow):
    """Percent return from `start` to `end` with external money `flow` taken out: (end - flow) / start - 1.

    None unless `start` is positive. Callers pass Decimals and keep their own rounding."""
    return ((end - flow) / start - 1) * 100 if start > 0 else None


def performance_return(equity, initial_equity, net_contributions=Decimal(0)):
    """The single return definition shared by portfolio and every ranking."""
    if equity is None or initial_equity is None or Decimal(str(initial_equity)) <= 0:
        return None
    equity = Decimal(str(equity))
    initial_equity = Decimal(str(initial_equity))
    contributions = Decimal(str(net_contributions or 0))
    return flow_adjusted_return(equity, initial_equity, contributions)


def krw_value(currency, native_value, usd_krw):
    """A native amount in KRW at the reference rate: the one conversion every valuation uses."""
    return native_value if currency == 'KRW' else native_value * usd_krw


def ensure_initial_krw(user, usd_krw, fx_date):
    """Fix the account's KRW starting value once, at the first reference rate it is valued with.

    Admin reset/rebase and the weekly SQL backfill set initial_krw without this
    rounding; they are left separate because routing them here would change
    the values they store."""
    if user.initial_krw is None:
        user.initial_krw=(user.initial_usd*usd_krw).quantize(Decimal('.0001'))
        user.initial_fx_date=fx_date


def dual_costs(trades, splits=None):
    """Cost of each open holding in KRW and in USD, each fill converted at its own rate.

    Moving average like the native cost: a buy adds its settled amount (fees
    included), a sale removes its share of the cost. A holding with a fill of
    unknown rate is left out, so it shows no converted return rather than a
    wrong one."""
    book, pending = {}, {s: list(v) for s, v in (splits or {}).items()}
    def split_until(symbol, until):
        # A split between fills multiplies the shares; the cost stays, less a fraction paid out.
        qty, krw, usd, known = book.get(symbol, (0, Decimal(0), Decimal(0), True))
        while pending.get(symbol) and (until is None or pending[symbol][0][0] <= until):
            _, ratio = pending[symbol].pop(0)
            if qty:
                exact = Decimal(qty) * ratio
                whole = int(exact)
                krw, usd, qty = krw * whole / exact, usd * whole / exact, whole
        book[symbol] = (qty, krw, usd, known)
    for t in trades:
        if pending: split_until(t.symbol, t.created_at)
        qty, krw, usd, known = book.get(t.symbol, (0, Decimal(0), Decimal(0), True))
        rate = t.usd_krw
        if rate is None or not rate > 0 or t.net_amount is None: known = False
        if t.side == 'buy':
            amount = t.net_amount or Decimal(0)
            if known:
                krw += amount * (rate if t.currency == 'USD' else 1)
                usd += amount if t.currency == 'USD' else amount / rate
            qty += t.quantity
        else:
            if qty: krw, usd = krw * (qty - t.quantity) / qty, usd * (qty - t.quantity) / qty
            qty -= t.quantity
            if qty <= 0: qty, krw, usd, known = 0, Decimal(0), Decimal(0), True
        book[t.symbol] = (qty, krw, usd, known)
    for symbol in list(pending): split_until(symbol, None)
    return {symbol: {'quantity': q, 'KRW': k, 'USD': u} for symbol, (q, k, u, known) in book.items() if known and q > 0}


def basis_view(value, currency, quantity, cost, usd_krw):
    """Average cost, P&L and return of one holding in KRW and USD at the current rate."""
    out = {}
    for basis in ('KRW', 'USD'):
        now = None
        if value is not None and usd_krw:
            now = value if currency == basis else value * usd_krw if basis == 'KRW' else value / usd_krw
        total = cost[basis] if cost else None
        out[basis] = {'average_cost': total / quantity if total is not None and quantity else None,
                      'pnl': now - total if now is not None and total is not None else None,
                      'return_pct': (now / total - 1) * 100 if now is not None and total else None}
    return out


def initialize_equity(uid, fx):
    # New accounts: creation-time daily FX. Existing accounts: first verified migration-day rate.
    q=fx.current_rate('USD','KRW')
    with Session.begin() as db:
        user=lock_user(db,uid)
        if user is None: raise AccountGone(uid)
        ensure_initial_krw(user,q['rate'],q['date'])
    return q


def portfolio(uid, market, fx):
    errors=[]; rate=None
    try: rate=initialize_equity(uid,fx)
    except MarketError as exc: errors.append(str(exc))
    with Session.begin() as db:
        user=lock_user(db,uid)
        if user is None: raise AccountGone(uid)
        ws=wallets(db,user)
        balances={c:w.balance for c,w in ws.items()}
        positions=list(db.scalars(select(Position).where(Position.user_id==uid)))
        from .splits import applied
        both=dual_costs(db.scalars(select(Transaction).where(Transaction.user_id==uid).order_by(Transaction.id)),applied(db))
        realized=dict(db.execute(select(Transaction.currency,func.sum(Transaction.realized_pnl)).where(Transaction.user_id==uid,Transaction.accounting_version==2,*([Transaction.created_at>=user.performance_since] if user.performance_since else [])).group_by(Transaction.currency)).all())
    rows=[]; equity=balances['KRW']+(balances['USD']*rate['rate'] if rate else 0)
    complete=rate is not None
    for p in positions:
        info=instrument(p.symbol); q=None
        try: q=market.quote(p.symbol)
        except MarketError as exc: errors.append(f'{p.symbol}: {exc}')
        native=Decimal(str(q.get('native_price',q['price']))) if q else None
        value=native*p.quantity if native is not None else None
        average=native_cost_basis(p)
        pnl=value-average*p.quantity if value is not None else None
        if value is None: complete=False
        elif info['currency']=='KRW' or rate: equity+=krw_value(info['currency'],value,rate['rate'] if rate else None)
        cost=both.get(p.symbol)
        # Replayed fills must account for the whole holding, or neither basis is shown.
        if cost and cost['quantity']!=p.quantity: cost=None
        rows.append(info | {'quantity':p.quantity,'average_cost':average,'quote':q,'value':value,'pnl':pnl,
                            'return_pct':pnl/(average*p.quantity)*100 if pnl is not None and average else None,
                            'basis':basis_view(value,info['currency'],p.quantity,cost,rate['rate'] if rate else None)})
    initial=user.initial_krw
    equity_usd=(equity/rate['rate']).quantize(Decimal('.0001')) if complete else None
    return {'username':user.username,'wallets':balances,'cash':balances['USD'],'positions':rows,
            'equity':equity if complete else None,'base_currency':'KRW','initial_equity':initial,
            # Rankings compare every account in USD at the current reference rate.
            'equity_usd':equity_usd,
            # Ranking, portfolio and public portfolio all use this same
            # definition.  External grants/transfers are removed from the
            # numerator through net_contributions_krw.
            'return_basis':RETURN_BASIS,
            'initial_fx_date':user.initial_fx_date,'baseline_note':user.baseline_note,'initial_usd':user.initial_usd,
            'initial_fx_effect':user.initial_usd*rate['rate']-initial if complete and initial else None,
            'other_pnl':equity-user.net_contributions_krw-user.initial_usd*rate['rate'] if complete and initial else None,
            'net_contributions_krw':user.net_contributions_krw,
            'pnl':equity-initial-user.net_contributions_krw if complete and initial else None,
            'return_pct':performance_return(equity,initial,user.net_contributions_krw) if complete else None,
            # The same account measured in dollars: the dollar principal against
            # today's dollar value, outside money at the rate it came in.  It
            # differs from the KRW return by the USD/KRW move since the start.
            'pnl_usd':equity_usd-user.initial_usd-user.net_contributions_usd if complete else None,
            'return_pct_usd':performance_return(equity_usd,user.initial_usd,user.net_contributions_usd) if complete else None,
            'realized_pnl':{'USD':realized.get('USD',Decimal(0)),'KRW':realized.get('KRW',Decimal(0))},
            'fx':rate,'errors':errors,'stale':any(r['quote'] and r['quote']['stale'] for r in rows)}


def ranking_values(ids, market, fx):
    """Account values for the ranking: the same figures portfolio() reports, without the rest.

    The ranking revalues every account every few seconds while a market is
    open. portfolio() locks each account row and replays its whole trade
    history for the per-holding cost view; none of that changes a total. Here
    every account is read in three queries without row locks, the reference
    rate is read once and each symbol is quoted once."""
    rate=None
    try: rate=fx.current_rate('USD','KRW')
    except MarketError: pass
    with Session() as db:
        users={u.id:u for u in db.scalars(select(User).where(User.id.in_(ids)))}
        balances={}
        for w in db.scalars(select(Wallet).where(Wallet.user_id.in_(ids))): balances[(w.user_id,w.currency)]=w.balance
        holdings={}
        for p in db.scalars(select(Position).where(Position.user_id.in_(ids)).order_by(Position.user_id,Position.symbol)):
            holdings.setdefault(p.user_id,[]).append((p.symbol,p.quantity))
    if rate and any(u.initial_krw is None for u in users.values()):
        # Fixed once per account, at the first rate it is valued with (as portfolio() does).
        for uid in [i for i,u in users.items() if u.initial_krw is None]:
            try: initialize_equity(uid,fx)
            except AccountGone: users.pop(uid)
        with Session() as db:
            for u in db.scalars(select(User).where(User.id.in_(list(users)))): users[u.id]=u
    quotes={}
    def quote(symbol):
        if symbol not in quotes:
            try: quotes[symbol]=market.quote(symbol)
            except MarketError: quotes[symbol]=None
        return quotes[symbol]
    values={}
    for uid in ids:
        user=users.get(uid)
        if user is None: continue
        # An account without a wallet row yet holds its legacy cash in USD (see money.wallets).
        usd=balances.get((uid,'USD'),user.cash); krw=balances.get((uid,'KRW'),Decimal(0))
        equity=krw+(usd*rate['rate'] if rate else 0); complete=rate is not None; stale=False
        for symbol,quantity in holdings.get(uid,[]):
            q=quote(symbol)
            if q is None: complete=False; continue
            stale=stale or bool(q['stale'])
            value=Decimal(str(q.get('native_price',q['price'])))*quantity
            if instrument(symbol)['currency']=='KRW' or rate: equity+=krw_value(instrument(symbol)['currency'],value,rate['rate'] if rate else None)
        equity_usd=(equity/rate['rate']).quantize(Decimal('.0001')) if complete else None
        values[uid]={'username':user.username,'equity':equity if complete else None,'equity_usd':equity_usd,
                     'return_pct':performance_return(equity,user.initial_krw,user.net_contributions_krw) if complete else None,
                     'return_pct_usd':performance_return(equity_usd,user.initial_usd,user.net_contributions_usd) if complete else None,
                     'fx':rate,'stale':stale}
    return values
