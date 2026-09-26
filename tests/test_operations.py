from decimal import Decimal as D
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone,timedelta
from sqlalchemy import select,func
from fastapi import HTTPException
from test_service import database,client,register,seed,FakeMarket,order
from app import main
from app.db import Session,User,Wallet,Transaction,AdminAudit,SeasonArchive,WeeklyState,WeeklyReport,WalletTransfer
from app.trading import execute_order
from app.portfolio import portfolio
from app.weekly import tick,report_list

def users(client):
    token=register(client,'operator')
    with Session.begin() as db:
        admin=db.scalar(select(User).where(User.username=='operator'));admin.is_admin=True
        u=User(username='investor',password_hash='unused');db.add(u);db.flush();uid=u.id
    return {'x-csrf-token':token},uid

def command(action='grant',**kwargs):
    return dict(action=action,currency='USD',amount='1000',reason='운영 테스트',confirmation='investor',request_id=str(uuid4()))|kwargs

def test_public_portfolio_readonly_and_admin_exclusion(client):
    headers,uid=users(client)
    execute_order(uid,order(quantity=2),FakeMarket())
    rows=client.get('/api/ranking').json()['rows']
    assert [r['username'] for r in rows]==['investor']
    result=client.get('/api/portfolios/investor')
    assert result.status_code==200
    assert result.json()['positions'][0]['quantity']==2
    assert not {'password_hash','transactions','orders','request_id'} & result.json().keys()
    assert client.get('/api/portfolios/operator').status_code==404
    assert client.post('/api/portfolios/investor',headers=headers,json={'cash':1}).status_code==405
    with Session() as db: assert db.get(Wallet,(uid,'USD')).balance==99800

def test_grants_dont_inflate_returns_and_are_idempotent(client):
    headers,uid=users(client)
    before=portfolio(uid,FakeMarket(),main.fx)
    for currency,amount in [('USD','1000'),('KRW','100000')]:
        body=command(currency=currency,amount=amount)
        assert client.post(f'/api/admin/users/{uid}/manage',headers=headers,json=body).status_code==200
        assert client.post(f'/api/admin/users/{uid}/manage',headers=headers,json=body).json()['replayed']
    after=portfolio(uid,FakeMarket(),main.fx)
    assert after['return_pct']==before['return_pct']==0
    # The dollar-basis return ignores grants too; the KRW grant is booked at its day's rate.
    assert before['return_pct_usd']==0 and abs(after['return_pct_usd'])<D('0.000001')
    assert after['equity']==before['equity']+1100000
    with Session() as db: assert db.get(User,uid).net_contributions_usd==(1000+D(100000)/after['fx']['rate']).quantize(D('.0001'))
    with Session() as db: assert db.scalar(select(func.count()).select_from(AdminAudit))==2

def test_bulk_grant_targets_active_non_admin_users(client):
    headers,uid=users(client)
    with Session.begin() as db:
        other=User(username='두번째사용자',password_hash='unused');db.add(other);db.flush();from app.money import wallets;wallets(db,other)
    body=command(confirmation='ALL USERS')
    result=client.post('/api/admin/users/manage-all',headers=headers,json=body)
    assert result.status_code==200 and result.json()['count']==2
    with Session() as db:
        assert db.get(Wallet,(uid,'USD')).balance==101000
        assert db.get(Wallet,(other.id,'USD')).balance==101000

def test_admin_can_delete_account_but_not_self(client):
    headers,uid=users(client)
    body=command('delete',confirmation='DELETE investor')
    assert client.post(f'/api/admin/users/{uid}/manage',headers=headers,json=body).status_code==200
    assert client.post(f'/api/admin/users/{uid}/manage',headers=headers,json=body).json()['replayed']
    with Session() as db: assert db.get(User,uid) is None
    with Session() as db: admin_id=db.scalar(select(User.id).where(User.username=='operator'))
    own=command('delete',confirmation='DELETE operator')
    assert client.post(f'/api/admin/users/{admin_id}/manage',headers=headers,json=own).status_code==409

