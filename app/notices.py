"""Site notices: templates such as the server-maintenance notice, or a
notice the administrator writes. Informational only; nothing is blocked."""
from datetime import datetime, timezone
from typing import Literal
from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, update
from .db import Session, SiteNotice
from .admin_ops import add_audit

TEMPLATES = {
    'maintenance': {'label': '서버 점검 예고', 'title': '서버 점검 예정',
                    'body': '서버 점검이 예정되어 있습니다. 점검 중에는 일시적으로 서비스 이용이 어려울 수 있습니다.'},
    'update': {'label': '업데이트 안내', 'title': '서비스 업데이트 안내',
               'body': '서비스가 업데이트되었습니다. 새로워진 기능을 확인해 보세요.'},
    'general': {'label': '일반 공지', 'title': '', 'body': ''},
}


def _clean(value):
    # Plain text only; line breaks are kept, other control characters dropped.
    return ''.join(ch for ch in value if ch == '\n' or ch >= ' ').strip()


class NoticeInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['maintenance', 'update', 'general']
    title: str = Field(max_length=60)
    body: str = Field(max_length=500)
    @field_validator('title', 'body')
    @classmethod
    def clean(cls, value): return _clean(value)


class MaintenanceInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool


def active_notice(db):
    return db.scalar(select(SiteNotice).where(SiteNotice.active.is_(True)).order_by(SiteNotice.id.desc()).limit(1))


def public(notice):
    if not notice: return None
    return {'id': notice.id, 'kind': notice.kind, 'label': TEMPLATES[notice.kind]['label'], 'title': notice.title, 'body': notice.body, 'posted_at': notice.posted_at}


def post(db, uid, kind, title, body):
    now = datetime.now(timezone.utc)
    db.execute(update(SiteNotice).where(SiteNotice.active.is_(True)).values(active=False, cleared_at=now))
    row = SiteNotice(kind=kind, title=title, body=body, active=True, posted_by=uid, posted_at=now)
    db.add(row); db.flush()
    add_audit(db, uid, uid, 'notice_post', f'공지 등록 · {TEMPLATES[kind]["label"]} · {title}'[:300], {'notice_id': row.id, 'kind': kind}, at=now)
    return row


def clear(db, uid):
    current = active_notice(db)
    if not current: return False
    now = datetime.now(timezone.utc)
    db.execute(update(SiteNotice).where(SiteNotice.active.is_(True)).values(active=False, cleared_at=now))
    add_audit(db, uid, uid, 'notice_clear', f'공지 해제 · {current.title}'[:300], {'notice_id': current.id, 'kind': current.kind}, at=now)
    return True


def install_notices(app, admin, csrf):
    @app.get('/api/notice')
    def notice():
        with Session() as db: current = active_notice(db)
        # 'maintenance' stays for pages loaded before notices had types.
        return {'notice': public(current), 'maintenance': bool(current and current.kind == 'maintenance')}

    @app.post('/api/admin/notice', dependencies=[Depends(csrf)])
    def post_notice(data: NoticeInput, uid=Depends(admin)):
        from fastapi import HTTPException
        if not data.title or not data.body: raise HTTPException(422, '공지 제목과 내용을 입력하세요.')
        with Session.begin() as db: return {'notice': public(post(db, uid, data.kind, data.title, data.body))}

    @app.post('/api/admin/notice/clear', dependencies=[Depends(csrf)])
    def clear_notice(uid=Depends(admin)):
        with Session.begin() as db: return {'cleared': clear(db, uid), 'notice': None}

    # Earlier on/off switch, kept as a shortcut for the maintenance template.
    @app.post('/api/admin/maintenance', dependencies=[Depends(csrf)])
    def set_maintenance(data: MaintenanceInput, uid=Depends(admin)):
        with Session.begin() as db:
            current = active_notice(db)
            if data.enabled and not (current and current.kind == 'maintenance'):
                template = TEMPLATES['maintenance']; post(db, uid, 'maintenance', template['title'], template['body'])
            elif not data.enabled and current and current.kind == 'maintenance':
                clear(db, uid)
        return {'maintenance': data.enabled}
