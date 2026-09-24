"""Audited administrator operations. Never invoked at startup."""
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal
from uuid import UUID
from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, delete, text
from .db import Session, User, Position, Wallet, Transaction, FxTransaction, LimitOrder, Watchlist, PopularityEvent, SeasonArchive, WeeklyState, WeeklyReport, AdminAudit
from .money import wallets, initial_amount, rounded
from .market import MarketError

class ManagementInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    action: Literal['grant','rebase','clear']
    currency: Literal['USD','KRW']='USD'
    amount: Decimal=Field(default=Decimal(0),ge=0,le=1000000000,max_digits=18,decimal_places=4)
    reason: str=Field(min_length=3,max_length=300)
    confirmation: str=Field(max_length=64)
    request_id: UUID

def serial(value): return json.loads(json.dumps(value,default=str))
def records(db,model,target):
    return [{c.name:getattr(row,c.name) for c in model.__table__.columns} for row in db.scalars(select(model).where(model.user_id==target))]

def install_admin_ops(app,ctx,admin,csrf):
    @app.get('/api/admin/audit')
    def audit(uid=Depends(admin)):
        with Session() as db:
            names=dict(db.execute(select(User.id,User.username)).all())
            return [{'id':r.id,'actor':names.get(r.actor_id),'target':names.get(r.target_id),'action':r.action,'reason':r.reason,'data':r.data,'created_at':r.created_at} for r in db.scalars(select(AdminAudit).order_by(AdminAudit.id.desc()).limit(100))]

    @app.post('/api/admin/users/{target}/manage',dependencies=[Depends(csrf)])
    def manage(target:int,data:ManagementInput,uid=Depends(admin)):
        with Session.begin() as db:
            db.execute(text('SELECT pg_advisory_xact_lock(74923102)'))
            u=db.scalar(select(User).where(User.id==target).with_for_update())
            if not u: raise HTTPException(404,'사용자가 없습니다.')
            prior=db.scalar(select(AdminAudit).where(AdminAudit.actor_id==uid,AdminAudit.request_id==str(data.request_id)))
            signature=data.model_dump(mode='json')
            if prior:
                if prior.target_id!=target or prior.data.get('request')!=signature: raise HTTPException(409,'재시도 요청 내용이 다릅니다.')
                return {'ok':True,'replayed':True}
            required='CLEAR '+u.username if data.action=='clear' else u.username
            if data.confirmation!=required: raise HTTPException(422,'대상 사용자 확인 문구가 일치하지 않습니다.')
            q=ctx.fx.current_rate('USD','KRW');rate=q['rate'];now=datetime.now(timezone.utc)
            ws=wallets(db,u)
            before={c:str(w.balance) for c,w in ws.items()}
            if u.initial_krw is None:
                u.initial_krw=u.initial_usd*rate;u.initial_fx_date=q['date']
            if data.action=='grant':
                if data.amount<=0 or rounded(data.amount,data.currency)!=data.amount: raise HTTPException(422,'지원금과 통화별 최소 단위를 확인하세요.')
                if ws[data.currency].balance+data.amount>Decimal('1000000000000000'): raise HTTPException(409,'지갑 한도를 초과합니다.')
                ws[data.currency].balance+=data.amount
                u.net_contributions_krw+=data.amount*(rate if data.currency=='USD' else 1)
            elif data.action=='rebase':
                equity=ws['KRW'].balance+ws['USD'].balance*rate
                for p in db.scalars(select(Position).where(Position.user_id==target)):
                    quote=ctx.market.quote(p.symbol)
                    if quote.get('stale'): raise MarketError('오래된 시세가 있어 수익률 기준을 재설정할 수 없습니다.')
                    equity+=Decimal(str(quote.get('native_price',quote['price'])))*p.quantity*(1 if p.symbol.startswith('KR:') else rate)
                if equity<=0: raise HTTPException(409,'총자산이 0 이하인 계좌는 기준 재설정이 불가능합니다.')
                u.initial_krw=equity;u.initial_usd=equity/rate;u.net_contributions_krw=0;u.initial_fx_date=q['date'];u.performance_since=now;u.baseline_note='admin-rebase'
            else:
                # Preserve recovery evidence before removing user-facing records.
                models=[Position,Transaction,FxTransaction,LimitOrder,Watchlist,PopularityEvent]
                archive={m.__tablename__:records(db,m,target) for m in models}
                archive['wallets']=before;archive['actor']=uid;archive['reason']=data.reason
                archive['performance']={'initial_krw':u.initial_krw,'initial_usd':u.initial_usd,'net_contributions_krw':u.net_contributions_krw,'initial_fx_date':u.initial_fx_date,'performance_since':u.performance_since}
                archive['weekly_rows']=[]
                for report in db.scalars(select(WeeklyReport)):
                    matches=[r for r in report.rows if r['username']==u.username]
                    if matches:
                        archive['weekly_rows'].append({'report_id':report.id,'rows':matches})
                        rows=[r for r in report.rows if r['username']!=u.username]
                        last=None;rank=0
                        for i,r in enumerate(rows,1):
                            if r['return_pct']!=last: rank=i
                            r['rank']=rank;last=r['return_pct']
                        report.rows=rows
                db.add(SeasonArchive(user_id=target,label='전체 초기화',data=serial(archive),created_at=now))
                for model in models: db.execute(delete(model).where(model.user_id==target))
                amount=initial_amount(db);ws['USD'].balance=amount;ws['KRW'].balance=0
                u.initial_usd=amount;u.initial_krw=amount*rate;u.net_contributions_krw=0;u.initial_fx_date=q['date'];u.performance_since=now;u.baseline_note='admin-clear';u.records_since=now
            u.cash=ws['USD'].balance
            if data.action!='grant':
                state=db.get(WeeklyState,1)
                if state: state.baseline={k:v for k,v in state.baseline.items() if k!=str(target)}
            db.add(AdminAudit(actor_id=uid,target_id=target,request_id=str(data.request_id),action=data.action,reason=data.reason,data={'request':signature,'before':before,'after':{c:str(w.balance) for c,w in ws.items()},'fx_rate':str(rate),'fx_date':q['date']},created_at=now))
        return {'ok':True,'replayed':False}
