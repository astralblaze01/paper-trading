import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal as D
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import pytest
from fastapi import HTTPException
from sqlalchemy import select, func
from fastapi.testclient import TestClient
from test_service import database, client, seed, order, register, FakeMarket
from app import main
from app.db import Session, User, Wallet, Transaction, FxTransaction, Watchlist, PopularityEvent
from app.fx import exchange, preview, FxService
from app.money import costs, maximum
from app.trading import execute_order, preview_order
from app.routes import FxOrder
from app.market import MarketError


def fx_order(source='USD', amount='1000'):
    return FxOrder(source=source,amount=D(amount),request_id=uuid4())

def test_fx_bps_spread_both_directions():
    uid=seed()
    q=preview(main.fx,'USD',D('1000'))
    assert q['fee']==1
    assert q['applied_rate']==D('999.5')
    assert q['received']==998500
    exchange(uid,fx_order(),main.fx)
    with Session() as db:
        assert db.get(Wallet,(uid,'USD')).balance==99000
        assert db.get(Wallet,(uid,'KRW')).balance==998500
    result=exchange(uid,fx_order('KRW','100000'),main.fx)
    assert result['received']==D('99.8500')
    with Session() as db:
        assert db.get(Wallet,(uid,'USD')).balance==D('99099.8500')
        assert db.scalar(select(func.count()).select_from(FxTransaction))==2

def test_fx_overdraft_concurrent_and_idempotent():
    uid=seed()
    def run(_):
        try: exchange(uid,fx_order(amount='60000'),main.fx); return 200
        except HTTPException as exc: return exc.status_code
    with ThreadPoolExecutor(max_workers=3) as pool: results=list(pool.map(run,range(3)))
    assert sorted(results)==[200,409,409]
    data=fx_order(amount='100')
    a=exchange(uid,data,main.fx); b=exchange(uid,data,main.fx)
    assert a['id']==b['id'] and b['replayed']
    with Session() as db: assert db.get(Wallet,(uid,'USD')).balance==39900

def test_fx_stale_refused():
    class Old:
        def krw_to_usd(self): return D('.001'),(datetime.now(timezone.utc)-timedelta(days=8)).date().isoformat()
    with pytest.raises(MarketError): FxService(Old()).current_rate('USD','KRW')

def test_max_fee_edge_and_execute_authoritative(monkeypatch):
    monkeypatch.setenv('US_BUY_FEE_BPS','10')
    assert maximum('AAPL',D(250),D(10000))==39
    uid=seed()
    with Session.begin() as db: db.get(User,uid).cash=10000
    q=preview_order(uid,'AAPL','buy',1,FakeMarket())
    assert q['max_quantity']==99
    result=execute_order(uid,main.Order(symbol='AAPL',side='buy',quantity=1000000,use_max=True,request_id=uuid4()),FakeMarket())
    assert result['quantity']==99
    with Session() as db:
        assert db.get(Wallet,(uid,'USD')).balance==D('90.1')
        trade=db.scalar(select(Transaction))
        assert trade.fee==D('9.9') and trade.gross_amount==9900 and trade.net_amount==D('9909.9')
    monkeypatch.setenv('US_BUY_FEE_BPS','50')
    with Session() as db: assert db.scalar(select(Transaction)).fee==D('9.9')

def test_kr_wallet_cannot_spend_usd_and_currency_rejected():
    uid=seed(); market=FakeMarket()
    market.quote=lambda s:{'symbol':s,'price':D(70),'native_price':D(70000),'currency':'KRW','fx_rate':D('.001'),'timestamp':int(time.time()),'stale':False}
    with pytest.raises(HTTPException):execute_order(uid,order(symbol='KR:005930'),market)
    exchange(uid,fx_order(amount='100'),main.fx)
    execute_order(uid,order(symbol='KR:005930'),market)
    with Session() as db:
        assert db.get(Wallet,(uid,'USD')).balance==99900
        assert db.get(Wallet,(uid,'KRW')).balance==29850
    market.quote=lambda s:{'price':D(70),'currency':'USD','timestamp':int(time.time()),'stale':False}
    with pytest.raises(HTTPException):execute_order(uid,order(symbol='KR:005930'),market)

