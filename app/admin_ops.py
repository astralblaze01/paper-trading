"""Audited administrator operations. Never invoked at startup."""
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal
from uuid import UUID
from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, delete, text, or_
from .db import ACCOUNT_LOCK, Session, User, Position, Transaction, FxTransaction, LimitOrder, Watchlist, PopularityEvent, SeasonArchive, WeeklyState, WeeklyReport, AdminAudit, WalletTransfer, UserAdminNote
from .accounts import delete_account_data
from .money import wallets, initial_amount, rounded

class ManagementInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    action: Literal['grant','rebase','clear','delete']
    currency: Literal['USD','KRW']='USD'
    amount: Decimal=Field(default=Decimal(0),ge=0,le=1000000000,max_digits=18,decimal_places=4)
    reason: str=Field(default='',max_length=300)
    # Accepted for older clients only; target confirmation phrases were removed.
    confirmation: str=Field(default='',max_length=64)
    request_id: UUID

ACTION_LABELS={'grant':'지원금 지급','rebase':'수익률 기준 재설정','clear':'회원가입 직후 상태로 초기화','delete':'계정 영구 삭제'}

class NoteInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    note: str=Field(max_length=200)

def serial(value): return json.loads(json.dumps(value,default=str))
def records(db,model,target):
    return [{c.name:getattr(row,c.name) for c in model.__table__.columns} for row in db.scalars(select(model).where(model.user_id==target))]

