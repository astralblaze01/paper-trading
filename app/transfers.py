"""Paper wallet transfers only, serialized with trades and weekly valuations."""
from datetime import datetime,timezone
from decimal import Decimal
from typing import Literal
from uuid import UUID
from fastapi import Depends,HTTPException
from pydantic import BaseModel,ConfigDict,Field,field_validator
from sqlalchemy import select,text,or_
from .db import Session,User,WalletTransfer
from .money import wallets,rounded,bps

class TransferInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    recipient: str=Field(min_length=3,max_length=32,pattern=r'^[가-힣A-Za-z0-9_]+$')
    currency: Literal['USD','KRW']
    amount: Decimal=Field(gt=0,le=1000000000,max_digits=18,decimal_places=4)
    @field_validator('recipient')
    @classmethod
    def normalize(cls,value): return value.lower()
class TransferOrder(TransferInput): request_id: UUID

def estimate(data):
    if rounded(data.amount,data.currency)!=data.amount: raise HTTPException(422,'USD는 소수점 4자리, KRW는 1원 단위로 입력하세요.')
    fee_bps=bps('TRANSFER_FEE_BPS','10');fee=rounded(data.amount*fee_bps/10000,data.currency,up=True)
    return {'recipient':data.recipient,'currency':data.currency,'amount':data.amount,'fee':fee,'fee_bps':fee_bps,'total':data.amount+fee,'received':data.amount}

def transfer(uid,data,fx):
    with Session.begin() as db:
        db.execute(text('SELECT pg_advisory_xact_lock(74923102)'))
        target=db.scalar(select(User).where(User.username==data.recipient))
        if not target or not target.active: raise HTTPException(404,'이체 가능한 사용자를 찾을 수 없습니다.')
        if target.id==uid: raise HTTPException(422,'본인에게 이체할 수 없습니다.')
        locked={u.id:u for u in db.scalars(select(User).where(User.id.in_([uid,target.id])).order_by(User.id).with_for_update().execution_options(populate_existing=True))}
        sender=locked.get(uid);target=locked[target.id]
        if not sender or not sender.active or not target.active: raise HTTPException(403,'정지된 계정은 이체할 수 없습니다.')
        prior=db.scalar(select(WalletTransfer).where(WalletTransfer.sender_id==uid,WalletTransfer.request_id==str(data.request_id)))
        if prior:
            if (prior.recipient_id,prior.currency,prior.amount)!=(target.id,data.currency,data.amount): raise HTTPException(409,'재시도 요청 내용이 다릅니다.')
            return {'id':prior.id,'replayed':True,'received':prior.amount,'fee':prior.fee}
        cost=estimate(data);q=fx.current_rate('USD','KRW');rate=q['rate']
        src=wallets(db,sender);dst=wallets(db,target)
        if src[data.currency].balance<cost['total']: raise HTTPException(409,'수수료를 포함한 이체 잔액이 부족합니다.')
        if dst[data.currency].balance+data.amount>Decimal('1000000000000000'): raise HTTPException(409,'받는 계좌의 한도를 초과합니다.')
        for u in [sender,target]:
            if u.initial_krw is None:u.initial_krw=u.initial_usd*rate;u.initial_fx_date=q['date']
        src[data.currency].balance-=cost['total'];dst[data.currency].balance+=data.amount
        flow=data.amount*(rate if data.currency=='USD' else 1)
        sender.net_contributions_krw-=flow;target.net_contributions_krw+=flow
        sender.cash=src['USD'].balance;target.cash=dst['USD'].balance
        row=WalletTransfer(sender_id=uid,recipient_id=target.id,request_id=str(data.request_id),currency=data.currency,amount=data.amount,fee=cost['fee'],fee_bps=cost['fee_bps'],fx_rate=rate,rate_date=q['date'],created_at=datetime.now(timezone.utc))
        db.add(row);db.flush()
        return {'id':row.id,'replayed':False,'received':row.amount,'fee':row.fee}

def install_transfers(app,ctx):
    @app.get('/api/users/suggest')
    def suggest(q:str='',uid=Depends(ctx.current_user)):
        query=q.strip().lower()
        if len(query)<1:return []
        with Session() as db:
            rows=db.scalars(select(User).where(User.id!=uid,User.active.is_(True),User.username.ilike(f'%{query}%')).order_by(User.username).limit(8))
            return [{'username':u.username} for u in rows]
    @app.post('/api/transfers/preview',dependencies=[Depends(ctx.csrf)])
    def preview(data:TransferInput,uid=Depends(ctx.current_user)):
        cost=estimate(data)
        with Session.begin() as db:
            target=db.scalar(select(User).where(User.username==data.recipient,User.active.is_(True)))
            if not target:raise HTTPException(404,'이체 가능한 사용자를 찾을 수 없습니다.')
            if target.id==uid:raise HTTPException(422,'본인에게 이체할 수 없습니다.')
            sender=db.scalar(select(User).where(User.id==uid).with_for_update())
            balance=wallets(db,sender)[data.currency].balance
        return cost|{'balance':balance,'balance_after':balance-cost['total']}
    @app.post('/api/transfers',dependencies=[Depends(ctx.csrf)])
    def send(data:TransferOrder,uid=Depends(ctx.current_user)):return transfer(uid,data,ctx.fx)
    @app.get('/api/transfers')
    def history(uid=Depends(ctx.current_user)):
        with Session() as db:
            user=db.get(User,uid);names=dict(db.execute(select(User.id,User.username)).all())
            query=select(WalletTransfer).where(or_(WalletTransfer.sender_id==uid,WalletTransfer.recipient_id==uid))
            if user.records_since:query=query.where(WalletTransfer.created_at>=user.records_since)
            return [{'id':r.id,'direction':'sent' if r.sender_id==uid else 'received','counterparty':names[r.recipient_id if r.sender_id==uid else r.sender_id],'currency':r.currency,'amount':r.amount,'fee':r.fee if r.sender_id==uid else 0,'created_at':r.created_at} for r in db.scalars(query.order_by(WalletTransfer.id.desc()).limit(100))]