def test_concurrent_fx_and_buy_no_double_spend():
    uid=seed()
    def run(kind):
        try:
            if kind: exchange(uid,fx_order(amount='60000'),main.fx)
            else: execute_order(uid,order(quantity=600),FakeMarket())
            return 200
        except HTTPException as exc: return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(run,[0,1]))
    assert sorted(results)==[200,409]
    with Session() as db: assert db.get(Wallet,(uid,'USD')).balance==40000

def test_realized_fees_and_tax(monkeypatch):
    monkeypatch.setenv('US_BUY_FEE_BPS','10'); monkeypatch.setenv('US_SELL_FEE_BPS','20')
    uid=seed(); m=FakeMarket(); execute_order(uid,order(quantity=10),m)
    m.quote=lambda s:{'price':D(110),'timestamp':int(time.time()),'stale':False}
    execute_order(uid,order(side='sell',quantity=5),m)
    with Session() as db:
        trade=db.scalar(select(Transaction).where(Transaction.side=='sell'))
        assert trade.realized_pnl==D('48.4')

def test_watchlist_popularity_dedup_and_admin_forbidden(client):
    token=register(client); headers={'x-csrf-token':token}
    assert client.get('/api/admin').status_code==403
    assert client.post('/api/watchlist',headers=headers,json={'symbol':'AAPL'}).status_code==200
    assert client.get('/api/watchlist').json()[0]['symbol']=='AAPL'
    for _ in range(3): assert client.post('/api/popularity',headers=headers,json={'symbol':'AAPL','kind':'view'}).status_code==200
    with Session() as db: assert db.scalar(select(func.count()).select_from(PopularityEvent))==2
    assert client.get('/api/explore?kind=popular').json()['rows'][0]['score']==2
    with TestClient(main.app,base_url='https://testserver') as other:
        other_token=register(other,'bob')
        assert other.get('/api/watchlist').json()==[]
        other.delete('/api/watchlist/AAPL',headers={'x-csrf-token':other_token})
    assert len(client.get('/api/watchlist').json())==1

def test_order_share_rounding_fees_and_zero_quantity(client, monkeypatch):
    token=register(client)
    monkeypatch.setenv('US_BUY_FEE_BPS','10')
    with Session.begin() as db:
        uid=db.scalar(select(User.id).where(User.username=='alice'))
        db.get(Wallet,(uid,'USD')).balance=D('10000')
    main.market.quote=lambda s:{'price':D(250),'timestamp':int(time.time()),'stale':False}
    for share,expected in [(100,39),(50,19),(25,9),(10,3),(5,1)]:
        r=client.get(f'/api/order-preview?symbol=AAPL&side=buy&share={share}')
        assert r.status_code==200
        q=r.json()
        assert q['quantity']==expected and q['can_submit']
        assert D(str(q['net_amount']))<=10000
    assert client.get('/api/order-preview?symbol=AAPL&share=33').status_code==422
    assert client.post('/api/orders',headers={'x-csrf-token':token},json=order(quantity=3).model_dump(mode='json')).status_code==200
    q=client.get('/api/order-preview?symbol=AAPL&side=sell&share=5').json()
    assert q['quantity']==0 and q['holding_after']==3 and not q['can_submit']
    assert client.get('/api/order-preview?symbol=AAPL&side=sell&share=100').json()['quantity']==3
    invalid=order(side='sell').model_dump(mode='json')|{'quantity':0}
    assert client.post('/api/orders',headers={'x-csrf-token':token},json=invalid).status_code==422

