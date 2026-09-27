"""Members agree at sign-up to be shown on the ranking and to other members; accounts
from before that agreement stay hidden on every read until they accept it."""
from datetime import datetime, timezone
from sqlalchemy import select, func, text
from test_service import database, client, register, agreement
from app import main
from app.accounts import PRIVACY_NOTICE_VERSION
from app.db import Session, User, PrivacyChoice, UserProfile, UserProfileImage, WeeklyReport, engine
from app.migrations import migrate

PASSWORD = 'a-secure-password-123'


def legacy_member(name='legacy'):
    """An account created before the sign-up agreement existed."""
    with Session.begin() as db:
        db.add(User(username=name, password_hash=main.hasher.hash(PASSWORD)))


def login(client, name):
    token = client.get('/api/session').json()['csrf']
    assert client.post('/api/login', headers={'x-csrf-token': token}, json={'username': name, 'password': PASSWORD}).status_code == 200
    return client.get('/api/session').json()['csrf']


def consent(client, token, version=PRIVACY_NOTICE_VERSION):
    return client.post('/api/consent', headers={'x-csrf-token': token}, json={'notice_version': version})


def ranked(client):
    return [row['username'] for row in client.get('/api/ranking').json()['rows']]


def test_signup_requires_both_agreements_and_the_current_notice(client):
    session = client.get('/api/session').json()
    assert session['notice_version'] == PRIVACY_NOTICE_VERSION
    headers = {'x-csrf-token': session['csrf']}
    base = {'username': 'joiner', 'password': PASSWORD, 'password_confirm': PASSWORD}
    for changes in ({'agree_terms': False}, {'over_14': False}, {'agree_terms': 'true'}, {'over_14': 1}):
        assert client.post('/api/register', headers=headers, json=base | agreement(session) | changes).status_code == 422, changes
    for missing in ('agree_terms', 'over_14', 'notice_version'):
        body = {k: v for k, v in (base | agreement(session)).items() if k != missing}
        assert client.post('/api/register', headers=headers, json=body).status_code == 422, missing
    stale = client.post('/api/register', headers=headers, json=base | agreement(session) | {'notice_version': 'old'})
    assert stale.status_code == 409
    with Session() as db:
        assert not db.scalar(select(func.count()).select_from(User))
    assert client.post('/api/register', headers=headers, json=base | agreement(session)).status_code == 200
    with Session() as db:
        user = db.scalar(select(User).where(User.username == 'joiner'))
        assert user.profile_public and user.ranking_public
        record, = db.scalars(select(PrivacyChoice).where(PrivacyChoice.user_id == user.id))
        assert (record.profile_public, record.ranking_public, record.notice_version) == (True, True, PRIVACY_NOTICE_VERSION)
        assert record.created_at


def test_new_member_is_ranked_and_visible_to_other_members(client):
    register(client, 'owner')
    assert client.get('/api/profile').json()['consent_required'] is False
    register(client, 'viewer')
    assert client.get('/api/portfolios/owner').status_code == 200
    assert client.get('/api/performance/owner').status_code == 200
    assert 'owner' in ranked(client)


def test_pre_agreement_account_is_hidden_until_it_accepts(client):
    legacy_member()
    with Session.begin() as db:
        owner = db.scalar(select(User).where(User.username == 'legacy'))
        db.add(UserProfile(user_id=owner.id, bio='before', image_version=1,
                           created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc)))
        db.add(UserProfileImage(user_id=owner.id, content_type='image/webp', data=b'image', updated_at=datetime.now(timezone.utc)))
    now = datetime.now(timezone.utc)
    with Session.begin() as db:
        db.add(WeeklyReport(scheduled_for=now, period_start=now, period_end=now,
                            rows=[{'username': 'legacy', 'return_pct': '1', 'rank': 1}],
                            notes={'excluded_new_or_zero': 0, 'late': False, 'quotes': {}}))
    register(client, 'viewer')
    for path in ('portfolios/legacy', 'performance/legacy', 'users/legacy/avatar'):
        assert client.get('/api/' + path).status_code == 404, path
    assert 'legacy' not in ranked(client)
    assert not client.get('/api/weekly').json()['reports'][0]['rows']
    # A stale cache from another process must also filter using current DB state.
    main._ranking_cache[main.market] = {'bucket': main._ranking_bucket(), 'payload': main._ranking_payload(
        [{'username': 'legacy', 'image_version': 99}], [], False, now.isoformat(), False)}
    assert 'legacy' not in ranked(client)

    token = login(client, 'legacy')
    assert client.get('/api/profile').json()['consent_required'] is True
    assert client.get('/api/users/legacy/avatar').status_code == 200  # own photo stays available
    assert client.get('/api/portfolio').status_code == 200            # own account keeps working
    assert consent(client, token).json() == {'consent_required': False, 'privacy_notice_version': PRIVACY_NOTICE_VERSION}
    assert consent(client, token).status_code == 200                   # repeating it records nothing new
    with Session() as db:
        legacy_id = db.scalar(select(User.id).where(User.username == 'legacy'))
        assert db.scalar(select(func.count()).select_from(PrivacyChoice).where(PrivacyChoice.user_id == legacy_id)) == 1
    assert client.get('/api/profile').json()['consent_required'] is False
    login(client, 'viewer')
    assert client.get('/api/portfolios/legacy').status_code == 200
    assert client.get('/api/users/legacy/avatar').status_code == 200
    assert 'legacy' in ranked(client)
    assert client.get('/api/weekly').json()['reports'][0]['rows']


def test_consent_requires_auth_csrf_and_current_notice(client):
    token = client.get('/api/session').json()['csrf']
    assert consent(client, token).status_code == 401
    legacy_member()
    token = login(client, 'legacy')
    assert consent(client, 'wrong').status_code == 403
    assert consent(client, token, version='old').status_code == 409
    assert client.post('/api/consent', headers={'x-csrf-token': token}, json={}).status_code == 422
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(PrivacyChoice)) == 0
        assert not db.scalar(select(User.ranking_public).where(User.username == 'legacy'))


def test_admin_is_never_asked_and_cannot_join(client):
    legacy_member('operator')
    with Session.begin() as db:
        db.scalar(select(User).where(User.username == 'operator')).is_admin = True
    token = login(client, 'operator')
    assert client.get('/api/profile').json()['consent_required'] is False
    assert consent(client, token).status_code == 403
    assert 'operator' not in ranked(client)


def test_withdrawal_deletes_the_agreement_record(client):
    token = register(client, 'leaver')
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(PrivacyChoice)) == 1
    assert client.post('/api/account/delete', headers={'x-csrf-token': token}, json={'password': PASSWORD}).status_code == 200
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(PrivacyChoice)) == 0


def test_policy_pages_are_public(client):
    for path, title in (('/privacy', '개인정보 처리방침'), ('/terms', '이용약관')):
        response = client.get(path)
        assert response.status_code == 200 and title in response.text
        assert '{{BRAND_NAME}}' not in response.text
        assert '7829hw@gmail.com' in response.text and 'jack3618@knu.ac.kr' in response.text
    page = client.get('/').text
    assert 'href="/privacy"' in page and 'href="/terms"' in page


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
