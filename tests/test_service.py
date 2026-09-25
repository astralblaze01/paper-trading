"""Integration suite: only run against a dedicated PostgreSQL test database."""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from app import main
from app.db import Base, engine, Session, User, Position, Transaction
from app.market import MarketError, Finnhub
from app.trading import execute_order

class FakeMarket:
    def status(self):
        return {"us": True, "kr": True}

    key = 'test'
    def quote(self, symbol):
        return {'symbol': symbol, 'price': Decimal('100'), 'timestamp': int(time.time()), 'stale': False}

@pytest.fixture(autouse=True)
def database(monkeypatch):
    monkeypatch.setenv('WEEKLY_ENABLED', 'false')
    from app.security import limiter
    limiter.clear()
    class FixedFX:
        def current_rate(self, source='USD', target='KRW'):
            from datetime import datetime, timezone
            return {'source':source,'target':target,'rate':Decimal(1000) if source=='USD' else Decimal('.001'),'date':datetime.now(timezone.utc).date().isoformat(),'stale':False}
    monkeypatch.setattr(main,'fx',FixedFX())
    for name in ('US_BUY_FEE_BPS','US_SELL_FEE_BPS','KR_BUY_FEE_BPS','KR_SELL_FEE_BPS','KR_SELL_TAX_BPS'):
        monkeypatch.setenv(name,'0')
    assert engine.url.database == 'paper_test', 'Refusing to touch a non-test database'
    from sqlalchemy import text
    with engine.begin() as db: db.execute(text('DROP TABLE IF EXISTS schema_migrations'))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, 'market', FakeMarket())
    yield

@pytest.fixture
def client():
    # Skip application lifespan: schema is initialized by database fixture.
    with TestClient(main.app,base_url='https://testserver') as c:
        yield c

# Fake adapter needs a client close hook for application shutdown.
FakeMarket.client = type('Client', (), {'close': lambda self: None})()

def register(c, name='alice'):
    """Create an account, then sign in: registration itself does not log in."""
    token = c.get('/api/session').json()['csrf']
    credentials = {'username': name, 'password': 'a-secure-password-123'}
    r = c.post('/api/register', headers={'x-csrf-token': token}, json=credentials | {'password_confirm': credentials['password']})
    assert r.status_code == 200, r.text
    assert c.post('/api/login', headers={'x-csrf-token': token}, json=credentials).status_code == 200
    return c.get('/api/session').json()['csrf']

def order(**changes):
    return main.Order(**({'symbol': 'AAPL', 'side': 'buy', 'quantity': 1, 'request_id': uuid4()} | changes))

def seed():
    with Session.begin() as db:
        u=User(username='test', password_hash='not-used')
        db.add(u)
        db.flush()
        return u.id

def test_auth_csrf_and_isolation(client):
    assert client.get('/api/portfolio').status_code == 401
    token = register(client)
    assert client.post('/api/orders', json=order().model_dump(mode='json')).status_code == 403
    payload=order(quantity=4).model_dump(mode='json')
    assert client.post('/api/orders', headers={'x-csrf-token': token}, json=payload).status_code == 200
    p=client.get('/api/portfolio').json()
    assert float(p['cash']) == 99600
    assert float(p['equity']) == 100000000  # KRW base currency
    with TestClient(main.app,base_url='https://testserver') as other:
        other_token=register(other, 'bob')
        assert other.get('/api/portfolio').json()['positions'] == []
        assert other.get('/api/transactions').json() == []
        payload['user_id']=1
        assert other.post('/api/orders', headers={'x-csrf-token': other_token}, json=payload).status_code == 422
        payload.pop('user_id')
        payload['side']='sell'
        assert other.post('/api/orders', headers={'x-csrf-token': other_token}, json=payload).status_code == 409
    with Session() as db:
        assert db.scalar(select(User).where(User.username=='alice')).password_hash.startswith('$argon2id$')
    assert client.post('/api/logout', headers={'x-csrf-token': token}, json={}).status_code==200
    assert client.get('/api/portfolio').status_code==401
    token=client.get('/api/session').json()['csrf']
    assert client.post('/api/login', headers={'x-csrf-token':token}, json={'username':'alice','password':'incorrect-password'}).status_code==401
    assert client.post('/api/login', headers={'x-csrf-token':token}, json={'username':'ALICE','password':'a-secure-password-123'}).status_code==200