def install_admin_ops(app,ctx,admin,csrf):
    @app.get('/api/admin/audit')
    def audit(uid=Depends(admin)):
        with Session() as db:
            names=dict(db.execute(select(User.id,User.username)).all())
            return [{'id':r.id,'actor':names.get(r.actor_id),'target':names.get(r.target_id),'action':r.action,'reason':r.reason,'data':r.data,'created_at':r.created_at} for r in db.scalars(select(AdminAudit).order_by(AdminAudit.id.desc()).limit(100))]

    @app.get('/api/admin/users/search')
    def search_users(q:str='',uid=Depends(admin)):
        term=q.strip()[:60]
        pattern='%'+term.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
        with Session() as db:
            query=select(User.id,User.username,User.is_admin,User.active,UserAdminNote.note).outerjoin(UserAdminNote,UserAdminNote.user_id==User.id)
            if term: query=query.where(or_(User.username.ilike(pattern,escape='\\'),UserAdminNote.note.ilike(pattern,escape='\\')))
            return [{'id':i,'username':name,'admin':is_admin,'active':active,'note':note or ''} for i,name,is_admin,active,note in db.execute(query.order_by(User.username).limit(50))]

    @app.post('/api/admin/users/{target}/note',dependencies=[Depends(csrf)])
    def save_note(target:int,data:NoteInput,uid=Depends(admin)):
        now=datetime.now(timezone.utc);note=data.note.strip()
        with Session.begin() as db:
            if not db.get(User,target): raise HTTPException(404,'사용자가 없습니다.')
            row=db.get(UserAdminNote,target,with_for_update=True)
            if not note:
                if row: db.delete(row)
            elif row: row.note=note;row.updated_at=now
            else: db.add(UserAdminNote(user_id=target,note=note,created_at=now,updated_at=now))
        return {'ok':True,'note':note}

    @app.post('/api/admin/users/{target}/manage',dependencies=[Depends(csrf)])
    def manage(target:int,data:ManagementInput,uid=Depends(admin)):
        reason=data.reason.strip() or ACTION_LABELS[data.action]
        quotes={}
        if data.action=='rebase':
            # Quote outside the account lock. The baseline uses the same last
            # provider prices as the portfolio, so it also works while the
            # market is closed.
            with Session() as db: symbols=list(db.scalars(select(Position.symbol).where(Position.user_id==target)))
            quotes={symbol:ctx.market.quote(symbol) for symbol in symbols}
        with Session.begin() as db:
            db.execute(text('SELECT pg_advisory_xact_lock(:k)'),{'k':ACCOUNT_LOCK})
            prior=db.scalar(select(AdminAudit).where(AdminAudit.actor_id==uid,AdminAudit.request_id==str(data.request_id)))
            signature=data.model_dump(mode='json')
            if prior:
                same_target=prior.target_id==target or (prior.action=='account_delete' and prior.data.get('deleted_user_id')==target)
                if not same_target or prior.data.get('request')!=signature: raise HTTPException(409,'재시도 요청 내용이 다릅니다.')
                return {'ok':True,'replayed':True}
            u=db.scalar(select(User).where(User.id==target).with_for_update())
            if not u: raise HTTPException(404,'사용자가 없습니다.')
            if data.action=='delete':
                if target==uid: raise HTTPException(409,'현재 로그인한 관리자 계정은 삭제할 수 없습니다.')
                username=delete_account_data(db,u)
                db.add(AdminAudit(actor_id=uid,target_id=uid,request_id=str(data.request_id),action='account_delete',reason=reason,data={'request':signature,'deleted_username':username,'deleted_user_id':target},created_at=datetime.now(timezone.utc)))
                return {'ok':True,'replayed':False,'deleted':username}
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
                    quote=quotes.get(p.symbol) or ctx.market.quote(p.symbol)
                    equity+=Decimal(str(quote.get('native_price',quote['price'])))*p.quantity*(1 if p.symbol.startswith('KR:') else rate)
                if equity<=0: raise HTTPException(409,'총자산이 0 이하인 계좌는 기준 재설정이 불가능합니다.')
                u.initial_krw=equity;u.initial_usd=equity/rate;u.net_contributions_krw=0;u.initial_fx_date=q['date'];u.performance_since=now;u.baseline_note='admin-rebase'
            else:
                # Preserve recovery evidence before removing user-facing records.
                models=[Position,Transaction,FxTransaction,LimitOrder,Watchlist,PopularityEvent]
                archive={m.__tablename__:records(db,m,target) for m in models}
                archive['wallet_transfers']=[{c.name:getattr(row,c.name) for c in WalletTransfer.__table__.columns} for row in db.scalars(select(WalletTransfer).where(or_(WalletTransfer.sender_id==target,WalletTransfer.recipient_id==target)))]
                archive['wallets']=before;archive['actor']=uid;archive['reason']=reason
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
                # Transfers also belong to the counterparty: keep the rows and
                # hide them from this user's history from now on.
                amount=initial_amount(db);ws['USD'].balance=amount;ws['KRW'].balance=0
                u.initial_usd=amount;u.initial_krw=amount*rate;u.net_contributions_krw=0;u.initial_fx_date=q['date'];u.performance_since=None;u.baseline_note='registration';u.records_since=now
            u.cash=ws['USD'].balance
            if data.action!='grant':
                state=db.get(WeeklyState,1)
                if state: state.baseline={k:v for k,v in state.baseline.items() if k!=str(target)}
            db.add(AdminAudit(actor_id=uid,target_id=target,request_id=str(data.request_id),action=data.action,reason=reason,data={'request':signature,'before':before,'after':{c:str(w.balance) for c,w in ws.items()},'fx_rate':str(rate),'fx_date':q['date']},created_at=now))
        return {'ok':True,'replayed':False}

    @app.post('/api/admin/users/manage-all',dependencies=[Depends(csrf)])
    def manage_all(data:ManagementInput,uid=Depends(admin)):
        if data.action!='grant': raise HTTPException(422,'모든 사용자 대상 작업은 지원금 지급만 허용합니다.')
        with Session.begin() as db:
            db.execute(text('SELECT pg_advisory_xact_lock(:k)'),{'k':ACCOUNT_LOCK})
            prior=db.scalar(select(AdminAudit).where(AdminAudit.actor_id==uid,AdminAudit.request_id==str(data.request_id)))
            signature=data.model_dump(mode='json')
            if prior:
                if prior.data.get('request')!=signature: raise HTTPException(409,'재시도 요청 내용이 다릅니다.')
                return {'ok':True,'replayed':True,'count':prior.data.get('count',0)}
            if data.amount<=0 or rounded(data.amount,data.currency)!=data.amount: raise HTTPException(422,'지원금과 통화별 최소 단위를 확인하세요.')
            q=ctx.fx.current_rate('USD','KRW');rate=q['rate']
            users=list(db.scalars(select(User).where(User.active.is_(True),User.is_admin.is_(False)).order_by(User.id).with_for_update()))
            for user in users:
                ws=wallets(db,user)
                if ws[data.currency].balance+data.amount>Decimal('1000000000000000'): raise HTTPException(409,f'{user.username} 지갑 한도를 초과합니다.')
                ws[data.currency].balance+=data.amount
                user.net_contributions_krw+=data.amount*(rate if data.currency=='USD' else 1)
                user.cash=ws['USD'].balance
            db.add(AdminAudit(actor_id=uid,target_id=uid,request_id=str(data.request_id),action='bulk_grant',reason=data.reason.strip() or '전체 지원금 지급',data={'request':signature,'count':len(users),'currency':data.currency,'amount':str(data.amount)},created_at=datetime.now(timezone.utc)))
            return {'ok':True,'replayed':False,'count':len(users)}
