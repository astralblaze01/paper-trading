"""AI traders' decision log, and the administrator's list of AI accounts.

An AI account (users.is_ai) posts one record per hourly run from scripts/ai_trader.py.
The account reads its own log; an administrator reads it, and everything else the
account owns, through the x-view-as header (see main.view_as)."""
import json
from datetime import datetime, timezone
from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, func
from .db import Session, User, AiDecision
from .market import MarketError

MAX_DECISION_BYTES = 200_000   # nginx lets this one path send up to 256 KB
DECISIONS_PAGE = 24            # a day of hourly runs
# The bulky part of a record: the data the AI looked at. Lists leave it out; one record has it.
HEAVY_FIELDS = ('research_data', 'account')


class DecisionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    status: str = Field(pattern=r'^(ok|error)$')
    summary: str = Field(max_length=400)
    data: dict


def decision_row(d, full=False):
    data = d.data if full else {k: v for k, v in d.data.items() if k not in HEAVY_FIELDS}
    return {'id': d.id, 'created_at': d.created_at, 'status': d.status, 'summary': d.summary, 'data': data}


def install_ai(app, ctx):
    user, csrf = ctx.current_user, ctx.csrf

    def ai_account(uid):
        with Session() as db:
            if not db.scalar(select(User.is_ai).where(User.id == uid)): raise HTTPException(403, 'AI 계정만 판단 기록을 남길 수 있습니다.')

    @app.post('/api/ai/decisions', dependencies=[Depends(csrf)])
    def log_decision(entry: DecisionInput, uid=Depends(user)):
        ai_account(uid)
        if len(json.dumps(entry.data, ensure_ascii=False).encode()) > MAX_DECISION_BYTES:
            raise HTTPException(413, '판단 기록이 너무 큽니다.')
        with Session.begin() as db:
            row = AiDecision(user_id=uid, created_at=datetime.now(timezone.utc), status=entry.status, summary=entry.summary, data=entry.data)
            db.add(row); db.flush()
            return {'id': row.id}

    @app.get('/api/ai/decisions')
    def decisions(before: int | None = Query(None, ge=1), uid=Depends(user)):
        """Newest first, a page at a time; `before` is the oldest id already shown."""
        ai_account(uid)
        where = [AiDecision.user_id == uid] + ([AiDecision.id < before] if before else [])
        with Session() as db:
            rows = db.scalars(select(AiDecision).where(*where).order_by(AiDecision.id.desc()).limit(DECISIONS_PAGE + 1)).all()
        return {'rows': [decision_row(d) for d in rows[:DECISIONS_PAGE]], 'more': len(rows) > DECISIONS_PAGE}

    @app.get('/api/ai/decisions/{decision_id}')
    def decision(decision_id: int, uid=Depends(user)):
        ai_account(uid)
        with Session() as db:
            d = db.get(AiDecision, decision_id)
            if not d or d.user_id != uid: raise HTTPException(404, '판단 기록을 찾을 수 없습니다.')
            return decision_row(d, full=True)

    @app.get('/api/admin/ai')
    def ai_accounts(uid=Depends(user)):
        """Every AI account with its latest run and current standing, for the admin AI page."""
        with Session() as db:
            if not db.scalar(select(User.is_admin).where(User.id == uid)): raise HTTPException(403, '관리자 권한이 필요합니다.')
            agents = db.scalars(select(User).where(User.is_ai.is_(True)).order_by(User.username)).all()
            counts = dict(db.execute(select(AiDecision.user_id, func.count()).group_by(AiDecision.user_id)).all())
            latest = {a.id: db.scalar(select(AiDecision).where(AiDecision.user_id == a.id).order_by(AiDecision.id.desc()).limit(1)) for a in agents}
        rows = []
        for a in agents:
            last = latest[a.id]
            try: p = ctx.wallet_portfolio(a.id, ctx.market, ctx.fx)
            except (MarketError, HTTPException): p = {}
            rows.append({'username': a.username, 'active': a.active, 'created_at': a.created_at, 'decisions': counts.get(a.id, 0),
                         'equity_usd': p.get('equity_usd'), 'return_pct': p.get('return_pct'), 'return_pct_usd': p.get('return_pct_usd'),
                         'model': (last.data.get('model') if last else None),
                         'last': {'created_at': last.created_at, 'status': last.status, 'summary': last.summary} if last else None})
        return rows
