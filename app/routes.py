import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal
from uuid import UUID
from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, func, delete
from sqlalchemy.dialects.postgresql import insert
from .db import Session, User, FxTransaction, Watchlist, PopularityEvent, LimitOrder, lock_user
from .instruments import SYMBOL_PATTERN, valid_symbol, instrument, CATALOG, market_of
from .market import MarketError
from .fx import preview, exchange
from .money import MAX_ORDER_QUANTITY, wallets, rounded
from .trading import preview_order
from .admin_ops import admin_overview, set_initial_amount, set_account_active, season_reset, list_archives
from .branding import BRAND_NAME

class Strict(BaseModel):
    model_config=ConfigDict(extra='forbid')
class FxInput(Strict):
    source: Literal['USD','KRW']
    amount: Decimal = Field(gt=0,le=1000000000000,max_digits=18,decimal_places=4)
class FxOrder(FxInput): request_id: UUID
class SymbolInput(Strict): symbol: str=Field(pattern=SYMBOL_PATTERN)
class EventInput(SymbolInput): kind: Literal['view','search']='view'
class LimitInput(SymbolInput):
    side: Literal['buy','sell']
    quantity: int=Field(gt=0,le=MAX_ORDER_QUANTITY,strict=True)
    limit_price: Decimal=Field(gt=0,le=1000000000,max_digits=16,decimal_places=4)
    request_id: UUID

class ResetInput(Strict):
    confirmation: Literal['RESET']
    label: str=Field(min_length=1,max_length=80)
class AmountInput(Strict): amount: Decimal=Field(ge=1,le=1000000000,max_digits=14,decimal_places=4)
class ActiveInput(Strict): active: bool


