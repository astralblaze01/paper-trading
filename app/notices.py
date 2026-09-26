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


# Several notices can be up at once; the newest is listed first.
MAX_ACTIVE = 10


def active_notices(db):
    return list(db.scalars(select(SiteNotice).where(SiteNotice.active.is_(True)).order_by(SiteNotice.id.desc())))


def active_notice(db):
    """The newest active notice (the single-notice view older pages read)."""
    notices = active_notices(db)
    return notices[0] if notices else None


def public(notice):
    if not notice: return None
    return {'id': notice.id, 'kind': notice.kind, 'label': TEMPLATES[notice.kind]['label'], 'title': notice.title, 'body': notice.body, 'posted_at': notice.posted_at}


def post(db, uid, kind, title, body):
    from fastapi import HTTPException
    if len(active_notices(db)) >= MAX_ACTIVE: raise HTTPException(409, f'공지는 최대 {MAX_ACTIVE}개까지 게시할 수 있습니다. 기존 공지를 내린 뒤 등록하세요.')
    now = datetime.now(timezone.utc)
    row = SiteNotice(kind=kind, title=title, body=body, active=True, posted_by=uid, posted_at=now)
    db.add(row); db.flush()
    add_audit(db, uid, uid, 'notice_post', f'공지 등록 · {TEMPLATES[kind]["label"]} · {title}'[:300], {'notice_id': row.id, 'kind': kind}, at=now)
    return row


def clear(db, uid, notice_id=None, kind=None):
    """Take down one notice (by id), every notice of a kind, or all of them; False when none was up."""
    targets = [n for n in active_notices(db) if (notice_id is None or n.id == notice_id) and (kind is None or n.kind == kind)]
    if not targets: return False
    now = datetime.now(timezone.utc)
    for notice in targets:
        notice.active = False; notice.cleared_at = now
        add_audit(db, uid, uid, 'notice_clear', f'공지 해제 · {notice.title}'[:300], {'notice_id': notice.id, 'kind': notice.kind}, at=now)
    return True


def listing(db):
    notices = [public(n) for n in active_notices(db)]
    # 'notice' and 'maintenance' stay for pages loaded before several notices could be up.
    return {'notices': notices, 'notice': notices[0] if notices else None,
            'maintenance': any(n['kind'] == 'maintenance' for n in notices)}


def install_notices(app, admin, csrf):
    @app.get('/api/notice')
    def notice():
        with Session() as db: return listing(db)

    @app.post('/api/admin/notice', dependencies=[Depends(csrf)])
    def post_notice(data: NoticeInput, uid=Depends(admin)):
        from fastapi import HTTPException
        if not data.title or not data.body: raise HTTPException(422, '공지 제목과 내용을 입력하세요.')
        with Session.begin() as db:
            row = post(db, uid, data.kind, data.title, data.body)
            return listing(db) | {'posted': public(row)}

    @app.post('/api/admin/notice/{notice_id}/clear', dependencies=[Depends(csrf)])
    def clear_one(notice_id: int, uid=Depends(admin)):
        from fastapi import HTTPException
        with Session.begin() as db:
            if not clear(db, uid, notice_id=notice_id): raise HTTPException(404, '게시 중인 공지를 찾을 수 없습니다.')
            return listing(db) | {'cleared': True}

    @app.post('/api/admin/notice/clear', dependencies=[Depends(csrf)])
    def clear_notice(uid=Depends(admin)):
        with Session.begin() as db: return listing(db) | {'cleared': clear(db, uid)} | {'notices': [], 'notice': None, 'maintenance': False}

    # Earlier on/off switch, kept as a shortcut for the maintenance template.
    @app.post('/api/admin/maintenance', dependencies=[Depends(csrf)])
    def set_maintenance(data: MaintenanceInput, uid=Depends(admin)):
        with Session.begin() as db:
            up = any(n.kind == 'maintenance' for n in active_notices(db))
            if data.enabled and not up:
                template = TEMPLATES['maintenance']; post(db, uid, 'maintenance', template['title'], template['body'])
            elif not data.enabled and up:
                clear(db, uid, kind='maintenance')
        return {'maintenance': data.enabled}
