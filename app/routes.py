import threading
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
from .instruments import SYMBOL_PATTERN, valid_symbol, instrument, CATALOG, DIVIDEND_SYMBOLS, market_of
from .market import MarketError, QuotePending
from .fx import preview, exchange
from .money import MAX_ORDER_QUANTITY, wallets, rounded
from .trading import preview_order
from .admin_ops import admin_overview, set_initial_amount, set_account_active, season_reset, list_archives, reset_password
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
    # Where the price must be to fill: at or 'below' / at or 'above' limit_price.
    trigger: Literal['below','above']
    request_id: UUID

class ResetInput(Strict):
    confirmation: Literal['RESET']
    label: str=Field(min_length=1,max_length=80)
class AmountInput(Strict): amount: Decimal=Field(ge=1,le=1000000000,max_digits=14,decimal_places=4)
class ActiveInput(Strict): active: bool


def event(uid,symbol,kind):
    with Session.begin() as db:
        db.execute(insert(PopularityEvent).values(user_id=uid,symbol=symbol,kind=kind,bucket=int(time.time())//3600,created_at=datetime.now(timezone.utc)).on_conflict_do_nothing())


def quote_fields(q):
    """The list-row columns (현재가, 등락률, 거래량, 거래대금) of one quote."""
    return {'price':q.get('native_price',q['price']), 'change_pct':q.get('change_pct'),
            'volume':q.get('volume'), 'turnover':q.get('turnover'), 'data_time':datetime.fromtimestamp(q['timestamp'],timezone.utc).isoformat(),
            'data_status':q.get('data_status','공급자 시세')+(' · 오래된 시세' if q.get('stale') else '')}

def quotes_for(market, symbols):
    """{symbol: quote or MarketError}. From the worker cache all misses share one wait
    (MultiMarket.quotes) instead of up to 3 s each, one symbol after another."""
    if hasattr(market,'quotes'): return market.quotes(list(symbols))
    out={}
    for symbol in symbols:
        try: out[symbol]=market.quote(symbol)
        except MarketError as exc: out[symbol]=exc
    return out

# A yield moves with the price, but slowly; one provider round per symbol every few hours is enough.
# A failed lookup is retried sooner, so a provider hiccup does not hide a yield for hours.
DIVIDEND_YIELD_TTL=6*3600; DIVIDEND_RETRY_TTL=600
dividend_yields={}           # symbol -> (expires at, dividend_info result)
dividend_refresh=None        # the background thread looking yields up, while it runs
dividend_lock=threading.Lock()

def refresh_dividends(market, symbols):
    """Look the yields up one symbol after another. Cold, that is about a minute of provider calls."""
    from .company import dividend_info
    for symbol in symbols:
        info=dividend_info(symbol,market)
        ttl=DIVIDEND_RETRY_TTL if info.get('status')=='unavailable' else DIVIDEND_YIELD_TTL
        dividend_yields[symbol]=(time.monotonic()+ttl,info)

def known_dividends(market):
    """{symbol: (yield or None, looked up yet)} from what is known now, never waiting on a provider.

    Missing or expired symbols are looked up by one background thread; an expired
    yield is still shown until its new one arrives."""
    global dividend_refresh
    now=time.monotonic()
    due=[s for s in DIVIDEND_SYMBOLS if s not in dividend_yields or dividend_yields[s][0]<=now]
    with dividend_lock:
        if due and not (dividend_refresh and dividend_refresh.is_alive()):
            dividend_refresh=threading.Thread(target=refresh_dividends,args=(market,due),daemon=True,name='dividend-yields')
            dividend_refresh.start()
    known=dict(dividend_yields)  # one snapshot, while the thread keeps adding
    return {s:(known[s][1].get('yield'),True) if s in known else (None,False) for s in DIVIDEND_SYMBOLS}

def dividend_rows(market):
    """The 배당주 list: DIVIDEND_SYMBOLS with live quotes, highest trailing dividend yield first."""
    yields=known_dividends(market)
    quotes=quotes_for(market,DIVIDEND_SYMBOLS)
    rows=[]
    for symbol in DIVIDEND_SYMBOLS:
        row=instrument(symbol) | {'market':market_of(symbol)}
        q=quotes[symbol]
        if isinstance(q,MarketError): row |= {'price':None,'change_pct':None,'volume':None,'turnover':None,'data_time':None,'data_status':str(q)}
        else: row |= quote_fields(q)
        value,looked_up=yields[symbol]
        # Not looked up yet (the page shows 확인 중), as opposed to looked up and unknown (—).
        row['dividend_yield']=value; row['dividend_pending']=not looked_up
        rows.append(row)
    # Unknown yields go last; ties keep the list order.
    rows.sort(key=lambda r:(r['dividend_yield'] is None,-(r['dividend_yield'] or 0)))
    known=sum(r['dividend_yield'] is not None for r in rows); pending=sum(r['dividend_pending'] for r in rows)
    notice=f'최근 12개월 배당 기준 수익률 · 확인된 종목 {known}개 (시장 전체 순위가 아닙니다)'
    if pending: notice+=f' · {pending}개 종목의 배당수익률을 확인하는 중입니다'
    return {'rows':rows,'scope':f'대표 배당주·배당 ETF {len(rows)}개 · 배당수익률 높은 순','notice':notice}

# Search results carry no prices; the list asks for the first few rows' quotes
# in small batches so one search never fans out into dozens of provider calls.
SEARCH_QUOTE_LIMIT=5

def curated_market_rows(market, asset, kind, unavailable=None):
    """Actual quotes from a small disclosed catalog, never a claimed full-market ranking."""
    rows=[]; failures=[]
    symbols=[symbol for symbol, _, category, _ in CATALOG if category==asset]
    quotes=quotes_for(market,symbols)
    for symbol in symbols:
        row=instrument(symbol) | {'market':market_of(symbol)}
        q=quotes[symbol]
        if isinstance(q,MarketError):
            failures.append(str(q))
            row |= {'price':None,'change_pct':None,'volume':None,'turnover':None,'data_time':None,'data_status':str(q)}
        else: row |= quote_fields(q)
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
            # The popular and dividend lists carry this service's own wording, not provider notes.
            elif '인기' not in result.get('scope','') and '배당수익률' not in result.get('scope',''):
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
    @app.get('/api/popular')
    def popular(hours:int=Query(1),uid=Depends(user)):
        if hours not in (1,24): raise HTTPException(422,'hours는 1 또는 24입니다.')
        """The five most-viewed or traded stocks across all markets, for the 시장 sidebar (no quote calls)."""
        with Session() as db:
            rows=db.execute(select(PopularityEvent.symbol,func.count().label('score'))
                            .where(PopularityEvent.created_at>=datetime.now(timezone.utc)-timedelta(hours=hours))
                            .group_by(PopularityEvent.symbol).order_by(func.count().desc(),PopularityEvent.symbol).limit(5)).all()
        return {'hours':hours,'rows':[{'symbol':symbol,'name':instrument(symbol)['name'],'score':score} for symbol,score in rows]}
    @app.get('/api/explore')
    def explore(market:Literal['US','KR']='US',asset:Literal['kr','us','kr_bond','us_bond','gold','dividend']|None=None,kind:Literal['volume','shares','up','down','popular']='volume',hours:Literal[1,24]=24,uid=Depends(user)):
        asset=asset or ('kr' if market=='KR' else 'us')
        if asset=='dividend': return market_result(dividend_rows(ctx.market),uid)
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
                rows.append(info|{'score':score,'market':market})
                if len(rows)==100: break
            quotes=quotes_for(ctx.market,[row['symbol'] for row in rows])
            for row in rows:
                q=quotes[row['symbol']]
                if isinstance(q,MarketError): row['data_status']=str(q)
                else: row|=quote_fields(q)
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
    @app.get('/api/search/quotes')
    def search_quotes(symbols:str=Query('',max_length=200),uid=Depends(user)):
        """Quotes for rows of a search result, which lists instruments without prices."""
        wanted=list(dict.fromkeys(s for s in symbols.split(',') if s))
        if len(wanted)>SEARCH_QUOTE_LIMIT: raise HTTPException(422,f'한 번에 최대 {SEARCH_QUOTE_LIMIT}개 종목입니다.')
        if not all(valid_symbol(s) for s in wanted): raise HTTPException(422,'잘못된 종목 코드입니다.')
        results=quotes_for(ctx.market,wanted)
        rows=[]
        for symbol in wanted:
            row={'symbol':symbol};q=results[symbol]
            if isinstance(q,MarketError):
                row|={'price':None,'change_pct':None,'volume':None,'turnover':None,'data_time':None,'data_status':str(q)}
                # Asked of the collector but not stored yet: the page asks again shortly.
                if isinstance(q,QuotePending): row['pending']=True
            else: row|=quote_fields(q)
            rows.append(row)
        return market_result({'rows':rows},uid)['rows']
    @app.post('/api/popularity',dependencies=[Depends(csrf)])
    def track(data:EventInput,uid=Depends(user)):
        event(uid,data.symbol,data.kind); return {'ok':True}
    @app.get('/api/watchlist')
    def watchlist(uid=Depends(user)):
        with Session() as db: symbols=list(db.scalars(select(Watchlist.symbol).where(Watchlist.user_id==uid).order_by(Watchlist.created_at.desc())))
        quotes=quotes_for(ctx.market,symbols)
        return [instrument(s)|({'quote':None,'error':str(quotes[s])} if isinstance(quotes[s],MarketError) else {'quote':quotes[s],'error':None})
                for s in symbols]
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
    @app.post('/api/admin/users/{target}/password',dependencies=[Depends(csrf)])
    def admin_reset_password(target:int,uid=Depends(admin)):
        if target==uid: raise HTTPException(409,'자기 비밀번호는 계정 설정에서 변경하세요.')
        return reset_password(uid,target,ctx.hasher)
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
            from .limits import public
            return [public(o) for o in db.scalars(select(LimitOrder).where(LimitOrder.user_id==uid).order_by(LimitOrder.id.desc()).limit(100))]
    @app.post('/api/limit-orders',dependencies=[Depends(csrf)])
    def limit_create(data:LimitInput,uid=Depends(user)):
        from .limits import create
        # Only a symbol the market can price can ever trigger.
        try: ctx.market.quote(data.symbol)
        except MarketError: raise HTTPException(422,'시세를 확인할 수 없는 종목은 예약할 수 없습니다.')
        return create(uid,data)
    @app.post('/api/limit-orders/{order_id}/cancel',dependencies=[Depends(csrf)])
    def limit_cancel(order_id:int,uid=Depends(user)):
        from .limits import cancel
        return cancel(uid,order_id)