def test_market_diagnostics_are_admin_only_and_cache_is_unchanged(client):
    register(client)
    cached={'rows':[{'symbol':'AAPL','name':'Apple','price':'100','currency':'USD','market':'US','data_time':'2026-09-24T00:00:00Z','data_status':'provider diagnostic'}],'source':'fixture','notice':''}
    class Shared:
        def volume_leaders(self): return cached
    main.market.providers={'US':Shared()}
    response=client.get('/api/explore?asset=us').json()
    assert 'data_time' not in response['rows'][0] and 'data_status' not in response['rows'][0]
    assert 'source' not in response
    assert cached['rows'][0]['data_status']=='provider diagnostic'
    with Session.begin() as db: db.scalar(select(User).where(User.username=='alice')).is_admin=True
    admin_response=client.get('/api/explore?asset=us').json()
    assert admin_response['rows'][0]['data_status']=='provider diagnostic'

def test_auth_bruteforce_limit(client):
    token=client.get('/api/session').json()['csrf']
    codes=[client.post('/api/login',headers={'x-csrf-token':token},json={'username':'nobody','password':'wrongpass'}).status_code for _ in range(22)]
    assert codes[-1]==429

def test_chart_ranges_errors_and_labeling(client,monkeypatch):
    from app.providers import USProvider,RANGES
    class Adapter:
        def get(self,path,params,ttl):
            return {'s':'ok','t':[int(time.time())-86400*6],'o':[100],'h':[110],'l':[90],'c':[101],'v':[1000]}
    p=USProvider(Adapter())
    main.market.providers={'US':p}
    register(client)
    for period in RANGES:
        r=client.get('/api/candles/AAPL?range='+period)
        assert r.status_code==200 and r.json()['stale'] is True
        assert r.json()['candles'][0]['close']==101
    assert client.get('/api/candles/AAPL?range=BAD').status_code==422
    assert client.get('/api/candles/BAD%27').status_code==422
    class Failing:
        def get(self,*args): raise MarketError('timeout or rate limit')
    main.market.providers={'US':USProvider(Failing())}
    assert client.get('/api/candles/AAPL').status_code==503

def test_limit_ownership_cancel_and_fill(client):
    from app.routes import LimitInput
    from app.limits import create,cancel,process
    from app.db import LimitOrder
    token=register(client)
    payload={'symbol':'AAPL','side':'buy','quantity':2,'limit_price':'101','request_id':str(uuid4())}
    assert client.post('/api/limit-orders',headers={'x-csrf-token':token},json=payload).status_code==405
    with Session() as db: uid=db.scalar(select(User.id).where(User.username=='alice'))
    oid=create(uid,LimitInput(**payload))['id']  # Existing order from before UI removal.
    with TestClient(main.app,base_url='https://testserver') as other:
        t=register(other,'bob')
        assert other.post(f'/api/limit-orders/{oid}/cancel',headers={'x-csrf-token':t},json={}).status_code==404
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(lambda _:process(FakeMarket()),range(2)))
    assert sum(results)==1
    with Session() as db:
        assert db.get(LimitOrder,oid).status=='filled'
        assert db.scalar(select(func.count()).select_from(Transaction))==1
    payload['request_id']=str(uuid4());payload['limit_price']='90'
    oid=create(uid,LimitInput(**payload))['id']
    assert process(FakeMarket())==0
    assert client.post(f'/api/limit-orders/{oid}/cancel',headers={'x-csrf-token':token},json={}).json()['status']=='cancelled'
    assert process(FakeMarket())==0

def test_weekly_krw_wallet_evaluation():
    from app.weekly import tick,next_run
    from app.db import WeeklyReport
    uid=seed()
    start=datetime.now(timezone.utc)
    assert tick(FakeMarket(),start,fx=main.fx)=='baseline'
    exchange(uid,fx_order(amount='1000'),main.fx)
    assert tick(FakeMarket(),next_run(start),fx=main.fx)=='published'
    with Session() as db:
        r=db.scalar(select(WeeklyReport))
        assert r.notes['base_currency']=='KRW'
        assert D(r.rows[0]['pnl'])==-1500  # FX fee + spread + KRW truncation

