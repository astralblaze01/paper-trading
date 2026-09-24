from datetime import datetime, timezone
import os
from contextlib import nullcontext
from decimal import Decimal
from sqlalchemy import select
from fastapi import HTTPException
from .db import Session, User, Position, Transaction
from .money import wallets, costs, maximum


def checked_quote(symbol, market, allow_stale=False):
    q=market.quote(symbol)
    if q.get('stale') and not allow_stale:
        raise HTTPException(409,'오래된 시세로는 주문할 수 없습니다.')
    stamp=datetime.fromtimestamp(q['timestamp'],timezone.utc)
    age=(datetime.now(timezone.utc)-stamp).total_seconds()
    max_age = int(os.getenv('MAX_QUOTE_AGE','900') if symbol.startswith('KR:') else os.getenv('US_MAX_QUOTE_AGE','1800'))
    if age < -60 or age > (7*86400 if allow_stale else max_age):
        raise HTTPException(409,'시세가 만료되었습니다.')
    currency='KRW' if symbol.startswith('KR:') else 'USD'
    if q.get('currency',currency)!=currency: raise HTTPException(409,'시세 통화가 일치하지 않습니다.')
    price=Decimal(str(q.get('native_price',q['price'])))
    if not price.is_finite() or price<=0: raise HTTPException(409,'유효한 가격이 없습니다.')
    return q, price


def preview_order(uid, symbol, side, quantity, market, share=None):
    q, price=checked_quote(symbol,market,allow_stale=True)
    with Session.begin() as db:
        user=db.scalar(select(User).where(User.id==uid).with_for_update())
        if not user or not user.active: raise HTTPException(403,'계좌를 사용할 수 없습니다.')
        ws=wallets(db,user)
        currency='KRW' if symbol.startswith('KR:') else 'USD'
        p=db.get(Position,(uid,symbol))
        available=ws[currency].balance
        maximum_quantity=maximum(symbol,price,available) if side=='buy' else (p.quantity if p else 0)
        maximum_quantity=min(maximum_quantity,1000000)
        if share is not None:
            quantity=int(Decimal(maximum_quantity)*Decimal(share)/Decimal(100))
        c=costs(symbol,side,price,quantity)
        return c | {'price':price,'quantity':quantity,'max_quantity':maximum_quantity,'balance':available,
                    'balance_after':available-c['net_amount'] if side=='buy' else available+c['net_amount'],
                    'quote_timestamp':q['timestamp'],'indicative_only':q.get('stale',False) or datetime.now(timezone.utc).timestamp()-q['timestamp']>int(os.getenv('MAX_QUOTE_AGE','900') if symbol.startswith('KR:') else os.getenv('US_MAX_QUOTE_AGE','1800')),
                    'holding':p.quantity if p else 0,
                    'holding_after':(p.quantity if p else 0)+(quantity if side=='buy' else -quantity),
                    'can_submit':0<quantity<=maximum_quantity,
                    'average_cost':p.native_average_cost if p else None,
                    'unrealized_pnl':(price-(p.native_average_cost if p.native_average_cost is not None else p.average_cost))*p.quantity if p else Decimal(0)}


def execute_order(user_id, order, market, db=None):
    with (Session.begin() if db is None else nullcontext(db)) as db:
        user=db.scalar(select(User).where(User.id==user_id).with_for_update())
        if not user or not user.active: raise HTTPException(403,'사용할 수 없는 계좌입니다.')
        previous=db.scalar(select(Transaction).where(Transaction.user_id==user_id,Transaction.request_id==str(order.request_id)))
        if previous:
            if (previous.symbol,previous.side)!=(order.symbol,order.side) or (not getattr(order,'use_max',False) and previous.quantity!=order.quantity):
                raise HTTPException(409,'동일 주문 ID에 다른 주문을 사용할 수 없습니다.')
            return {'id':previous.id,'replayed':True,'quantity':previous.quantity}
        q,price=checked_quote(order.symbol,market)
        ws=wallets(db,user)
        currency='KRW' if order.symbol.startswith('KR:') else 'USD'
        position=db.get(Position,(user_id,order.symbol))
        quantity=order.quantity
        if getattr(order,'use_max',False):
            quantity=maximum(order.symbol,price,ws[currency].balance) if order.side=='buy' else (position.quantity if position else 0)
        if quantity<=0: raise HTTPException(409,'주문 가능한 수량이 없습니다.')
        c=costs(order.symbol,order.side,price,quantity)
        realized=Decimal(0)
        if order.side=='buy':
            if ws[currency].balance<c['net_amount']: raise HTTPException(409,f'{currency} 잔액이 부족합니다. 필요한 통화로 먼저 환전하세요.')
            if position is None:
                position=Position(user_id=user_id,symbol=order.symbol,quantity=0,average_cost=Decimal(0),native_average_cost=Decimal(0))
                db.add(position)
            native_cost=position.native_average_cost if position.native_average_cost is not None else position.average_cost
            position.native_average_cost=(native_cost*position.quantity+c['net_amount'])/(position.quantity+quantity)
            position.average_cost=(position.average_cost*position.quantity+q['price']*quantity)/(position.quantity+quantity)
            position.quantity+=quantity
            ws[currency].balance-=c['net_amount']
        else:
            if position is None or position.quantity<quantity: raise HTTPException(409,'보유 수량이 부족합니다.')
            native_cost=position.native_average_cost if position.native_average_cost is not None else position.average_cost
            realized=c['net_amount']-native_cost*quantity
            position.quantity-=quantity
            ws[currency].balance+=c['net_amount']
            if position.quantity==0: db.delete(position)
        user.cash=ws['USD'].balance  # Legacy compatibility mirror; wallets are authoritative.
        trade=Transaction(user_id=user_id,request_id=str(order.request_id),symbol=order.symbol,side=order.side,quantity=quantity,
                          price=q['price'],native_price=price,fx_rate=q.get('fx_rate',Decimal(1)),fx_date=q.get('fx_date'),
                          quote_time=datetime.fromtimestamp(q['timestamp'],timezone.utc),created_at=datetime.now(timezone.utc),
                          realized_pnl=realized,accounting_version=2,**c)
        db.add(trade); db.flush()
        return {'id':trade.id,'replayed':False,'quantity':quantity,'currency':currency,'net_amount':c['net_amount']}