def test_korean_username_registration_and_login(client):
    token=register(client,'홍길동_01')
    assert client.post('/api/logout',headers={'x-csrf-token':token},json={}).status_code==200
    token=client.get('/api/session').json()['csrf']
    assert client.post('/api/login',headers={'x-csrf-token':token},json={'username':'홍길동_01','password':'a-secure-password-123'}).status_code==200

def test_weighted_average_and_realized_return():
    uid=seed()
    market=FakeMarket()
    execute_order(uid, order(quantity=10), market)
    market.quote=lambda s: {'symbol':s,'price':Decimal('200'),'timestamp':int(time.time()),'stale':False}
    execute_order(uid, order(quantity=10), market)
    execute_order(uid, order(side='sell',quantity=5), market)
    with Session() as db:
        p=db.get(Position,(uid,'AAPL'))
        u=db.get(User,uid)
        assert p.quantity==15 and p.average_cost==150
        assert u.cash==98000
        assert db.scalar(select(func.count()).select_from(Transaction))==3
    from app.portfolio import portfolio
    v=portfolio(uid,market,main.fx)  # KRW base at the fixture's 1000 KRW/USD
    assert v['equity_usd']==101000 and v['equity']==101000000 and v['return_pct']==1
    assert [(r['quantity'],r['average_cost'],r['value']) for r in v['positions']]==[(15,150,3000)]

def test_concurrent_buys_prevent_overdraft():
    uid=seed()
    def buy(_):
        try: execute_order(uid,order(quantity=600),FakeMarket()); return 200
        except HTTPException as e: return e.status_code
    with ThreadPoolExecutor(max_workers=4) as pool: results=list(pool.map(buy,range(4)))
    assert sorted(results)==[200,409,409,409]
    with Session() as db:
        assert db.get(User,uid).cash==40000
        assert db.get(Position,(uid,'AAPL')).quantity==600

def test_concurrent_sells_prevent_shorting():
    uid=seed()
    execute_order(uid,order(quantity=10),FakeMarket())
    def sell(_):
        try: execute_order(uid,order(side='sell',quantity=7),FakeMarket()); return 200
        except HTTPException as e: return e.status_code
    with ThreadPoolExecutor(max_workers=3) as pool: results=list(pool.map(sell,range(3)))
    assert sorted(results)==[200,409,409]
    with Session() as db: assert db.get(Position,(uid,'AAPL')).quantity==3

def test_concurrent_idempotency():
    uid=seed()
    o=order(quantity=3)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:execute_order(uid,o,FakeMarket()),range(4)))
    assert len({r['id'] for r in results})==1
    with Session() as db:
        assert db.get(User,uid).cash==99700
        assert db.scalar(select(func.count()).select_from(Transaction))==1
    with pytest.raises(HTTPException):execute_order(uid,order(quantity=4,request_id=o.request_id),FakeMarket())

def test_stale_and_provider_failure_rollback():
    uid=seed()
    market=FakeMarket()
    market.quote=lambda s:{'stale':True}
    with pytest.raises(HTTPException):execute_order(uid,order(),market)
    def fail(s):raise MarketError('offline')
    market.quote=fail
    with pytest.raises(MarketError):execute_order(uid,order(),market)
    with Session() as db:
        assert db.get(User,uid).cash==100000
        assert db.scalar(select(func.count()).select_from(Transaction))==0

def test_invalid_input(client):
    token=register(client)
    for quantity in [0,-1,1.5,1000001,True]:
        payload={'symbol':'AAPL','side':'buy','quantity':quantity,'request_id':str(uuid4())}
        assert client.post('/api/orders',headers={'x-csrf-token':token},json=payload).status_code==422
    assert client.get('/api/quote/AAPL%27').status_code==422

