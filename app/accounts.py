"""Account removal, public profiles and profile images."""
import io
import warnings
from datetime import datetime, timezone
from fastapi import Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, delete, update, or_, text
from .db import (ACCOUNT_LOCK, Session, PerformanceSnapshot, User, Position, Wallet, Transaction, FxTransaction, LimitOrder, Watchlist, PopularityEvent,
                 SeasonArchive, WeeklyReport, AdminAudit, WalletTransfer, UserAdminNote, UserProfile, UserProfileImage, lock_user)
from .weekly import drop_from_baseline

MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_TYPES = {'image/jpeg': 'JPEG', 'image/png': 'PNG', 'image/webp': 'WEBP'}
AVATAR_SIZE = 512
BIO_LENGTH = 160


def delete_account_data(db, user):
    """Remove one account and everything it owns inside the caller's transaction.

    Transfers stay in the counterparty's history with this side detached, so
    deleting one account never changes another user's records."""
    target, username = user.id, user.username
    for report in db.scalars(select(WeeklyReport)):
        if any(r.get('username') == username for r in report.rows):
            report.rows = [r for r in report.rows if r.get('username') != username]
    drop_from_baseline(db, target)
    db.execute(update(WalletTransfer).where(WalletTransfer.sender_id == target).values(sender_id=None))
    db.execute(update(WalletTransfer).where(WalletTransfer.recipient_id == target).values(recipient_id=None))
    for model in (Position, Transaction, FxTransaction, LimitOrder, Watchlist, PopularityEvent, SeasonArchive, Wallet,
                  UserAdminNote, UserProfileImage, UserProfile, PerformanceSnapshot):
        db.execute(delete(model).where(model.user_id == target))
    db.execute(delete(AdminAudit).where(or_(AdminAudit.actor_id == target, AdminAudit.target_id == target)))
    db.delete(user)
    db.flush()
    return username


def membership_days(created_at, now=None):
    """Days since sign-up in Korea time; the sign-up day itself counts as day 1."""
    if created_at is None: return None
    from zoneinfo import ZoneInfo
    seoul = ZoneInfo('Asia/Seoul')
    today = (now or datetime.now(timezone.utc)).astimezone(seoul).date()
    return max(1, (today - created_at.astimezone(seoul).date()).days + 1)


def profile_of(db, uid):
    row = db.get(UserProfile, uid)
    return {'bio': row.bio if row else '', 'image_version': row.image_version if row else 0}


def profile_versions(db, ids):
    return dict(db.execute(select(UserProfile.user_id, UserProfile.image_version).where(UserProfile.user_id.in_(ids))).all()) if ids else {}


def _profile_row(db, uid):
    row = db.get(UserProfile, uid, with_for_update=True)
    if row is None:
        now = datetime.now(timezone.utc)
        row = UserProfile(user_id=uid, bio='', image_version=0, created_at=now, updated_at=now)
        db.add(row)
    return row


