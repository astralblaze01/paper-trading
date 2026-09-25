"""Audited administrator operations. Never invoked at startup."""
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4
from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, delete, func, text, or_
from .db import ACCOUNT_LOCK, Session, User, Wallet, Settings, Position, Transaction, FxTransaction, LimitOrder, Watchlist, PopularityEvent, SeasonArchive, WeeklyReport, AdminAudit, WalletTransfer, UserAdminNote, lock_user
from .accounts import delete_account_data
from .money import wallets, initial_amount, rounded, bps
from .weekly import assign_ranks, drop_from_baseline

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

def add_audit(db,actor_id,target_id,action,reason,data,request_id=None,at=None):
    """Log one admin action. Retry-safe operations pass their request id; the rest get a fresh one."""
    db.add(AdminAudit(actor_id=actor_id,target_id=target_id,request_id=request_id or str(uuid4()),action=action,reason=reason,
                      data=data,created_at=at or datetime.now(timezone.utc)))

def serial(value): return json.loads(json.dumps(value,default=str))
def records(db,model,target):
    return [{c.name:getattr(row,c.name) for c in model.__table__.columns} for row in db.scalars(select(model).where(model.user_id==target))]

# Upper bound for a wallet after a grant, kept well inside the Numeric(20,4) users.cash mirror.
WALLET_CAP=Decimal('1000000000000000')

def _check_grant_amount(data):
    if data.amount<=0 or rounded(data.amount,data.currency)!=data.amount: raise HTTPException(422,'지원금과 통화별 최소 단위를 확인하세요.')

def _grant(user,ws,data,rate,over_cap):
    """A grant is outside money: it also raises net contributions, so it never shows up as return."""
    if ws[data.currency].balance+data.amount>WALLET_CAP: raise HTTPException(409,over_cap)
    ws[data.currency].balance+=data.amount
    user.net_contributions_krw+=data.amount*(rate if data.currency=='USD' else 1)

def _rebase(db,u,ws,quotes,market,q,now):
    """Restart the return from today's equity; cash and holdings stay."""
    rate=q['rate']
    equity=ws['KRW'].balance+ws['USD'].balance*rate
    for p in db.scalars(select(Position).where(Position.user_id==u.id)):
        quote=quotes.get(p.symbol) or market.quote(p.symbol)
        equity+=Decimal(str(quote.get('native_price',quote['price'])))*p.quantity*(1 if p.symbol.startswith('KR:') else rate)
    if equity<=0: raise HTTPException(409,'총자산이 0 이하인 계좌는 기준 재설정이 불가능합니다.')
    u.initial_krw=equity;u.initial_usd=equity/rate;u.net_contributions_krw=0;u.initial_fx_date=q['date'];u.performance_since=now;u.baseline_note='admin-rebase'

def _clear(db,u,ws,before,actor,reason,q,now):
    """Back to the state right after registration; everything removed is archived first."""
    target,rate=u.id,q['rate']
    # Preserve recovery evidence before removing user-facing records.
    models=[Position,Transaction,FxTransaction,LimitOrder,Watchlist,PopularityEvent]
    archive={m.__tablename__:records(db,m,target) for m in models}
    archive['wallet_transfers']=[{c.name:getattr(row,c.name) for c in WalletTransfer.__table__.columns} for row in db.scalars(select(WalletTransfer).where(or_(WalletTransfer.sender_id==target,WalletTransfer.recipient_id==target)))]
    archive['wallets']=before;archive['actor']=actor;archive['reason']=reason
    archive['performance']={'initial_krw':u.initial_krw,'initial_usd':u.initial_usd,'net_contributions_krw':u.net_contributions_krw,'initial_fx_date':u.initial_fx_date,'performance_since':u.performance_since}
    archive['weekly_rows']=[]
    for report in db.scalars(select(WeeklyReport)):
        matches=[r for r in report.rows if r['username']==u.username]
        if matches:
            archive['weekly_rows'].append({'report_id':report.id,'rows':matches})
            rows=[r for r in report.rows if r['username']!=u.username]
            assign_ranks(rows)
            report.rows=rows
    db.add(SeasonArchive(user_id=target,label='전체 초기화',data=serial(archive),created_at=now))
    for model in models: db.execute(delete(model).where(model.user_id==target))
    amount=initial_amount(db);ws['USD'].balance=amount;ws['KRW'].balance=0
    u.initial_usd=amount;u.initial_krw=amount*rate;u.net_contributions_krw=0;u.initial_fx_date=q['date'];u.performance_since=None;u.baseline_note='registration'
    # Transfers also belong to the counterparty: keep the rows and
    # hide them from this user's history from now on.
    u.records_since=now

def admin_overview(market,health):
    """Everything the admin page shows: accounts, settings, the notice, provider diagnostics."""
    from .us_quotes import diagnostics as us_diagnostics
    names=['FX_FEE_BPS','FX_SPREAD_BPS','US_BUY_FEE_BPS','US_SELL_FEE_BPS','KR_BUY_FEE_BPS','KR_SELL_FEE_BPS','KR_SELL_TAX_BPS']
    with Session() as db:
        notes=dict(db.execute(select(UserAdminNote.user_id,UserAdminNote.note)).all())
        users=[{'id':u.id,'username':u.username,'active':u.active,'admin':u.is_admin,'initial_usd':u.initial_usd,'initial_krw':u.initial_krw,'note':notes.get(u.id,''),'wallets':{w.currency:w.balance for w in db.scalars(select(Wallet).where(Wallet.user_id==u.id))}} for u in db.scalars(select(User).order_by(User.id))]
        amount=initial_amount(db)
        counts={'users':db.scalar(select(func.count()).select_from(User)),'transactions':db.scalar(select(func.count()).select_from(Transaction)),'positions':db.scalar(select(func.count()).select_from(Position)),'pending_orders':db.scalar(select(func.count()).select_from(LimitOrder).where(LimitOrder.status=='pending'))}
    from .notices import active_notice, public, TEMPLATES
    with Session() as db: notice=public(active_notice(db))
    return {'users':users,'initial_usd':amount,'notice':notice,'notice_templates':TEMPLATES,'maintenance':bool(notice and notice['kind']=='maintenance'),'fees':{n:bps(n,'10' if n=='FX_FEE_BPS' else '5' if n=='FX_SPREAD_BPS' else '0') for n in names},'health':health(),'providers':market.status(),'counts':counts,'us_market':us_diagnostics(market),'kr_market':us_diagnostics(market,'KR')}