def test_provider_cache_and_rate_limit():
    import httpx
    m=Finnhub();m.key='test'
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json={'c':100,'t':int(time.time())})
    m.client.close()
    m.client=httpx.Client(base_url='https://test',transport=httpx.MockTransport(handler))
    assert m.quote('AAPL')['price']==100
    m.quote('AAPL')
    assert len(calls)==1
    m.client.close()
    m.client=httpx.Client(base_url='https://test',transport=httpx.MockTransport(lambda r:httpx.Response(429)))
    with pytest.raises(MarketError):m.quote('MSFT')
    with pytest.raises(MarketError):m.quote('TSLA')
    m.client.close()

def test_ranking(client):
    register(client)
    r=client.get('/api/ranking').json()
    assert r['rows'][0]['username']=='alice'
    assert float(r['rows'][0]['return_pct'])==0
    assert r['base_currency']=='USD' and Decimal(str(r['rows'][0]['equity_usd']))==100000
    assert r['refresh_interval_seconds']==10
    assert r['return_basis']=='초기 KRW 평가액 대비 (외부 입출금 반영)'
    assert r['next_refresh_at']

def test_ranking_keeps_last_snapshot_when_all_markets_are_closed(client, monkeypatch):
    register(client)
    class Closed:
        def market_status(self):
            return {'label':'휴장','timezone':'Asia/Seoul','verified':True}
    main.market.providers={'KR':Closed(),'US':Closed()}
    main._ranking_cache.clear()
    first=client.get('/api/ranking').json()
    assert first['market_open'] is False
    def should_not_revalue(*args, **kwargs):
        raise AssertionError('closed market must use the cached snapshot')
    monkeypatch.setattr(main, 'wallet_portfolio', should_not_revalue)
    second=client.get('/api/ranking').json()
    assert second['rows']==first['rows']
    assert second['refreshed'] is False
    assert second['market_open'] is False

def test_ranking_ten_second_boundary_and_last_good_snapshot(client,monkeypatch):
    from datetime import datetime, timezone, timedelta
    register(client)
    stamp=datetime(2026,9,24,0,0,19,tzinfo=timezone.utc)
    assert main._ranking_bucket(stamp).second==10
    assert main._next_ranking_boundary(stamp).second==20
    first=client.get('/api/ranking').json()
    main._ranking_cache[main.market]['bucket']-=timedelta(seconds=10)
    monkeypatch.setattr(main,'wallet_portfolio',lambda *args:{'return_pct':None})
    failed=client.get('/api/ranking').json()
    assert failed['rows']==first['rows'] and failed['updated_at']==first['updated_at']
    assert failed['stale'] and failed['incomplete']
    with Session.begin() as db: db.scalar(select(User).where(User.username=='alice')).is_admin=True
    assert not client.get('/api/ranking').json()['rows']

def test_registration_validation_and_separate_login(client):
    token = client.get('/api/session').json()['csrf']
    headers = {'x-csrf-token': token}
    def signup(username, password, confirm=None):
        return client.post('/api/register', headers=headers, json={'username': username, 'password': password, 'password_confirm': password if confirm is None else confirm})
    assert signup('shortpass', '1234567').status_code == 422
    assert signup('', 'abcd1234').status_code == 422
    assert signup('emptypass', '').status_code == 422
    mismatch = signup('mismatch', 'abcd1234', 'abcd12345')
    assert mismatch.status_code == 422 and mismatch.json()['detail'] == '비밀번호가 일치하지 않습니다.'
    assert client.post('/api/register', headers=headers, json={'username': 'noconfirm', 'password': 'abcd1234'}).status_code == 422
    with Session() as db: assert not db.scalar(select(func.count()).select_from(User))
    assert signup('eightpass', 'abcd1234').json() == {'ok': True, 'username': 'eightpass'}
    # A new account signs in from the start page; registration itself does not log in.
    assert client.get('/api/session').json()['username'] is None
    assert signup('EightPass', 'abcd1234').status_code == 409
    with Session() as db:
        user = db.scalar(select(User).where(User.username == 'eightpass'))
        assert user.password_hash.startswith('$argon2') and 'abcd1234' not in user.password_hash
    assert client.post('/api/login', headers=headers, json={'username': 'eightpass', 'password': 'abcd1234'}).status_code == 200
    assert client.get('/api/session').json()['username'] == 'eightpass'