def test_admin_reset_archives_and_initial_settings(client):
    from app.db import SeasonArchive
    token=register(client)
    with Session.begin() as db:
        u=db.scalar(select(User).where(User.username=='alice'));u.is_admin=True;uid=u.id
    headers={'x-csrf-token':token}
    assert client.get('/api/admin').status_code==200
    assert client.post('/api/admin/initial',headers=headers,json={'amount':'50000'}).status_code==200
    assert client.post(f'/api/admin/users/{uid}/reset',headers=headers,json={'label':'test','confirmation':'NO'}).status_code==422
    assert client.post(f'/api/admin/users/{uid}/reset',headers=headers,json={'label':'test','confirmation':'RESET'}).status_code==200
    with Session() as db:
        assert db.get(Wallet,(uid,'USD')).balance==50000
        assert db.scalar(select(func.count()).select_from(SeasonArchive))==1

def test_internal_worker_not_public(client):
    assert client.post('/internal/jobs').status_code==403

def test_provider_timeout_and_candle_cache():
    import httpx
    from app.market import Finnhub
    from app.providers import USProvider
    f=Finnhub();f.key='test';f.client.close()
    f.client=httpx.Client(base_url='https://test',transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ReadTimeout('timeout'))))
    try:
        with pytest.raises(MarketError):USProvider(f).candles('AAPL','1D')
    finally:f.client.close()


def test_stale_us_market_order_is_rejected_instead_of_queued(client):
    from app.db import LimitOrder
    token=register(client)
    old={'symbol':'AAPL','price':D('100'),'timestamp':int(time.time())-86400,'stale':True}
    main.market.quote=lambda s:old
    request={'symbol':'AAPL','side':'buy','quantity':2,'request_id':str(uuid4())}
    headers={'x-csrf-token':token}
    preview=client.get('/api/order-preview?symbol=AAPL&side=buy&quantity=2')
    assert preview.status_code==200 and preview.json()['indicative_only']
    first=client.post('/api/orders',headers=headers,json=request)
    assert first.status_code==409
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(Transaction))==0
        assert db.scalar(select(func.count()).select_from(LimitOrder))==0


def test_us_delayed_quote_window():
    from app.trading import checked_quote
    m=FakeMarket();m.quote=lambda s:{'symbol':s,'price':D(100),'timestamp':int(time.time())-1200,'stale':False}
    checked_quote('AAPL',m)
    m.quote=lambda s:{'symbol':s,'price':D(100),'timestamp':int(time.time())-1900,'stale':False}
    with pytest.raises(HTTPException):checked_quote('AAPL',m)


def test_explore_bond_gold_quotes_and_honest_ranking_fallback(client):
    register(client)
    with Session.begin() as db:
        db.scalar(select(User).where(User.username=='alice')).is_admin=True
    class MissingRanks:
        def volume_leaders(self): raise MarketError('전체 시장 순위 키 필요')
        def movers(self, direction): raise MarketError('전체 시장 순위 키 필요')
    main.market.providers={'US':MissingRanks(),'KR':MissingRanks()}
    main.market.quote=lambda s:{'symbol':s,'price':D('100'),'native_price':D('70000') if s.startswith('KR:') else D('100'),
                                'change_pct':D('1.2'),'volume':None,'timestamp':int(time.time()),'stale':False,'data_status':'실제 시세 대역'}
    for asset,expected in [('kr_bond','KR:114260'),('us_bond','TLT'),('gold','GLD')]:
        r=client.get('/api/explore?asset='+asset+'&kind=up')
        assert r.status_code==200 and expected in {row['symbol'] for row in r.json()['rows']}
        assert all(row['price'] is not None and row['data_time'] for row in r.json()['rows'])
        assert '전체 시장 순위가 아닙니다' in r.json()['notice']
        if asset=='kr_bond': assert len(r.json()['rows'])>1
        if asset=='gold': assert {'GLD','KR:411060'} <= {row['symbol'] for row in r.json()['rows']}
    r=client.get('/api/explore?asset=us&kind=volume').json()
    assert '전체 시장 순위 키 필요' in r['notice']
    assert len(r['rows'])==3 and all(D(str(row['price']))==100 for row in r['rows'])
    assert '거래대금순으로 정렬할 수 없습니다' in r['notice']

