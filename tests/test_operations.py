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
    assert after['equity']==before['equity']+1100000
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


def test_transfers_fee_flow_idempotency_and_ownership(client):
    from app.transfers import TransferOrder,transfer
    headers,recipient=users(client)
    with Session() as db:sender=db.scalar(select(User.id).where(User.username=='operator'))
    body=TransferOrder(recipient='investor',currency='USD',amount=D(1000),request_id=uuid4())
    result=transfer(sender,body,main.fx)
    assert result['fee']==1 and transfer(sender,body,main.fx)['replayed']
    assert portfolio(sender,FakeMarket(),main.fx)['return_pct']==D('-.001')
    assert portfolio(recipient,FakeMarket(),main.fx)['return_pct']==0
    with Session() as db:
        assert db.get(Wallet,(sender,'USD')).balance==98999
        assert db.get(Wallet,(recipient,'USD')).balance==101000
    token=register(client,'outsider')
    assert client.get('/api/transfers').json()==[]
    assert client.post('/api/transfers',headers={'x-csrf-token':token},json={'sender_id':sender,**body.model_dump(mode='json')}).status_code==422

def test_transfer_recipient_suggestions(client):
    headers,recipient=users(client)
    rows=client.get('/api/users/suggest?q=vest').json()
    assert rows==[{'username':'investor'}]


def test_transfer_concurrency_prevents_overdraft_and_negative_units(client):
    from app.transfers import TransferOrder,transfer
    headers,recipient=users(client)
    with Session() as db:sender=db.scalar(select(User.id).where(User.username=='operator'))
    def send(_):
        try:transfer(sender,TransferOrder(recipient='investor',currency='USD',amount=D(60000),request_id=uuid4()),main.fx);return 200
        except HTTPException as e:return e.status_code
    with ThreadPoolExecutor(max_workers=2) as pool: assert sorted(pool.map(send,[1,2]))==[200,409]
    with Session() as db:assert db.get(Wallet,(sender,'USD')).balance==39940
    import pytest
    with pytest.raises(HTTPException):transfer(sender,TransferOrder(recipient='operator',currency='USD',amount=D(1),request_id=uuid4()),main.fx)
    with pytest.raises(HTTPException):transfer(sender,TransferOrder(recipient='investor',currency='KRW',amount=D('.5'),request_id=uuid4()),main.fx)