def test_korean_trade_stores_fx_and_preserves_usd_account():
    uid = seed()
    market = FakeMarket()
    market.quote = lambda s: {'symbol': s, 'price': Decimal('50'), 'native_price': Decimal('70000'), 'currency': 'KRW', 'fx_rate': Decimal(1) / Decimal(1400), 'fx_date': '2026-09-23', 'timestamp': int(time.time()), 'stale': False}
    from app.db import Wallet
    with Session.begin() as db:
        db.add(Wallet(user_id=uid,currency='KRW',balance=Decimal(700000)))
    execute_order(uid, order(symbol='KR:005930', quantity=10), market)
    execute_order(uid, order(symbol='KR:005930', side='sell', quantity=4), market)
    with Session() as db:
        assert db.get(User, uid).cash == 100000
        assert db.get(Wallet,(uid,'KRW')).balance == 280000
        position = db.get(Position, (uid, 'KR:005930'))
        assert position.quantity == 6 and position.average_cost == 50
        trades = list(db.scalars(select(Transaction)))
        assert all(t.currency == 'KRW' and t.native_price == 70000 and t.fx_date == '2026-09-23' for t in trades)
        assert position.native_average_cost == 70000

def test_migration_preserves_legacy_trades():
    from sqlalchemy import text
    from app.migrations import migrate
    uid = seed()
    execute_order(uid, order(quantity=2), FakeMarket())
    with engine.begin() as db:
        for column in ('currency', 'native_price', 'fx_rate', 'fx_date'):
            db.execute(text('ALTER TABLE transactions DROP COLUMN ' + column))
    migrate(engine)
    migrate(engine)
    with Session() as db:
        trade = db.scalar(select(Transaction))
        assert trade.currency == 'USD' and trade.native_price == trade.price == 100
        assert trade.fx_rate == 1
        assert db.get(User, uid).cash == 99800
        assert db.get(Position, (uid, 'AAPL')).quantity == 2

def test_multimarket_discovery_and_normalization(monkeypatch):
    from app.multi_market import MultiMarket
    from app.market import MarketError
    m = MultiMarket()
    try:
        assert m.search('', 'us_bond')[0]['symbol'] == 'SHY'
        assert m.search('', 'kr_bond')[0]['symbol'] == 'KR:114260'
        assert m.search('', 'gold')[0]['symbol'] == 'GLD'
        assert m.search('삼성', 'kr')[0]['currency'] == 'KRW'
        assert m.search('005930', 'kr')[0]['symbol'] == 'KR:005930'
        monkeypatch.setattr(m.kr, 'quote', lambda s: {'symbol': s, 'price': Decimal(70000), 'timestamp': int(time.time()), 'stale': False})
        monkeypatch.setattr(m.fx, 'krw_to_usd', lambda: (Decimal(1) / Decimal(1400), '2026-09-23'))
        q = m.quote('KR:005930')
        assert q['price'] == 50 and q['native_price'] == 70000 and q['currency'] == 'KRW'
        with pytest.raises(MarketError): m.quote('KR:005930/../orders')
    finally: m.close()

def test_korean_master_parser_supports_name_and_code_search():
    from app.kr_symbols import parse_master
    line=('005930'.ljust(9)+'STD-CODE-000'.ljust(12)+'삼성전자'.ljust(40)).encode('euc-kr')
    rows=parse_master(line+b'\n','kospi')
    assert rows==[{'symbol':'KR:005930','name':'삼성전자','category':'kr','currency':'KRW','exchange':'kospi'}]

