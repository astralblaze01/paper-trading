"""Test-only app, refuses to operate on the real database."""
import sys
sys.path.insert(0,'/srv')
import os
from decimal import Decimal
from datetime import datetime,timezone
from sqlalchemy import create_engine,URL,text
url=URL.create('postgresql+psycopg',username='paper',password=os.environ['DB_PASSWORD'],host='db',database='postgres')
with create_engine(url,isolation_level='AUTOCOMMIT').connect() as db:
    if not db.scalar(text("SELECT 1 FROM pg_database WHERE datname='paper_browser_test'")):db.execute(text('CREATE DATABASE paper_browser_test'))
os.environ['DATABASE_URL']=url.set(database='paper_browser_test').render_as_string(hide_password=False)
os.environ['WEEKLY_ENABLED']='false'
from app import main
assert main.engine.url.database=='paper_browser_test'
from app.instruments import discover,instrument
from app.providers import candle_result
class FixtureMarket:
    key='browser-fixture'
    client=None
    def __init__(self):self.client=self;self.providers={'US':self,'KR':self}
    def close(self):pass
    def status(self):return {'us':True,'kr':True}
    def search(self,q='',category='all'):return discover(q,category)
    def quote(self,s):
        import time
        c='KRW' if s.startswith('KR:') else 'USD';p=Decimal(70000) if c=='KRW' else Decimal(100)
        return instrument(s)|{'price':p/1000 if c=='KRW' else p,'native_price':p,'fx_rate':Decimal('.001') if c=='KRW' else Decimal(1),'fx_date':datetime.now(timezone.utc).date().isoformat(),'timestamp':int(time.time()),'stale':False,'change':-1 if s=='GLD' else 1,'change_pct':-1 if s=='GLD' else 1,'high':p+2,'low':p-2,'volume':12000,'turnover':p*12000,'data_status':'브라우저 테스트 전용 시세'}
    def candles(self,s,period):
        import time
        rows=[{'time':int(time.time())-(59-i)*300,'open':100+i/10,'high':102+i/10,'low':98+i/10,'close':101+i/10,'volume':1000+i*10} for i in range(60)]
        return candle_result(s,period,'5',rows,'테스트 대역')
    def market_status(self):return {'label':'정규장','timezone':'America/New_York','verified':True}
    def movers(self,direction):return {'rows':[instrument('AAPL')|{'market':'US','price':100,'change_pct':1,'volume':10000,'data_time':'2026-09-24T00:00:00Z','data_status':'브라우저 테스트 시세 진단'}],'scope':'테스트 전용','notice':'운영 공급자 아님'}
    def volume_leaders(self):
        from app.market import MarketError
        raise MarketError('브라우저 테스트: 순위 공급자 설정 필요')
# Company facts: fixed provider answers in the recorded Finnhub/KIS shapes.
# AAPL full, NVDA partial (loss, no book data), GLD an ETF, MSFT a provider outage.
class FixtureFinnhub:
    METRIC={'AAPL':{'marketCapitalization':3200000,'peTTM':28.4,'pbQuarterly':7.2,'roeTTM':31.5,'psTTM':8.6,'dividendYieldIndicatedAnnual':0.45},
            'NVDA':{'marketCapitalization':125400,'peTTM':-12.3,'psTTM':20.1}}
    def get(self,path,params,ttl):
        from app.market import MarketError
        if params['symbol']=='MSFT':raise MarketError('브라우저 테스트: 기업정보 공급자 응답 없음')
        return {'metric':self.METRIC.get(params['symbol'],{})} if path=='/stock/metric' else {}
class FixtureKIS:
    PRICE={'005930':{'stck_prpr':'70000','hts_avls':'4500000','per':'14.20','pbr':'1.30'},
           '114260':{'stck_prpr':'70000','hts_avls':'52000','per':'0.00','pbr':'0.00'}}
    RATIO={'005930':[{'stac_yymm':'202606','roe_val':'20.00','sps':'40000'},{'stac_yymm':'202512','roe_val':'9.10','sps':'35000'},{'stac_yymm':'202412','roe_val':'8.00','sps':'30000'}]}
    def get(self,path,tr_id,params,ttl=15,tr_cont=''):
        code=params.get('FID_INPUT_ISCD') or params.get('fid_input_iscd') or params.get('PDNO') or params.get('SHT_CD')
        if path.endswith('inquire-price'):return {'output':self.PRICE.get(code,{})}
        if path.endswith('financial-ratio'):return {'output':self.RATIO.get(code,[])}
        if path.endswith('dividend'):return {'output1':[]}
        return {'output':{}}
FixtureMarket.us=FixtureFinnhub();FixtureMarket.kr=FixtureKIS()
class FixtureFX:
    def current_rate(self,source='USD',target='KRW'):return {'source':source,'target':target,'rate':Decimal(1000) if source=='USD' else Decimal('.001'),'date':datetime.now(timezone.utc).date().isoformat(),'stale':False}
main.market=FixtureMarket();main.fx=FixtureFX()
# Fixture administrator exists only in the explicitly isolated browser DB.
from app.db import Base,Session,User
from app.migrations import migrate
from sqlalchemy import select
Base.metadata.create_all(main.engine)
migrate(main.engine)
with Session.begin() as db:
    admin=db.scalar(select(User).where(User.username=='browser_admin'))
    if admin is None:
        db.add(User(username='browser_admin',password_hash=main.hasher.hash('browser-fixture-password'),is_admin=True))
# Test-only deterministic publisher; no upstream provider is ever called here.
if os.getenv('QUOTE_SSE_ENABLED') == 'true':
    import asyncio
    from contextlib import asynccontextmanager
    from fastapi import Depends, HTTPException
    from app.redis_cache import redis_cache
    from app.instruments import valid_symbol
    original_lifespan = main.app.router.lifespan_context
    fixture_market = main.market
    overrides = {}
    worker_paused = False
    original_quote = fixture_market.quote

    def fixture_quote(symbol):
        cached = redis_cache.get_json(f'market:price:{symbol}')
        if cached:
            from app.quote_data import normalize_quote
            return normalize_quote(symbol, cached)
        return original_quote(symbol)
    fixture_market.quote = fixture_quote

    def publish(symbol, price=None):
        import time
        q = original_quote(symbol)
        if price is not None:
            overrides[symbol] = Decimal(str(price))
        if symbol in overrides:
            q['native_price'] = overrides[symbol]
            q['price'] = overrides[symbol] * q['fx_rate']
        q['_cached_at'] = time.time()
        assert redis_cache.store_quote(symbol, q, 45)

    @main.app.post('/internal/test-quote/{symbol}')
    def inject(symbol: str, price: str, uid=Depends(main.current_user)):
        if not valid_symbol(symbol):
            raise HTTPException(422)
        publish(symbol, price)
        return {'ok': True}

    @main.app.post('/internal/test-worker')
    def pause_worker(paused: bool, uid=Depends(main.current_user)):
        global worker_paused
        worker_paused = paused
        return {'ok': True}

    async def fixture_worker():
        while True:
            for symbol in ([] if worker_paused else redis_cache.requested_symbols()):
                publish(symbol)
            await asyncio.sleep(.2)

    @asynccontextmanager
    async def fixture_lifespan(app):
        async with original_lifespan(app):
            task = asyncio.create_task(fixture_worker())
            try:
                yield
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    main.app.router.lifespan_context = fixture_lifespan

import uvicorn
uvicorn.run(main.app,host='0.0.0.0',port=8000)