def event(uid,symbol,kind):
    with Session.begin() as db:
        db.execute(insert(PopularityEvent).values(user_id=uid,symbol=symbol,kind=kind,bucket=int(time.time())//3600,created_at=datetime.now(timezone.utc)).on_conflict_do_nothing())


def curated_market_rows(market, asset, kind, unavailable=None):
    """Actual quotes from a small disclosed catalog, never a claimed full-market ranking."""
    rows=[]; failures=[]
    for symbol, _, category, _ in CATALOG:
        if category!=asset: continue
        row=instrument(symbol) | {'market':market_of(symbol)}
        try:
            q=market.quote(symbol)
            row |= {'price':q.get('native_price',q['price']), 'change_pct':q.get('change_pct'),
                    'volume':q.get('volume'), 'turnover':q.get('turnover'), 'data_time':datetime.fromtimestamp(q['timestamp'],timezone.utc).isoformat(),
                    'data_status':q.get('data_status','공급자 시세')+(' · 오래된 시세' if q.get('stale') else '')}
        except MarketError as exc:
            failures.append(str(exc))
            row |= {'price':None,'change_pct':None,'volume':None,'turnover':None,'data_time':None,'data_status':str(exc)}
        rows.append(row)
    sort_key={'volume':'turnover','shares':'volume'}.get(kind,'change_pct')
    def score(row):
        try:
            value=Decimal(str(row.get(sort_key)))
            return value if value.is_finite() else None
        except (InvalidOperation,TypeError):
            return None
    scores={r['symbol']:score(r) for r in rows}
    priced=[r for r in rows if scores[r['symbol']] is not None]
    def sort_order(row):
        # Unpriced rows go last; the rest by value, largest first unless listing fallers.
        value=scores[row['symbol']]
        if value is None: return True, Decimal(0)
        return False, value if kind=='down' else -value
    if priced: rows.sort(key=sort_order)
    notes=['등록 종목의 실제 공급자 시세입니다. 전체 시장 순위가 아닙니다.']
    measure={'volume':'거래대금','shares':'거래량'}.get(kind)
    if measure and not priced: notes.append(f'이 시세 공급자는 {measure}을 제공하지 않아 {measure}순으로 정렬할 수 없습니다.')
    if measure and priced: notes.append(f'{measure}이 제공된 종목만 그 값으로 정렬하고 나머지는 뒤에 표시합니다.')
    if unavailable: notes.append(str(unavailable))
    if failures: notes.append(f'{len(failures)}개 종목 시세 조회 실패/공급자 설정 필요')
    return {'rows':rows,'scope':'등록 종목 둘러보기','notice':' '.join(notes),'unavailable':bool(unavailable),
            'source':'실제 시세' if priced else '시세 확인 대기'}


def install(app,ctx):
    user=ctx.current_user; csrf=ctx.csrf
    def diagnostics_allowed(uid):
        with Session() as db:
            return bool(db.scalar(select(User.is_admin).where(User.id==uid)))
    def market_result(result, uid):
        """Keep operational symbols for detail navigation, but protect provider diagnostics."""
        clean=dict(result)
        with Session() as db:
            watches=set(db.scalars(select(Watchlist.symbol).where(Watchlist.user_id==uid)))
        is_admin=diagnostics_allowed(uid)
        if not is_admin:
            clean.pop('source',None); clean.pop('data_time',None)
            count=len(result.get('rows',[]))
            if '등록 종목' in result.get('scope',''):
                clean['scope']='등록 종목'
                clean['notice']=f'{count}개 · 전체 시장 순위가 아닙니다.'
            elif '인기' not in result.get('scope',''):
                clean['scope']='시장 순위'
                clean['notice']=f'{count}개 종목'+(' · 거래대금 추정치' if any(r.get('turnover_estimated') for r in result.get('rows',[])) else '')
        clean['rows']=[{k:v for k,v in row.items() if is_admin or k not in ('data_time','data_status','source')} | {'watchlisted':row['symbol'] in watches} for row in result.get('rows',[])]
        return clean
    def admin(uid=Depends(user)):
        with Session() as db:
            if not db.get(User,uid).is_admin: raise HTTPException(403,'관리자 권한이 필요합니다.')
        return uid
    def provider(symbol):
        if not valid_symbol(symbol): raise HTTPException(422,'잘못된 종목코드입니다.')
        return ctx.market.providers[market_of(symbol)]

    @app.get('/api/order-preview')
    def order_preview(symbol: str, side: Literal['buy','sell']='buy', quantity: int=Query(1,ge=1,le=MAX_ORDER_QUANTITY),share: int|None=Query(None),uid=Depends(user)):
        if not valid_symbol(symbol): raise HTTPException(422,'잘못된 종목코드입니다.')
        if share is not None and share not in (5,10,25,50,100): raise HTTPException(422,'지원하지 않는 수량 비율입니다.')
        from .redis_cache import redis_cache
        redis_cache.request_stream(symbol)
        return preview_order(uid,symbol,side,quantity,ctx.market,share=share)|{'market_closed':ctx.closed_market_message(symbol)}

    @app.get('/api/fx')
    def fx_rate(uid=Depends(user)): return ctx.fx.current_rate('USD','KRW')
    @app.post('/api/fx/preview',dependencies=[Depends(csrf)])
    def fx_preview(data:FxInput,uid=Depends(user)):
        q=preview(ctx.fx,data.source,data.amount)
        with Session.begin() as db:
            u=lock_user(db,uid); ws=wallets(db,u)
            q['balances_after']={c:w.balance for c,w in ws.items()}
            q['balances_after'][data.source]-=data.amount; q['balances_after'][q['target']]+=q['received']
        return q
    @app.get('/api/fx/share')
    def fx_share(source:Literal['USD','KRW'],percent:int=Query(ge=1,le=100),uid=Depends(user)):
        # The FX fee comes out of the amount sent, so a share of the balance is always exchangeable.
        if percent not in (5,10,25,50,100): raise HTTPException(422,'지원하지 않는 비율입니다.')
        with Session.begin() as db:
            u=lock_user(db,uid)
            balance=wallets(db,u)[source].balance
        return {'source':source,'percent':percent,'balance':balance,'amount':rounded(balance*Decimal(percent)/100,source)}
    @app.post('/api/fx/exchange',dependencies=[Depends(csrf)])
    def fx_exchange(data:FxOrder,uid=Depends(user)): return exchange(uid,data,ctx.fx)
    @app.get('/api/fx/history')
    def fx_history(uid=Depends(user)):
        with Session() as db:
            return [{'id':r.id,'source':r.source,'target':r.target,'amount':r.amount,'received':r.received,'fee':r.fee,'rate':r.rate,'rate_date':r.rate_date,'created_at':r.created_at} for r in db.scalars(select(FxTransaction).where(FxTransaction.user_id==uid).order_by(FxTransaction.id.desc()).limit(100))]

    @app.get('/api/company/{symbol}')
    def company(symbol:str,uid=Depends(user)):
        if not valid_symbol(symbol): raise HTTPException(422,'잘못된 종목코드입니다.')
        from .company import company_info
        return company_info(symbol,ctx.market)

    @app.get('/api/candles/{symbol}')
    def candles(symbol:str,range:Literal['1D','1W','3M','1Y','5Y','ALL']='1D',uid=Depends(user)):
        return provider(symbol).candles(symbol,range)
    @app.get('/api/market-status/{symbol}')
    def status(symbol:str,uid=Depends(user)): return provider(symbol).market_status()
    @app.get('/api/explore')
    def explore(market:Literal['US','KR']='US',asset:Literal['kr','us','kr_bond','us_bond','gold']|None=None,kind:Literal['volume','shares','up','down','popular']='volume',hours:Literal[1,24]=24,uid=Depends(user)):
        asset=asset or ('kr' if market=='KR' else 'us')
        market='KR' if asset in ('kr','kr_bond') else 'US'
        if kind=='popular':
            with Session() as db:
                query=select(PopularityEvent.symbol,func.count().label('score')).where(PopularityEvent.created_at>=datetime.now(timezone.utc)-timedelta(hours=hours))
                if asset!='gold': query=query.where(PopularityEvent.symbol.like('KR:%') if market=='KR' else ~PopularityEvent.symbol.like('KR:%'))
                counts=db.execute(query.group_by(PopularityEvent.symbol).order_by(func.count().desc(),PopularityEvent.symbol).limit(100)).all()
            rows=[]
            for symbol, score in counts:
                info=instrument(symbol)
                if info['category']!=asset: continue
                row=info|{'score':score,'market':market}
                try:
                    q=ctx.market.quote(symbol)
                    row|={'price':q.get('native_price',q['price']),'change_pct':q.get('change_pct'),
                          'volume':q.get('volume'),'turnover':q.get('turnover'),'data_time':datetime.fromtimestamp(q['timestamp'],timezone.utc).isoformat(),
                          'data_status':q.get('data_status','공급자 시세')+(' · 오래된 시세' if q.get('stale') else '')}
                except MarketError as exc:
                    row['data_status']=str(exc)
                rows.append(row)
                if len(rows)==100: break
            return market_result({'rows':rows, 'scope':f'{BRAND_NAME} 인기 · 최근 {hours}시간','notice':f'현재 집계 {len(rows)}개 · 사용자·종목·행동별 시간당 1회만 집계합니다.'},uid)
        if asset in ('kr_bond','us_bond','gold'):
            return market_result(curated_market_rows(ctx.market,asset,kind),uid)
        p=ctx.market.providers[market]
        try:
            # Provider results are cached. Never append UI notices onto that shared object.
            # kind 'volume' is the historical name of the turnover (거래대금) ranking.
            result=dict(p.volume_leaders() if kind=='volume' else p.movers(kind))
            result['rows']=[r for r in result['rows'] if instrument(r['symbol'])['category']==asset]
            result['rows']=result['rows'][:100]
            result['notice']=f"현재 공급자 제공 {len(result['rows'])}개 · 최대 100개 표시. " + result.get('notice','')
            return market_result(result,uid)
        except MarketError as exc: return market_result(curated_market_rows(ctx.market,asset,kind,exc),uid)
    @app.post('/api/popularity',dependencies=[Depends(csrf)])
    def track(data:EventInput,uid=Depends(user)):
        event(uid,data.symbol,data.kind); return {'ok':True}
    @app.get('/api/watchlist')
    def watchlist(uid=Depends(user)):
        with Session() as db: symbols=list(db.scalars(select(Watchlist.symbol).where(Watchlist.user_id==uid).order_by(Watchlist.created_at.desc())))
        rows=[]
        for symbol in symbols:
            try: rows.append(instrument(symbol)|{'quote':ctx.market.quote(symbol),'error':None})
            except MarketError as exc: rows.append(instrument(symbol)|{'quote':None,'error':str(exc)})
        return rows
    @app.post('/api/watchlist',dependencies=[Depends(csrf)])
    def watch_add(data:SymbolInput,uid=Depends(user)):
        with Session.begin() as db:
            lock_user(db,uid)
            if db.scalar(select(func.count()).select_from(Watchlist).where(Watchlist.user_id==uid))>=50: raise HTTPException(409,'관심종목은 최대 50개입니다.')
            db.execute(insert(Watchlist).values(user_id=uid,symbol=data.symbol,created_at=datetime.now(timezone.utc)).on_conflict_do_nothing())
        event(uid,data.symbol,'watch'); return {'ok':True}
    @app.delete('/api/watchlist/{symbol}',dependencies=[Depends(csrf)])
    def watch_remove(symbol:str,uid=Depends(user)):
        with Session.begin() as db: db.execute(delete(Watchlist).where(Watchlist.user_id==uid,Watchlist.symbol==symbol))
        return {'ok':True}

    @app.get('/api/admin')
    def admin_info(uid=Depends(admin)): return admin_overview(ctx.market,ctx.health)
    from .notices import install_notices
    install_notices(app,admin,csrf)
    @app.post('/api/admin/initial',dependencies=[Depends(csrf)])
    def set_initial(data:AmountInput,uid=Depends(admin)):
        set_initial_amount(uid,data.amount)
        return {'ok':True,'applies_to':'new accounts and explicitly reset accounts'}
    @app.post('/api/admin/users/{target}/active',dependencies=[Depends(csrf)])
    def set_active(target:int,data:ActiveInput,uid=Depends(admin)):
        if target==uid and not data.active: raise HTTPException(409,'자기 계정을 정지할 수 없습니다.')
        set_account_active(uid,target,data.active)
        return {'ok':True}
    @app.post('/api/admin/users/{target}/reset',dependencies=[Depends(csrf)])
    def reset(target:int,data:ResetInput,uid=Depends(admin)):
        season_reset(uid,target,data.label,ctx.fx.current_rate('USD','KRW'))
        return {'ok':True,'archived':True}
    from .admin_ops import install_admin_ops
    install_admin_ops(app,ctx,admin,csrf)

    @app.get('/api/admin/performance-snapshots')
    def snapshot_status(uid=Depends(admin)):
        from .performance_snapshots import status
        return status()
    @app.post('/api/admin/performance-snapshots/run',dependencies=[Depends(csrf)])
    def snapshot_run(uid=Depends(admin)):
        # Idempotent: accounts and benchmarks already stored today are skipped.
        from .performance_snapshots import capture_daily_snapshots, status
        return {'result':capture_daily_snapshots(ctx.market,ctx.fx,force=True)}|status()
    @app.get('/api/admin/archives')
    def archives(uid=Depends(admin)): return list_archives()

    @app.get('/api/limit-orders')
    def limits(uid=Depends(user)):
        with Session() as db:
            return [{'id':o.id,'symbol':o.symbol,'side':o.side,'quantity':o.quantity,'limit_price':o.limit_price if o.order_type=='limit' else None,'order_type':o.order_type,'use_max':o.use_max,'status':o.status,'reason':o.reason} for o in db.scalars(select(LimitOrder).where(LimitOrder.user_id==uid).order_by(LimitOrder.id.desc()).limit(100))]
    # New limit orders are no longer exposed. Existing records can still be
    # inspected/cancelled and the worker honours their original terms.
    @app.post('/api/limit-orders/{order_id}/cancel',dependencies=[Depends(csrf)])
    def limit_cancel(order_id:int,uid=Depends(user)):
        from .limits import cancel
        return cancel(uid,order_id)