def test_kis_trade_value_ranking_uses_provider_money_sort():
    from app.providers import KRProvider
    class Adapter:
        def get(self,path,tr_id,params,ttl,tr_cont=''):
            assert path.endswith('/volume-rank') and params['FID_BLNG_CLS_CODE']=='3'
            return {'output':[
                {'mksc_shrn_iscd':'005930','hts_kor_isnm':'삼성전자','stck_prpr':'100','prdy_ctrt':'1','acml_vol':'100','acml_tr_pbmn':'1000'},
                {'mksc_shrn_iscd':'000660','hts_kor_isnm':'SK하이닉스','stck_prpr':'200','prdy_ctrt':'2','acml_vol':'50','acml_tr_pbmn':'2000'}]}
    rows=KRProvider(Adapter()).volume_leaders()['rows']
    assert [r['symbol'] for r in rows]==['KR:000660','KR:005930']
    assert [r['turnover'] for r in rows]==['2000','1000']

def test_us_chart_uses_read_only_kis_fallback_when_finnhub_denies():
    from app.providers import USProvider
    from zoneinfo import ZoneInfo
    class Denied:
        def get(self,*args): raise MarketError('Finnhub candle 403')
    class Kis:
        configured=True
        cooldown=0
        def get(self,path,tr_id,params,ttl,tr_cont=''):
            assert params['EXCD']=='NAS' and params['SYMB']=='AAPL'
            today=datetime.now(ZoneInfo('America/New_York')).strftime('%Y%m%d')
            if path.endswith('inquire-time-itemchartprice'):
                return {'output2':[{'xymd':today,'xhms':'100000','open':'100','high':'102','low':'99','last':'101','evol':'50'}]}
            assert path.endswith('dailyprice')
            return {'output2':[{'xymd':today,'open':'100','high':'102','low':'99','clos':'101','tvol':'500'}]}
    provider=USProvider(Denied(),Kis())
    for period in ('1D','1W','3M','1Y','5Y','ALL'):
        result=provider.candles('AAPL',period)
        assert result['candles'] and result['candles'][0]['close']=='101'
        assert result['source'].startswith('KIS')

def test_cached_rank_notice_does_not_grow_on_refresh(client):
    register(client)
    cached={'rows':[{'symbol':'AAPL','name':'Apple','price':'100','change_pct':'1','volume':10,'market':'US','currency':'USD'}],
            'scope':'테스트 공급자','notice':'원본 문구'}
    class Shared:
        def volume_leaders(self): return cached
    main.market.providers={'US':Shared()}
    notices=[client.get('/api/explore?asset=us&kind=volume').json()['notice'] for _ in range(3)]
    assert len(set(notices))==1
    assert cached['notice']=='원본 문구'


def test_fx_conversion_reduces_equity_and_explains_currency_effect():
    from app.portfolio import portfolio
    uid=seed()
    before=portfolio(uid,FakeMarket(),main.fx)
    exchange(uid,fx_order(amount='50000'),main.fx)
    after=portfolio(uid,FakeMarket(),main.fx)
    assert after['equity'] < before['equity']
    assert after['return_pct'] < 0
    assert after['initial_fx_effect']==0
    assert after['pnl']==after['initial_fx_effect']+after['other_pnl']
    class HigherFX:
        def current_rate(self,source='USD',target='KRW'):
            return {'rate':D(1100),'date':'2026-09-24','stale':False}
    changed=portfolio(uid,FakeMarket(),HigherFX())
    assert changed['initial_fx_effect']==D(10000000)
    assert changed['pnl']==changed['initial_fx_effect']+changed['other_pnl']