def test_kis_read_only_timestamp_and_cache():
    import httpx
    from datetime import datetime, timedelta
    from app.multi_market import KoreaPrices, SEOUL
    m = KoreaPrices()
    m.configured = True
    m.key = 'test'; m.secret = 'test'
    from app.redis_cache import redis_cache
    redis_cache.delete(m._token_key())
    m.client.close()
    calls = []
    stamp = datetime.now(SEOUL) - timedelta(minutes=1)
    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == '/oauth2/tokenP':
            return httpx.Response(200, json={'access_token': 'mock-token', 'expires_in': 86400})
        assert request.method == 'GET'
        if request.url.path.endswith('/inquire-price'):  # capability: KRX and NXT listing
            return httpx.Response(200, json={'rt_cd': '0', 'output': {'stck_sdpr': '69000', 'rprs_mrkt_kor_name': 'KOSPI200', 'bstp_kor_isnm': '전기·전자'}})
        assert request.url.path == m.PATH
        assert request.url.params['FID_COND_MRKT_DIV_CODE'] == 'UN'  # unified KRX+NXT
        filler = datetime.now(SEOUL).replace(second=0)
        # A later zero-volume bar only repeats the price; it is not a trade.
        return httpx.Response(200, json={'rt_cd': '0', 'output1': {'hts_kor_isnm': '삼성전자','acml_tr_pbmn':'140000000'}, 'output2': [
            {'stck_bsop_date': filler.strftime('%Y%m%d'), 'stck_cntg_hour': filler.strftime('%H%M%S'), 'stck_prpr': '70000', 'cntg_vol': '0'},
            {'stck_bsop_date': stamp.strftime('%Y%m%d'), 'stck_cntg_hour': stamp.strftime('%H%M%S'), 'stck_prpr': '70000', 'cntg_vol': '12'}]})
    m.client = httpx.Client(base_url='https://test', transport=httpx.MockTransport(handler))
    try:
        q = m.quote('KR:005930')
        assert q['price'] == 70000 and not q['stale']
        assert q['timestamp'] == int(stamp.timestamp())
        assert q['turnover'] == '140000000' and q['venue'] == 'UNIFIED'
        assert q['valid_sessions'] == ['pre_market', 'regular', 'after_hours']
        m.quote('KR:005930')
        assert len(calls) == 4  # token, bars, KRX + NXT listing; the repeat is cached
    finally:
        redis_cache.delete(m._token_key())
        m.client.close()

class FakeRedis:
    """Just enough of redis-py for the shared KIS token path."""
    def __init__(self): self.data = {}
    def get(self, key): return self.data.get(key)
    def setex(self, key, ttl, value): self.data[key] = value
    def delete(self, key): self.data.pop(key, None)
    def lock(self, name, **kwargs):
        import threading
        return threading.Lock()
    def pipeline(self): return FakePipeline(self)

class FakePipeline:
    def __init__(self, redis): self.redis = redis; self.deletes = []
    def __enter__(self): return self
    def __exit__(self, *exc): pass
    def watch(self, key): pass
    def get(self, key): return self.redis.get(key)
    def multi(self): pass
    def delete(self, key): self.deletes.append(key)
    def execute(self):
        for key in self.deletes: self.redis.delete(key)

@pytest.fixture
def shared_kis(monkeypatch):
    import httpx
    from app.multi_market import KoreaPrices
    from app.redis_cache import redis_cache
    fake = FakeRedis()
    monkeypatch.setattr(redis_cache, 'client', fake)
    monkeypatch.setattr('app.multi_market.time.sleep', lambda seconds: None)
    state = {'issued': 0, 'status': 200, 'auth': []}
    def handler(request):
        if request.url.path == '/oauth2/tokenP':
            state['issued'] += 1
            return httpx.Response(200, json={'access_token': f"token-{state['issued']}", 'expires_in': 86400})
        state['auth'].append(request.headers['authorization'])
        if state['status'] != 200: return httpx.Response(state['status'])
        return httpx.Response(200, json={'rt_cd': '0'})
    processes = []
    def process():
        m = KoreaPrices()
        m.configured = True; m.key = 'key'; m.secret = 'secret'
        m.client.close()
        m.client = httpx.Client(base_url='https://test', transport=httpx.MockTransport(handler))
        processes.append(m)
        return m
    yield process, state, fake
    for m in processes: m.client.close()

