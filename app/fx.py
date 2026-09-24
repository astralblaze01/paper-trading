from typing import Protocol
from decimal import Decimal
from datetime import datetime, timezone, date
from sqlalchemy import select
from fastapi import HTTPException
from .db import Session, User, FxTransaction
from .market import MarketError
from .money import wallets, rounded, bps

class FxProvider(Protocol):
    def current_rate(self, source: str, target: str) -> dict: ...

class FxService:
    def __init__(self, provider): self.provider=provider
    def current_rate(self, source='USD', target='KRW'):
        if {source,target} != {'USD','KRW'}: raise HTTPException(422,'USD와 KRW 사이에서만 환전 가능합니다.')
        rate, day = self.provider.krw_to_usd()
        age = (datetime.now(timezone.utc).date()-date.fromisoformat(day)).days
        if not rate.is_finite() or rate<=0 or not 0<=age<=7: raise MarketError('기준환율이 오래되어 환전을 중단합니다.')
        return {'source':source,'target':target,'rate':rate if source=='KRW' else Decimal(1)/rate,'date':day,'label':'ECB 일별 기준환율 · 실시간 환율 아님','stale':False}

def preview(fx, source, amount):
    target='KRW' if source=='USD' else 'USD'
    q=fx.current_rate(source,target)
    fee_bps, spread_bps = bps('FX_FEE_BPS','10'), bps('FX_SPREAD_BPS','5')
    if amount<=0 or rounded(amount,source)!=amount: raise HTTPException(422,'금액과 통화별 최소 단위를 확인하세요.')
    fee=rounded(amount*fee_bps/10000,source,up=True)
    rate=q['rate']*(1-spread_bps/10000)
    received=rounded((amount-fee)*rate,target)
    if received<=0: raise HTTPException(422,'환전 금액이 너무 작습니다.')
    return q | dict(amount=amount,fee=fee,fee_bps=fee_bps,spread_bps=spread_bps,applied_rate=rate,received=received)

def exchange(uid, data, fx):
    with Session.begin() as db:
        user=db.scalar(select(User).where(User.id==uid).with_for_update())
        if not user or not user.active: raise HTTPException(403,'사용할 수 없는 계좌입니다.')
        previous=db.scalar(select(FxTransaction).where(FxTransaction.user_id==uid,FxTransaction.request_id==str(data.request_id)))
        if previous:
            if previous.source!=data.source or previous.amount!=data.amount: raise HTTPException(409,'중복 요청 ID의 내용이 다릅니다.')
            return {'id':previous.id,'replayed':True,'received':previous.received}
        q=preview(fx,data.source,data.amount)
        ws=wallets(db,user)
        if ws[data.source].balance<data.amount: raise HTTPException(409,'환전 잔액이 부족합니다.')
        ws[data.source].balance-=data.amount
        ws[q['target']].balance+=q['received']
        user.cash=ws['USD'].balance
        t=FxTransaction(user_id=uid,request_id=str(data.request_id),source=data.source,target=q['target'],amount=data.amount,received=q['received'],fee=q['fee'],rate=q['applied_rate'],fee_bps=q['fee_bps'],spread_bps=q['spread_bps'],rate_date=q['date'],created_at=datetime.now(timezone.utc))
        db.add(t); db.flush()
        return {'id':t.id,'replayed':False,'received':t.received}