def set_initial_amount(actor,amount):
    with Session.begin() as db:
        before=initial_amount(db)
        db.merge(Settings(key='INITIAL_USD',value=str(amount)))
        add_audit(db,actor,actor,'initial_amount','초기 지급액 변경',{'before':str(before),'after':str(amount)})

def set_account_active(actor,target,active):
    with Session.begin() as db:
        u=lock_user(db,target)
        if not u: raise HTTPException(404,'사용자가 없습니다.')
        add_audit(db,actor,target,'account_status','계정 상태 변경',{'before':u.active,'after':active})
        u.active=active

def season_reset(actor,target,label,q):
    """Archive the season and restart the account from the initial funding at rate quote q."""
    with Session.begin() as db:
        db.execute(text('SELECT pg_advisory_xact_lock(:k)'),{'k':ACCOUNT_LOCK})
        u=lock_user(db,target)
        if not u: raise HTTPException(404,'사용자가 없습니다.')
        ws=wallets(db,u); ps=list(db.scalars(select(Position).where(Position.user_id==target)))
        snapshot={'wallets':{c:str(w.balance) for c,w in ws.items()},'positions':[{'symbol':p.symbol,'quantity':p.quantity,'average_cost':str(p.average_cost),'native_average_cost':str(p.native_average_cost)} for p in ps],'initial_krw':str(u.initial_krw),'actor':actor}
        db.add(SeasonArchive(user_id=target,label=label,data=snapshot,created_at=datetime.now(timezone.utc)))
        add_audit(db,actor,target,'season_reset',label,{'archived':True})
        for p in ps: db.delete(p)
        for o in db.scalars(select(LimitOrder).where(LimitOrder.user_id==target,LimitOrder.status=='pending')): o.status='cancelled'; o.reason='관리자 초기화'
        amount=initial_amount(db); ws['USD'].balance=amount; ws['KRW'].balance=0; u.cash=amount; u.initial_usd=amount; u.initial_krw=amount*q['rate']; u.initial_fx_date=q['date']; u.baseline_note='admin-reset'; u.net_contributions_krw=0; u.performance_since=datetime.now(timezone.utc)
        drop_from_baseline(db,target)

def list_archives():
    with Session() as db: return [{'id':a.id,'user_id':a.user_id,'label':a.label,'data':a.data,'created_at':a.created_at} for a in db.scalars(select(SeasonArchive).order_by(SeasonArchive.id.desc()).limit(100))]

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
            u=lock_user(db,target)
            if not u: raise HTTPException(404,'사용자가 없습니다.')
            if data.action=='delete':
                if target==uid: raise HTTPException(409,'현재 로그인한 관리자 계정은 삭제할 수 없습니다.')
                username=delete_account_data(db,u)
                add_audit(db,uid,uid,'account_delete',reason,{'request':signature,'deleted_username':username,'deleted_user_id':target},request_id=str(data.request_id))
                return {'ok':True,'replayed':False,'deleted':username}
            q=ctx.fx.current_rate('USD','KRW');rate=q['rate'];now=datetime.now(timezone.utc)
            ws=wallets(db,u)
            before={c:str(w.balance) for c,w in ws.items()}
            if u.initial_krw is None:
                u.initial_krw=u.initial_usd*rate;u.initial_fx_date=q['date']
            if data.action=='grant':
                _check_grant_amount(data)
                _grant(u,ws,data,rate,'지갑 한도를 초과합니다.')
            elif data.action=='rebase': _rebase(db,u,ws,quotes=quotes,market=ctx.market,q=q,now=now)
            else: _clear(db,u,ws,before=before,actor=uid,reason=reason,q=q,now=now)
            u.cash=ws['USD'].balance
            if data.action!='grant': drop_from_baseline(db,target)
            add_audit(db,uid,target,data.action,reason,{'request':signature,'before':before,'after':{c:str(w.balance) for c,w in ws.items()},'fx_rate':str(rate),'fx_date':q['date']},request_id=str(data.request_id),at=now)
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
            _check_grant_amount(data)
            q=ctx.fx.current_rate('USD','KRW');rate=q['rate']
            users=list(db.scalars(select(User).where(User.active.is_(True),User.is_admin.is_(False)).order_by(User.id).with_for_update()))
            for user in users:
                ws=wallets(db,user)
                _grant(user,ws,data,rate,f'{user.username} 지갑 한도를 초과합니다.')
                user.cash=ws['USD'].balance
            add_audit(db,uid,uid,'bulk_grant',data.reason.strip() or '전체 지원금 지급',{'request':signature,'count':len(users),'currency':data.currency,'amount':str(data.amount)},request_id=str(data.request_id))
            return {'ok':True,'replayed':False,'count':len(users)}
