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
import uvicorn
uvicorn.run(main.app,host='0.0.0.0',port=8000)