def normalize_image(raw, declared_type):
    """Decode, verify and re-encode an upload. Returns WebP bytes.

    The declared MIME type must match the decoded format, so a renamed file or
    a mislabeled body is rejected. Re-encoding drops metadata and any trailing
    payload; only pixels leave this function."""
    from PIL import Image, ImageOps, UnidentifiedImageError
    expected = IMAGE_TYPES.get(declared_type)
    if not expected: raise HTTPException(415, 'JPG, PNG, WEBP 이미지만 업로드할 수 있습니다.')
    if not raw: raise HTTPException(422, '이미지 파일이 비어 있습니다.')
    if len(raw) > MAX_IMAGE_BYTES: raise HTTPException(413, '이미지는 최대 5MB까지 업로드할 수 있습니다.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as probe:
                if probe.format != expected: raise HTTPException(415, '파일 형식과 이미지 내용이 일치하지 않습니다.')
                if probe.width * probe.height > 40_000_000: raise HTTPException(413, '이미지 해상도가 너무 큽니다.')
                probe.verify()
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
                image = ImageOps.exif_transpose(image)
                image = image.convert('RGBA' if 'A' in image.getbands() or image.mode == 'P' else 'RGB')
                image.thumbnail((AVATAR_SIZE, AVATAR_SIZE))
                out = io.BytesIO()
                image.save(out, 'WEBP', quality=85, method=4)
                return out.getvalue()
    except HTTPException: raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HTTPException(415, '올바른 이미지 파일이 아닙니다.')


class BioInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    bio: str = Field(max_length=BIO_LENGTH)
    @field_validator('bio')
    @classmethod
    def clean(cls, value):
        # Plain text only; control characters other than line breaks are dropped.
        return ''.join(ch for ch in value if ch == '\n' or ch >= ' ').strip()


class WithdrawInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    password: str = Field(min_length=1, max_length=128)


def install_accounts(app, ctx):
    user, csrf = ctx.current_user, ctx.csrf

    @app.get('/api/profile')
    def my_profile(uid=Depends(user)):
        with Session() as db:
            me = db.get(User, uid)
            return {'username': me.username, **profile_of(db, uid), 'bio_max_length': BIO_LENGTH,
                    'member_since': me.created_at, 'member_days': membership_days(me.created_at)}

    @app.post('/api/profile', dependencies=[Depends(csrf)])
    def save_profile(data: BioInput, uid=Depends(user)):
        with Session.begin() as db:
            row = _profile_row(db, uid)
            row.bio = data.bio; row.updated_at = datetime.now(timezone.utc)
            return {'bio': row.bio, 'image_version': row.image_version}

    @app.post('/api/profile/image', dependencies=[Depends(csrf)])
    async def upload_image(request: Request, uid=Depends(user)):
        declared = request.headers.get('content-type', '').split(';')[0].strip().lower()
        if declared not in IMAGE_TYPES: raise HTTPException(415, 'JPG, PNG, WEBP 이미지만 업로드할 수 있습니다.')
        length = request.headers.get('content-length')
        if length and length.isdigit() and int(length) > MAX_IMAGE_BYTES:
            raise HTTPException(413, '이미지는 최대 5MB까지 업로드할 수 있습니다.')
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_IMAGE_BYTES: raise HTTPException(413, '이미지는 최대 5MB까지 업로드할 수 있습니다.')
            chunks.append(chunk)
        data = normalize_image(b''.join(chunks), declared)
        now = datetime.now(timezone.utc)
        with Session.begin() as db:
            row = _profile_row(db, uid)
            db.merge(UserProfileImage(user_id=uid, content_type='image/webp', data=data, updated_at=now))
            row.image_version += 1; row.updated_at = now
            return {'bio': row.bio, 'image_version': row.image_version}

    @app.post('/api/profile/image/delete', dependencies=[Depends(csrf)])
    def delete_image(uid=Depends(user)):
        with Session.begin() as db:
            row = _profile_row(db, uid)
            db.execute(delete(UserProfileImage).where(UserProfileImage.user_id == uid))
            row.image_version = 0; row.updated_at = datetime.now(timezone.utc)
            return {'bio': row.bio, 'image_version': 0}

    @app.get('/api/users/{username}/avatar')
    def avatar(username: str, uid=Depends(user)):
        with Session() as db:
            target = db.scalar(select(User.id).where(User.username == username.lower(), User.active.is_(True)))
            image = db.get(UserProfileImage, target) if target else None
            if not image: raise HTTPException(404, '프로필 이미지가 없습니다.')
            # URLs carry ?v=<image_version>, so a changed image gets a new URL.
            return Response(image.data, media_type='image/webp', headers={'Cache-Control': 'private, max-age=86400'})

    @app.post('/api/account/delete', dependencies=[Depends(csrf)])
    def withdraw(data: WithdrawInput, request: Request, uid=Depends(user)):
        from argon2.exceptions import VerificationError, InvalidHashError
        with Session.begin() as db:
            # The account-wide lock, so no admin operation or weekly run sees a half-removed account.
            db.execute(text('SELECT pg_advisory_xact_lock(:k)'), {'k': ACCOUNT_LOCK})
            me = lock_user(db, uid)
            if me.is_admin: raise HTTPException(409, '관리자 계정은 회원 탈퇴할 수 없습니다.')
            try: ctx.hasher.verify(me.password_hash, data.password)
            except (VerificationError, InvalidHashError): raise HTTPException(401, '비밀번호가 올바르지 않습니다.')
            delete_account_data(db, me)
        request.session.clear()
        return {'ok': True}