def kis_request(m, n):
    return m.get('/uapi/domestic-stock/v1/quotations/test', 'TEST', {'n': n}, ttl=1)

def test_kis_token_is_issued_once_across_processes(shared_kis):
    process, state, fake = shared_kis
    web, worker = process(), process()
    kis_request(web, 1); kis_request(worker, 2)
    assert state['issued'] == 1
    assert state['auth'] == ['Bearer token-1', 'Bearer token-1']
    assert json.loads(fake.get(web._token_key()))['access_token'] == 'token-1'

def test_kis_rate_limit_cooldown_blocks_requests_with_valid_token(shared_kis):
    process, state, fake = shared_kis
    m = process()
    kis_request(m, 1)
    state['status'] = 429
    with pytest.raises(MarketError): kis_request(m, 2)
    sent = len(state['auth'])
    state['status'] = 200
    with pytest.raises(MarketError, match='대기'): kis_request(m, 3)
    assert len(state['auth']) == sent

def test_kis_rejected_token_keeps_newer_shared_token(shared_kis):
    process, state, fake = shared_kis
    web, worker = process(), process()
    kis_request(web, 1); kis_request(worker, 2)
    state['status'] = 401
    with pytest.raises(MarketError): kis_request(web, 3)
    assert fake.get(web._token_key()) is None
    # Another process already stored a replacement; a stale 401 must keep it.
    fake.setex(web._token_key(), 60, json.dumps({'access_token': 'token-2', 'expires_at': time.time() + 3600}))
    with pytest.raises(MarketError): kis_request(worker, 4)
    assert json.loads(fake.get(web._token_key()))['access_token'] == 'token-2'

def test_stale_fx_and_outage_rejected():
    import httpx
    from datetime import datetime, timezone, timedelta
    from app.multi_market import ReferenceFX
    m = ReferenceFX()
    m.client.close()
    old_date = (datetime.now(timezone.utc) - timedelta(days=8)).date().isoformat()
    m.client = httpx.Client(base_url='https://test', transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'base': 'USD', 'quote': 'KRW', 'rate': 1400, 'date': old_date})))
    try:
        with pytest.raises(MarketError): m.krw_to_usd()
    finally: m.client.close()

def test_fx_daily_rate_cache():
    import httpx
    from datetime import datetime, timezone
    from app.multi_market import ReferenceFX
    m = ReferenceFX(); m.client.close()
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'base': 'USD', 'quote': 'KRW', 'rate': 1400, 'date': datetime.now(timezone.utc).date().isoformat()})
    m.client = httpx.Client(base_url='https://test', transport=httpx.MockTransport(handler))
    try:
        rate, day = m.krw_to_usd()
        assert rate == Decimal('0.000714285714')
        m.krw_to_usd()
        assert len(calls) == 1
    finally: m.client.close()

def test_weekly_schedule_korea_time(monkeypatch):
    from datetime import datetime, timezone
    from app.weekly import next_run
    monkeypatch.setenv('WEEKLY_DAY', '5'); monkeypatch.setenv('WEEKLY_HOUR', '9')
    assert next_run(datetime(2026, 9, 25, 23, 59, tzinfo=timezone.utc)) == datetime(2026, 9, 26, 0, 0, tzinfo=timezone.utc)
    assert next_run(datetime(2026, 9, 26, 0, 0, tzinfo=timezone.utc)) == datetime(2026, 10, 3, 0, 0, tzinfo=timezone.utc)

