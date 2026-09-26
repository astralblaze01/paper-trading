from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid5, NAMESPACE_URL, UUID
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from .db import Session, LimitOrder, Transaction, PopularityEvent, Position, lock_user
from .market import MarketError
from .money import OrderRejected
from .trading import execute_order, checked_quote


# Reservation orders (예약 주문): a trigger price and a direction. They fill at
# the market price on the first worker pass (about once a minute) during a
# tradable session where the price has reached the trigger.
#   limit: buy at or below / sell at or above the price (지정가 매수, 익절 매도)
#   stop:  buy at or above / sell at or below the price (돌파 매수, 손절 매도)
MAX_PENDING = 20
ORDER_TYPES = {('buy', 'below'): 'limit', ('sell', 'above'): 'limit', ('buy', 'above'): 'stop', ('sell', 'below'): 'stop'}


def trigger_of(side, order_type):
    """'below' or 'above': where the price must be for the order to fill."""
    return 'below' if (side == 'buy') == (order_type == 'limit') else 'above'


def triggered(side, order_type, price, target):
    return price <= target if trigger_of(side, order_type) == 'below' else price >= target


def create(uid,data):
    from .instruments import currency_of
    order_type=ORDER_TYPES[(data.side,data.trigger)]
    if currency_of(data.symbol)=='KRW' and data.limit_price!=data.limit_price.to_integral_value():
        raise HTTPException(422,'원화 종목의 조건 가격은 1원 단위로 입력하세요.')
    with Session.begin() as db:
        user=lock_user(db,uid)
        if not user or not user.active: raise HTTPException(403,'정지된 계좌입니다.')
        previous=db.scalar(select(LimitOrder).where(LimitOrder.user_id==uid,LimitOrder.request_id==str(data.request_id)))
        if previous:
            if previous.order_type!=order_type or (previous.symbol,previous.side,previous.quantity,previous.limit_price)!=(data.symbol,data.side,data.quantity,data.limit_price): raise HTTPException(409,'동일 요청 ID에 다른 주문입니다.')
            return {'id':previous.id,'status':previous.status,'replayed':True}
        if db.scalar(select(Transaction.id).where(Transaction.user_id==uid,Transaction.request_id==str(data.request_id))):
            raise HTTPException(409,'이미 처리된 시장가 주문 ID입니다.')
        pending=list(db.scalars(select(LimitOrder.id).where(LimitOrder.user_id==uid,LimitOrder.status=='pending')))
        if len(pending)>=MAX_PENDING: raise HTTPException(409,f'대기 중인 예약 주문은 최대 {MAX_PENDING}개입니다.')
        if data.side=='sell':
            held=db.get(Position,(uid,data.symbol))
            if not held or held.quantity<data.quantity: raise HTTPException(409,'보유 수량보다 많이 예약 매도할 수 없습니다.')
        row=LimitOrder(user_id=uid,request_id=str(data.request_id),symbol=data.symbol,side=data.side,quantity=data.quantity,limit_price=data.limit_price,order_type=order_type,use_max=False,status='pending',created_at=datetime.now(timezone.utc))
        db.add(row); db.flush(); return {'id':row.id,'status':row.status,'replayed':False}


def public(row):
    return {'id':row.id,'symbol':row.symbol,'side':row.side,'quantity':row.quantity,
            'limit_price':row.limit_price if row.order_type in ('limit','stop') else None,
            'trigger':trigger_of(row.side,row.order_type) if row.order_type in ('limit','stop') else None,
            'order_type':row.order_type,'use_max':row.use_max,'status':row.status,'reason':row.reason,
            'created_at':row.created_at,'filled_at':row.filled_at,'filled_price':row.filled_price}


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
            # Gone since the listing (account deletion or an admin clear removed it):
            # nothing is left to fill. A queued order never outlives its user (FK).
            if row is None or row.status!='pending': continue
            if not user.active: row.status='cancelled'; row.reason='계좌 정지'; continue
            try:
                q,price=checked_quote(row.symbol,market)
                if row.order_type in ('limit','stop') and not triggered(row.side,row.order_type,price,row.limit_price):
                    row.reason=None; continue
                fixed=SimpleNamespace(quote=lambda symbol:q,providers=getattr(market,'providers',{}),fx=getattr(market,'fx',None))
                request_id=UUID(row.request_id) if row.order_type=='market' else uuid5(NAMESPACE_URL,f'paper-limit:{uid}:{order_id}')
                order=SimpleNamespace(symbol=row.symbol,side=row.side,quantity=row.quantity,use_max=row.use_max,request_id=request_id)
                execute_order(uid,order,fixed,db=db,requested_at=row.created_at)
                row.status='filled'; row.reason=None; row.filled_at=datetime.now(timezone.utc); row.filled_price=price; filled+=1
                db.execute(insert(PopularityEvent).values(user_id=uid,symbol=row.symbol,kind='order',bucket=int(datetime.now(timezone.utc).timestamp())//3600,created_at=datetime.now(timezone.utc)).on_conflict_do_nothing())
            except MarketError: row.reason='시세 조회 대기'
            except OrderRejected as exc: row.status='rejected'; row.reason=str(exc.detail)[:200]
            # Any other refusal (stale quote, session closed, ...) is retried on the next pass.
            except HTTPException as exc: row.reason=str(exc.detail)[:200]
    return filled
