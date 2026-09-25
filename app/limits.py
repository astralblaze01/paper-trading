from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid5, NAMESPACE_URL, UUID
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from .db import Session, LimitOrder, Transaction, PopularityEvent, lock_user
from .market import MarketError
from .trading import execute_order, checked_quote


def create(uid,data):
    with Session.begin() as db:
        user=lock_user(db,uid)
        if not user or not user.active: raise HTTPException(403,'정지된 계좌입니다.')
        previous=db.scalar(select(LimitOrder).where(LimitOrder.user_id==uid,LimitOrder.request_id==str(data.request_id)))
        if previous:
            if previous.order_type!='limit' or (previous.symbol,previous.side,previous.quantity,previous.limit_price)!=(data.symbol,data.side,data.quantity,data.limit_price): raise HTTPException(409,'동일 요청 ID에 다른 주문입니다.')
            return {'id':previous.id,'status':previous.status}
        if db.scalar(select(Transaction.id).where(Transaction.user_id==uid,Transaction.request_id==str(data.request_id))):
            raise HTTPException(409,'이미 처리된 시장가 주문 ID입니다.')
        pending=list(db.scalars(select(LimitOrder.id).where(LimitOrder.user_id==uid,LimitOrder.status=='pending')))
        if len(pending)>=20: raise HTTPException(409,'미체결 주문은 최대 20개입니다.')
        row=LimitOrder(user_id=uid,request_id=str(data.request_id),symbol=data.symbol,side=data.side,quantity=data.quantity,limit_price=data.limit_price,order_type='limit',use_max=False,status='pending',created_at=datetime.now(timezone.utc))
        db.add(row); db.flush(); return {'id':row.id,'status':row.status}


def cancel(uid,order_id):
    with Session.begin() as db:
        lock_user(db,uid)
        row=db.scalar(select(LimitOrder).where(LimitOrder.id==order_id,LimitOrder.user_id==uid).with_for_update())
        if not row: raise HTTPException(404,'주문을 찾을 수 없습니다.')
        if row.status=='pending': row.status='cancelled'
        return {'id':row.id,'status':row.status}


def process(market):
    with Session() as db: ids=list(db.execute(select(LimitOrder.id,LimitOrder.user_id).where(LimitOrder.status=='pending').order_by(LimitOrder.id).limit(100)))
    filled=0
    for order_id,uid in ids:
        with Session.begin() as db:
            user=lock_user(db,uid)
            row=db.scalar(select(LimitOrder).where(LimitOrder.id==order_id).with_for_update())
            if row.status!='pending': continue
            if not user.active: row.status='cancelled'; row.reason='계좌 정지'; continue
            try:
                q,price=checked_quote(row.symbol,market)
                if row.order_type=='limit' and ((row.side=='buy' and price>row.limit_price) or (row.side=='sell' and price<row.limit_price)): continue
                fixed=SimpleNamespace(quote=lambda symbol:q,providers=getattr(market,'providers',{}))
                request_id=UUID(row.request_id) if row.order_type=='market' else uuid5(NAMESPACE_URL,f'paper-limit:{uid}:{order_id}')
                order=SimpleNamespace(symbol=row.symbol,side=row.side,quantity=row.quantity,use_max=row.use_max,request_id=request_id)
                execute_order(uid,order,fixed,db=db,requested_at=row.created_at)
                row.status='filled'; row.reason=None; filled+=1
                db.execute(insert(PopularityEvent).values(user_id=uid,symbol=row.symbol,kind='order',bucket=int(datetime.now(timezone.utc).timestamp())//3600,created_at=datetime.now(timezone.utc)).on_conflict_do_nothing())
            except MarketError: row.reason='시세 조회 대기'
            except HTTPException as exc:
                if '잔액' in str(exc.detail) or '수량' in str(exc.detail): row.status='rejected'
                row.reason=str(exc.detail)[:200]
    return filled