def test_rebase_preserves_assets_clear_archives_and_removes_records(client):
    headers,uid=users(client)
    execute_order(uid,order(quantity=2),FakeMarket())
    with Session.begin() as db:
        admin_id=db.scalar(select(User.id).where(User.username=='operator'))
        db.add(WalletTransfer(sender_id=admin_id,recipient_id=uid,request_id=str(uuid4()),currency='USD',amount=10,fee=0,fee_bps=0,fx_rate=1000,rate_date='2026-09-24',created_at=datetime.now(timezone.utc)))
    assert client.post(f'/api/admin/users/{uid}/manage',headers=headers,json=command('rebase')).status_code==200
    with Session() as db: assert db.scalar(select(func.count()).select_from(Transaction))==1
    # No target confirmation phrase is required any more.
    assert client.post(f'/api/admin/users/{uid}/manage',headers=headers,json=command('clear',confirmation='')).status_code==200
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(Transaction))==0
        assert db.scalar(select(SeasonArchive)).data['transactions'][0]['quantity']==2
        assert db.get(Wallet,(uid,'USD')).balance==100000
        assert db.get(User,uid).net_contributions_krw==0
        # The transfer also belongs to the sender: it stays in their history
        # and is only hidden from the reset account.
        assert db.scalar(select(func.count()).select_from(WalletTransfer))==1
        assert db.get(User,uid).records_since is not None

def test_normal_user_cannot_admin_manage(client):
    token=register(client)
    body=command(confirmation='alice')
    assert client.post('/api/admin/users/1/manage',headers={'x-csrf-token':token},json=body).status_code==403
    assert client.get('/api/admin/audit').status_code==403

def test_weekly_excludes_admin_and_grant_performance(client):
    headers,uid=users(client);now=datetime.now(timezone.utc)
    assert tick(FakeMarket(),now,main.fx)=='baseline'
    with Session() as db:
        state=db.get(WeeklyState,1);assert len(state.baseline)==1;due=state.next_due
    assert client.post(f'/api/admin/users/{uid}/manage',headers=headers,json=command()).status_code==200
    assert tick(FakeMarket(),due+timedelta(seconds=1),main.fx)=='published'
    rows=report_list()['reports'][0]['rows']
    assert len(rows)==1 and rows[0]['username']=='investor' and D(rows[0]['return_pct'])==0

def test_company_cache_and_provider_outage():
    from app.company import cache,company_info
    from app.market import MarketError
    class Adapter:
        def __init__(self):self.calls=0
        def get(self,*args):self.calls+=1;return {'name':'Test Co','finnhubIndustry':'Technology','weburl':'https://example.com'}
    m=type('M',(),{'us':Adapter()})()
    assert company_info('PROFILETEST',m)['name']=='Test Co'
    calls=m.us.calls  # profile + dividend metric
    company_info('PROFILETEST',m);assert m.us.calls==calls==2
    m.us.get=lambda *args:(_ for _ in ()).throw(MarketError('offline'))
    assert company_info('OFFLINETEST',m)['notice']


def test_transfer_feature_is_removed_but_history_is_kept(client):
    headers,uid=users(client)
    with Session() as db:admin_id=db.scalar(select(User.id).where(User.username=='operator'))
    with Session.begin() as db:
        db.add(WalletTransfer(sender_id=admin_id,recipient_id=uid,request_id=str(uuid4()),currency='USD',amount=10,fee=0,fee_bps=0,fx_rate=1000,rate_date='2026-09-24',created_at=datetime.now(timezone.utc)))
    body={'recipient':'investor','currency':'USD','amount':'1'}
    for method,path,payload in [('post','/api/transfers',body|{'request_id':str(uuid4())}),('post','/api/transfers/preview',body),
                                ('get','/api/transfers',None),('get','/api/transfers/share?currency=USD&percent=50',None),
                                ('get','/api/users/suggest?q=inv',None),('get','/api/wallets',None)]:
        r=getattr(client,method)(path,headers=headers,**({'json':payload} if payload else {}))
        assert r.status_code in (404,405),(path,r.status_code)
    assert 'TRANSFER_FEE_BPS' not in client.get('/api/admin').json()['fees'] and 'transfers' not in client.get('/api/admin').json()['counts']
    # Past transfers stay stored; balances are untouched by the removal.
    with Session() as db:assert db.scalar(select(func.count()).select_from(WalletTransfer))==1