def test_weekly_publication_returns_ties_and_idempotency(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app.weekly import tick, next_run
    from app.db import WeeklyReport, WeeklyState
    monkeypatch.setenv('WEEKLY_DAY', '5'); monkeypatch.setenv('WEEKLY_HOUR', '9')
    uid = seed()
    with Session.begin() as db:
        db.add(User(username='second', password_hash='unused'))
    start = datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)
    assert tick(FakeMarket(), start) == 'baseline'
    with Session.begin() as db:
        for user in db.scalars(select(User)): user.cash = 102000
        db.add(User(username='newcomer', password_hash='unused'))
    due = next_run(start)
    assert tick(FakeMarket(), due) == 'published'
    assert tick(FakeMarket(), due + timedelta(seconds=1)) == 'waiting'
    with Session() as db:
        reports = list(db.scalars(select(WeeklyReport)))
        assert len(reports) == 1
        assert [r['rank'] for r in reports[0].rows] == [1, 1]
        assert all(Decimal(r['return_pct']) == 2 for r in reports[0].rows)
        assert reports[0].notes['excluded_new_or_zero'] == 1
        assert db.get(WeeklyState, 1).baseline[str(uid)]['equity'] == '102000.0000'
    with Session.begin() as db: db.get(User, uid).cash = 103020
    assert tick(FakeMarket(), due + timedelta(days=7)) == 'published'
    with Session() as db:
        report = db.scalar(select(WeeklyReport).order_by(WeeklyReport.id.desc()))
        row = next(r for r in report.rows if r['username'] == 'test')
        assert Decimal(row['return_pct']) == 1  # weekly, not the 3.02% lifetime return
        assert Decimal(row['total_return_pct']) == Decimal('3.02')

def test_weekly_missing_prices_defers_without_changing_baseline():
    from datetime import datetime, timedelta, timezone
    from app.weekly import tick, next_run
    from app.db import WeeklyReport, WeeklyState
    uid = seed()
    start = datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)
    tick(FakeMarket(), start)
    execute_order(uid, order(), FakeMarket())
    class Offline:
        def quote(self, symbol): raise MarketError('offline')
    due = next_run(start)
    assert tick(Offline(), due) == 'unavailable'
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(WeeklyReport)) == 0
        assert db.get(WeeklyState, 1).baseline_at == start
        assert db.get(WeeklyState, 1).next_due == due
    m = FakeMarket()
    m.quote = lambda s: {'price': Decimal(100), 'timestamp': int(due.timestamp()), 'stale': False}
    assert tick(m, due + timedelta(minutes=5)) == 'published'

def test_weekly_holiday_uses_durable_quote_but_rejects_ancient_quote():
    from datetime import datetime, timedelta, timezone
    from app.weekly import tick, next_run
    from app.db import ReportPrice, WeeklyReport
    uid = seed()
    execute_order(uid, order(quantity=2), FakeMarket())
    start = datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)
    m = FakeMarket()
    m.quote = lambda s: {'price': Decimal(100), 'timestamp': int(start.timestamp()), 'stale': False}
    assert tick(m, start) == 'baseline'
    class Offline:
        def quote(self, symbol): raise MarketError('offline')
    due = next_run(start)
    assert tick(Offline(), due) == 'published'
    with Session() as db:
        report = db.scalar(select(WeeklyReport))
        assert report.notes['quotes']['AAPL']['quote_time'] == start.isoformat()
    assert tick(Offline(), due + timedelta(days=7)) == 'unavailable'

def test_weekly_concurrent_publish_and_restart_catchup():
    from datetime import datetime, timedelta, timezone
    from app.weekly import tick, next_run
    from app.db import WeeklyReport
    seed()
    start = datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)
    tick(FakeMarket(), start)
    resumed = next_run(start) + timedelta(days=9)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: tick(FakeMarket(), resumed), range(4)))
    assert results.count('published') == 1
    with Session() as db:
        reports = list(db.scalars(select(WeeklyReport)))
        assert len(reports) == 1
        assert reports[0].period_end == resumed
        assert reports[0].notes['late'] is True

def test_weekly_api_requires_login_and_hides_holdings(client):
    from datetime import datetime, timezone
    from app.weekly import tick, next_run
    assert client.get('/api/weekly').status_code == 401
    register(client)
    start = datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)
    tick(FakeMarket(), start)
    tick(FakeMarket(), next_run(start))
    result = client.get('/api/weekly').json()
    assert result['reports'][0]['rows'][0]['username'] == 'alice'
    assert 'quotes' not in result['reports'][0]
