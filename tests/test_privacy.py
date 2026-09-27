"""Privacy choices are enforced on every read, including cached/historical views."""
from datetime import datetime, timezone
from sqlalchemy import select, func, text
from test_service import database, client, register
from app import main
from app.accounts import PRIVACY_NOTICE_VERSION
from app.db import Session, User, PrivacyChoice, UserProfile, UserProfileImage, WeeklyReport, engine
from app.migrations import migrate


def choose(client, token, profile=False, ranking=False, version=PRIVACY_NOTICE_VERSION):
    return client.post('/api/profile/privacy', headers={'x-csrf-token': token},
                       json={'profile_public': profile, 'ranking_public': ranking, 'notice_version': version})


def test_registration_is_private_and_choices_are_independent(client):
    token = register(client, 'owner', public=False)
    profile = client.get('/api/profile').json()
    assert profile['profile_public'] is False and profile['ranking_public'] is False
    with Session.begin() as db:
        owner = db.scalar(select(User).where(User.username == 'owner'))
        db.add(UserProfile(user_id=owner.id, bio='private', image_version=1,
                           created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc)))
        db.add(UserProfileImage(user_id=owner.id, content_type='image/webp', data=b'image', updated_at=datetime.now(timezone.utc)))
    assert client.get('/api/users/owner/avatar').status_code == 200  # own photo stays available
    assert choose(client, token, ranking=True).status_code == 200
    assert [r['username'] for r in client.get('/api/ranking').json()['rows']] == ['owner']
    assert client.get('/api/ranking').json()['rows'][0]['image_version'] == 0
    register(client, 'viewer', public=False)
    for path in ('portfolios/owner', 'performance/owner', 'users/owner/avatar'):
        assert client.get('/api/' + path).status_code == 404
    # Return to the owner and publish only the profile.
    token = client.get('/api/session').json()['csrf']
    assert client.post('/api/login', headers={'x-csrf-token': token}, json={'username':'owner', 'password':'a-secure-password-123'}).status_code == 200
    token = client.get('/api/session').json()['csrf']
    assert choose(client, token, profile=True).status_code == 200
    assert client.get('/api/portfolios/owner').status_code == 200
    assert client.get('/api/performance/owner').status_code == 200
    assert not client.get('/api/ranking').json()['rows']


def test_revocation_hides_cached_ranking_and_historical_weekly(client):
    token = register(client, 'owner')
    assert client.get('/api/ranking').json()['rows']  # warm cache
    now = datetime.now(timezone.utc)
    with Session.begin() as db:
        db.add(WeeklyReport(scheduled_for=now, period_start=now, period_end=now,
                           rows=[{'username':'owner', 'return_pct':'1', 'rank':1}],
                           notes={'excluded_new_or_zero':0, 'late':False, 'quotes':{}}))
    assert client.get('/api/weekly').json()['reports'][0]['rows']
    assert choose(client, token).status_code == 200
    assert not client.get('/api/ranking').json()['rows']
    assert not client.get('/api/weekly').json()['reports'][0]['rows']
    assert client.get('/api/portfolios/owner').status_code == 404
    assert client.get('/api/portfolio').status_code == 200
    with Session() as db:
        changes = list(db.scalars(select(PrivacyChoice).order_by(PrivacyChoice.id)))
        assert [(c.profile_public, c.ranking_public) for c in changes] == [(True, True), (False, False)]
        assert all(c.notice_version == PRIVACY_NOTICE_VERSION and c.created_at for c in changes)
    # A stale cache from another process must also filter using current DB choices.
    main._ranking_cache[main.market] = {'bucket': main._ranking_bucket(), 'payload': main._ranking_payload(
        [{'username':'owner', 'image_version':99}], [], False, now.isoformat(), False)}
    assert not client.get('/api/ranking').json()['rows']


def test_privacy_updates_require_auth_csrf_current_notice_and_real_booleans(client):
    token = client.get('/api/session').json()['csrf']
    assert choose(client, token).status_code == 401
    token = register(client, public=False)
    assert choose(client, 'wrong').status_code == 403
    assert choose(client, token, version='old').status_code == 409
    assert choose(client, token, profile='true').status_code == 422
    assert choose(client, token).status_code == 200
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(PrivacyChoice)) == 0
    assert choose(client, token, profile=True).status_code == 200
    assert client.post('/api/account/delete', headers={'x-csrf-token': token},
                       json={'password':'a-secure-password-123'}).status_code == 200
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(PrivacyChoice)) == 0


def test_migration_existing_accounts_become_private_without_losing_balances():
    with Session.begin() as db:
        me = User(username='legacy', password_hash='preserve', cash=123)
        db.add(me)
    with engine.begin() as db:
        db.execute(text('ALTER TABLE users DROP COLUMN profile_public'))
        db.execute(text('ALTER TABLE users DROP COLUMN ranking_public'))
    migrate(engine)
    migrate(engine)
    with Session() as db:
        me = db.scalar(select(User).where(User.username == 'legacy'))
        assert me.cash == 123 and me.password_hash == 'preserve'
        assert me.profile_public is False and me.ranking_public is False
