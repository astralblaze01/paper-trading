from decimal import Decimal
from sqlalchemy import select, func
from .db import Session, User, Position, Transaction
from .money import wallets, native_cost_basis
from .instruments import instrument
from .market import MarketError


RETURN_BASIS = '초기 KRW 평가액 대비 (외부 입출금 반영)'


def performance_return(equity, initial_equity, net_contributions=Decimal(0)):
    """The single return definition shared by portfolio and every ranking."""
    if equity is None or initial_equity is None or Decimal(str(initial_equity)) <= 0:
        return None
    equity = Decimal(str(equity))
    initial_equity = Decimal(str(initial_equity))
    contributions = Decimal(str(net_contributions or 0))
    return ( (equity - contributions) / initial_equity - 1 ) * 100


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


def initialize_equity(uid, fx):
    # New accounts: creation-time daily FX. Existing accounts: first verified migration-day rate.
    q=fx.current_rate('USD','KRW')
    with Session.begin() as db:
        user=db.scalar(select(User).where(User.id==uid).with_for_update())
        ensure_initial_krw(user,q['rate'],q['date'])
    return q


def portfolio(uid, market, fx):
    errors=[]; rate=None
    try: rate=initialize_equity(uid,fx)
    except MarketError as exc: errors.append(str(exc))
    with Session.begin() as db:
        user=db.scalar(select(User).where(User.id==uid).with_for_update())
        ws=wallets(db,user)
        balances={c:w.balance for c,w in ws.items()}
        positions=list(db.scalars(select(Position).where(Position.user_id==uid)))
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
        rows.append(info | {'quantity':p.quantity,'average_cost':average,'quote':q,'value':value,'pnl':pnl,
                            'return_pct':pnl/(average*p.quantity)*100 if pnl is not None and average else None})
    initial=user.initial_krw
    return {'username':user.username,'wallets':balances,'cash':balances['USD'],'positions':rows,
            'equity':equity if complete else None,'base_currency':'KRW','initial_equity':initial,
            # Rankings compare every account in USD at the current reference rate.
            'equity_usd':(equity/rate['rate']).quantize(Decimal('.0001')) if complete else None,
            # Ranking, portfolio and public portfolio all use this same
            # definition.  External grants/transfers are removed from the
            # numerator through net_contributions_krw.
            'return_basis':RETURN_BASIS,
            'initial_fx_date':user.initial_fx_date,'baseline_note':user.baseline_note,
            'initial_fx_effect':user.initial_usd*rate['rate']-initial if complete and initial else None,
            'other_pnl':equity-user.net_contributions_krw-user.initial_usd*rate['rate'] if complete and initial else None,
            'net_contributions_krw':user.net_contributions_krw,
            'pnl':equity-initial-user.net_contributions_krw if complete and initial else None,
            'return_pct':performance_return(equity,initial,user.net_contributions_krw) if complete else None,
            'realized_pnl':{'USD':realized.get('USD',Decimal(0)),'KRW':realized.get('KRW',Decimal(0))},
            'fx':rate,'errors':errors,'stale':any(r['quote'] and r['quote']['stale'] for r in rows)}
