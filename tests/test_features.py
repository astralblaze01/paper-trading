import io
import time
from decimal import Decimal as D
from uuid import uuid4
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from test_service import database, client, register, FakeMarket, order
from app import main
from app.db import Session, User, Wallet, WalletTransfer, Position, UserAdminNote, UserProfile, UserProfileImage
from app.trading import execute_order
from app.portfolio import portfolio


def admin_and_user(client, name='investor'):
    token = register(client, 'operator')
    with Session.begin() as db:
        db.scalar(select(User).where(User.username == 'operator')).is_admin = True
        u = User(username=name, password_hash='unused'); db.add(u); db.flush(); uid = u.id
    return {'x-csrf-token': token}, uid


def second_client(name):
    c = TestClient(main.app, base_url='https://testserver')
    return c, {'x-csrf-token': register(c, name)}


def balances(uid):
    with Session() as db:
        return {w.currency: w.balance for w in db.scalars(select(Wallet).where(Wallet.user_id == uid))}


def uid_of(name):
    with Session() as db: return db.scalar(select(User.id).where(User.username == name))


def image_bytes(fmt, size=(40, 30)):
    from PIL import Image
    out = io.BytesIO(); Image.new('RGB', size, (200, 40, 40)).save(out, fmt); return out.getvalue()


# 관리자 -------------------------------------------------------------------

def test_admin_notes_are_searchable_and_admin_only(client):
    headers, uid = admin_and_user(client, 'abc123')
    assert client.post(f'/api/admin/users/{uid}/note', headers=headers, json={'note': '홍길동 / 컴공 동기'}).status_code == 200
    assert [r['username'] for r in client.get('/api/admin/users/search?q=홍길동').json()] == ['abc123']
    assert [r['note'] for r in client.get('/api/admin/users/search?q=ABC').json()] == ['홍길동 / 컴공 동기']
    assert client.get('/api/admin/users/search?q=%25').json() == []  # wildcards are literal
    assert client.post(f'/api/admin/users/{uid}/note', headers=headers, json={'note': '수정된 메모'}).status_code == 200
    assert next(u for u in client.get('/api/admin').json()['users'] if u['id'] == uid)['note'] == '수정된 메모'
    other, other_headers = second_client('plain')
    assert other.get('/api/admin/users/search?q=홍').status_code == 403
    assert other.post(f'/api/admin/users/{uid}/note', headers=other_headers, json={'note': 'x'}).status_code == 403
    for action in ('grant', 'rebase', 'clear', 'delete'):
        body = {'action': action, 'amount': '10', 'request_id': str(uuid4())}
        assert other.post(f'/api/admin/users/{uid}/manage', headers=other_headers, json=body).status_code == 403
    assert other.post('/api/admin/users/manage-all', headers=other_headers, json={'action': 'grant', 'amount': '10', 'request_id': str(uuid4())}).status_code == 403
    # The memo never reaches public or ranking responses.
    execute_order(uid, order(), FakeMarket())
    assert '수정된 메모' not in other.get('/api/portfolios/abc123').text + other.get('/api/ranking').text
    other.close()
    assert client.post(f'/api/admin/users/{uid}/note', headers=headers, json={'note': ''}).status_code == 200
    with Session() as db: assert db.get(UserAdminNote, uid) is None


def test_admin_actions_run_without_confirmation_phrase(client):
    headers, uid = admin_and_user(client)
    def manage(action, **extra):
        return client.post(f'/api/admin/users/{uid}/manage', headers=headers, json={'action': action, 'request_id': str(uuid4())} | extra)
    assert manage('grant', currency='KRW', amount='5000').status_code == 200
    assert balances(uid)['KRW'] == 5000
    execute_order(uid, order(quantity=3), FakeMarket())
    # Rebase values at the last provider price, so a closed market (stale quote) works.
    stale = FakeMarket(); stale.quote = lambda s: {'symbol': s, 'price': D(120), 'timestamp': int(time.time()) - 86400, 'stale': True}
    main.market = stale
    assert manage('rebase').status_code == 200
    after = portfolio(uid, stale, main.fx)
    assert after['return_pct'] == 0 and after['positions'][0]['quantity'] == 3
    with Session() as db: assert db.get(User, uid).baseline_note == 'admin-rebase'
    main.market = FakeMarket()
    assert manage('clear').status_code == 200
    with Session() as db:
        assert not db.scalar(select(func.count()).select_from(Position).where(Position.user_id == uid))
    assert balances(uid) == {'USD': 100000, 'KRW': 0}
    assert manage('delete').status_code == 200
    with Session() as db: assert db.get(User, uid) is None
    audit = client.get('/api/admin/audit').json()
    assert audit[0]['reason'] == '계정 영구 삭제'


def test_account_delete_keeps_counterparty_history(client):
    headers, uid = admin_and_user(client)
    other, other_headers = second_client('friend')
    friend = uid_of('friend')
    with Session.begin() as db:
        db.add(WalletTransfer(sender_id=uid, recipient_id=friend, request_id=str(uuid4()), currency='USD', amount=10, fee=0, fee_bps=0, fx_rate=1000, rate_date='2026-09-24', created_at=main.datetime.now(main.timezone.utc)))
        db.add(UserAdminNote(user_id=uid, note='memo', created_at=main.datetime.now(main.timezone.utc), updated_at=main.datetime.now(main.timezone.utc)))
    friend_before = balances(friend)
    assert client.post(f'/api/admin/users/{uid}/manage', headers=headers, json={'action': 'delete', 'request_id': str(uuid4())}).status_code == 200
    with Session() as db:
        kept = db.scalar(select(WalletTransfer))
        assert kept.sender_id is None and kept.recipient_id == friend  # detached, not deleted
    assert balances(friend) == friend_before
    with Session() as db: assert db.get(UserAdminNote, uid) is None
    other.close()


# 주문 · 휴장 ---------------------------------------------------------------

class Status:
    def __init__(self, label): self.label = label
    def market_status(self): return {'label': self.label, 'timezone': 'X', 'verified': True}


@pytest.mark.parametrize('market_code,label,symbol,blocked,text', [
    ('US', '휴장', 'AAPL', True, '미국 주식시장 휴장일'),
    ('US', '장마감', 'AAPL', True, '현재 미국 주식시장이 휴장 중'),
    ('KR', '장후', 'KR:005930', True, '현재 한국 주식시장이 휴장 중'),
    ('KR', '휴장', 'KR:005930', True, '한국 주식시장 휴장일'),
    ('US', '프리장', 'AAPL', False, None),
    ('US', '정규장', 'AAPL', False, None),
    ('US', '장 상태 확인 불가', 'AAPL', False, None),
])
def test_closed_market_orders_explain_why(client, market_code, label, symbol, blocked, text):
    headers = {'x-csrf-token': register(client)}
    main.market.providers = {'KR': Status('정규장'), 'US': Status('정규장')} | {market_code: Status(label)}
    body = {'symbol': symbol, 'side': 'buy', 'quantity': 1, 'request_id': str(uuid4())}
    r = client.post('/api/orders', headers=headers, json=body)
    if blocked:
        assert r.status_code == 409 and text in r.json()['detail'] and '장 운영 시간' in r.json()['detail']
        assert client.get(f'/api/order-preview?symbol={symbol}').json()['market_closed']
    else:
        assert r.status_code != 409 or '휴장' not in r.json()['detail']
        assert client.get('/api/order-preview?symbol=AAPL').json()['market_closed'] is None


# 랭킹 -------------------------------------------------------------------

def test_ranking_orders_by_usd_equity_not_return(client):
    headers, _ = admin_and_user(client, 'grower')
    for name in ('grower', 'granted'):
        with Session.begin() as db:
            if not db.scalar(select(User).where(User.username == name)): db.add(User(username=name, password_hash='x'))
    grower, granted = uid_of('grower'), uid_of('granted')
    portfolio(grower, FakeMarket(), main.fx); portfolio(granted, FakeMarket(), main.fx)
    # KRW held outside contributions raises grower's return (+10%) but not above granted's value.
    with Session.begin() as db: db.get(Wallet, (grower, 'KRW')).balance = D(10000000)
    assert client.post(f'/api/admin/users/{granted}/manage', headers=headers, json={'action': 'grant', 'amount': '50000', 'request_id': str(uuid4())}).status_code == 200
    main._ranking_cache.clear()
    rows = client.get('/api/ranking').json()['rows']
    assert [(r['username'], r['equity_usd'], round(float(r['return_pct']), 4)) for r in rows] == [('granted', 150000, 0), ('grower', 110000, 10)]
    assert [r['rank'] for r in rows] == [1, 2]


# 프로필 · 이미지 ----------------------------------------------------------

def test_profile_bio_and_image_lifecycle(client):
    headers = {'x-csrf-token': register(client, 'painter')}
    profile = client.get('/api/profile').json()
    assert {k: profile[k] for k in ('username', 'bio', 'image_version', 'bio_max_length')} == {'username': 'painter', 'bio': '', 'image_version': 0, 'bio_max_length': 160}
    assert profile['member_days'] == 1 and profile['member_since']  # sign-up day counts as day 1
    assert client.post('/api/profile', headers=headers, json={'bio': '  장기 투자 위주로 하고 있습니다.\x07 '}).json()['bio'] == '장기 투자 위주로 하고 있습니다.'
    assert client.post('/api/profile', headers=headers, json={'bio': 'x' * 161}).status_code == 422
    for fmt, mime in (('PNG', 'image/png'), ('JPEG', 'image/jpeg'), ('WEBP', 'image/webp')):
        r = client.post('/api/profile/image', headers=headers | {'content-type': mime}, content=image_bytes(fmt))
        assert r.status_code == 200, r.text
    assert client.get('/api/profile').json()['image_version'] == 3
    served = client.get('/api/users/painter/avatar')
    assert served.status_code == 200 and served.headers['content-type'] == 'image/webp' and served.content[:4] == b'RIFF'
    assert 'max-age' in served.headers['cache-control']
    # Oversized images are resized on the server.
    client.post('/api/profile/image', headers=headers | {'content-type': 'image/png'}, content=image_bytes('PNG', (2000, 1000)))
    from PIL import Image
    assert max(Image.open(io.BytesIO(client.get('/api/users/painter/avatar').content)).size) == 512
    assert client.post('/api/profile/image/delete', headers=headers).json()['image_version'] == 0
    assert client.get('/api/users/painter/avatar').status_code == 404
    # Profile survives a new session (reload / restart reads the database).
    fresh = TestClient(main.app, base_url='https://testserver')
    token = fresh.get('/api/session').json()['csrf']
    fresh.post('/api/login', headers={'x-csrf-token': token}, json={'username': 'painter', 'password': 'a-secure-password-123'})
    assert fresh.get('/api/profile').json()['bio'] == '장기 투자 위주로 하고 있습니다.'
    fresh.close()


@pytest.mark.parametrize('mime,body,status', [
    ('image/png', b'not an image at all', 415),
    ('image/jpeg', None, 415),  # PNG bytes declared as JPEG
    ('text/plain', b'hello', 415),
    ('image/svg+xml', b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', 415),
    ('application/x-msdownload', b'MZ\x90\x00', 415),
    ('image/png', b'', 422),
    ('image/png', b'\x89PNG' + b'0' * (5 * 1024 * 1024), 413),
])
def test_profile_image_rejections(client, mime, body, status):
    headers = {'x-csrf-token': register(client, 'painter')}
    r = client.post('/api/profile/image', headers=headers | {'content-type': mime}, content=image_bytes('PNG') if body is None else body)
    assert r.status_code == status, r.text
    with Session() as db: assert not db.scalar(select(func.count()).select_from(UserProfileImage))


def test_public_profile_hides_private_fields_and_cannot_be_edited_by_others(client):
    headers = {'x-csrf-token': register(client, 'owner')}
    client.post('/api/profile', headers=headers, json={'bio': '미국 성장주와 ETF 위주'})
    other, other_headers = second_client('visitor')
    public = other.get('/api/portfolios/owner').json()
    assert public['profile'] == {'bio': '미국 성장주와 ETF 위주', 'image_version': 0}
    assert public['equity_usd'] == 100000
    assert public['member_days'] == 1 and public['member_since']  # same rule as the own profile
    text = str(public)
    for secret in ('password', 'argon2', 'user_id', "'id'", 'note', 'csrf'):
        assert secret not in text
    # Profile endpoints act only on the signed-in account; there is no target parameter.
    assert other.post('/api/profile', headers=other_headers, json={'bio': 'hacked', 'username': 'owner'}).status_code == 422
    other.post('/api/profile/image/delete', headers=other_headers)
    assert client.get('/api/profile').json()['bio'] == '미국 성장주와 ETF 위주'
    other.close()


# 회원 탈퇴 -----------------------------------------------------------------

def test_withdrawal_removes_account_and_invalidates_session(client):
    headers = {'x-csrf-token': register(client, 'leaver')}
    leaver = uid_of('leaver')
    other, other_headers = second_client('stayer')
    stayer = uid_of('stayer')
    execute_order(leaver, order(quantity=2), FakeMarket())
    client.post('/api/profile', headers=headers, json={'bio': 'bye'})
    client.post('/api/profile/image', headers=headers | {'content-type': 'image/png'}, content=image_bytes('PNG'))
    with Session.begin() as db:
        db.add(WalletTransfer(sender_id=stayer, recipient_id=leaver, request_id=str(uuid4()), currency='USD', amount=10, fee=0, fee_bps=0, fx_rate=1000, rate_date='2026-09-24', created_at=main.datetime.now(main.timezone.utc)))
    stayer_before = balances(stayer)
    old_cookie = client.cookies.get('paper_session')
    assert client.post('/api/account/delete', headers=headers, json={'password': 'wrong-password'}).status_code == 401
    assert client.post('/api/account/delete', headers=headers, json={'password': 'x', 'username': 'stayer'}).status_code == 422
    assert client.post('/api/account/delete', headers=headers, json={'password': 'a-secure-password-123'}).json() == {'ok': True}
    with Session() as db:
        assert db.get(User, leaver) is None
        for model in (Wallet, Position, UserProfile, UserProfileImage):
            assert not db.scalar(select(func.count()).select_from(model).where(model.user_id == leaver))
    # The old session cookie no longer reaches protected APIs.
    replay = TestClient(main.app, base_url='https://testserver', cookies={'paper_session': old_cookie})
    assert replay.get('/api/portfolio').status_code == 401
    replay.close()
    token = client.get('/api/session').json()['csrf']
    assert client.post('/api/login', headers={'x-csrf-token': token}, json={'username': 'leaver', 'password': 'a-secure-password-123'}).status_code == 401
    # The other account keeps its balance and its transfer record.
    assert balances(stayer) == stayer_before
    with Session() as db:
        kept = db.scalar(select(WalletTransfer))
        assert kept.sender_id == stayer and kept.recipient_id is None
    other.close()


def test_admin_cannot_withdraw_via_user_flow(client):
    headers, _ = admin_and_user(client)
    assert client.post('/api/account/delete', headers=headers, json={'password': 'a-secure-password-123'}).status_code == 409


# 시장 탐색 · 배당 ---------------------------------------------------------

def test_explore_volume_kind_uses_share_volume(client):
    register(client)
    seen = []
    class Provider:
        def movers(self, direction):
            seen.append(direction)
            return {'rows': [{'symbol': 'AAPL', 'name': 'Apple', 'volume': 5}, {'symbol': 'NVDA', 'name': 'NVIDIA', 'volume': 9}], 'scope': '미국 거래량 순위', 'notice': ''}
        def volume_leaders(self): raise AssertionError('turnover ranking must not be used')
    main.market.providers = {'US': Provider(), 'KR': Provider()}
    assert [r['symbol'] for r in client.get('/api/explore?asset=us&kind=shares').json()['rows']] == ['AAPL', 'NVDA']
    assert seen == ['shares']


def test_kr_volume_provider_requests_share_volume_ranking():
    from app.providers import KRProvider
    calls = []
    class Adapter:
        def get(self, path, tr, params, ttl):
            calls.append(params['FID_BLNG_CLS_CODE'])
            return {'output': [{'mksc_shrn_iscd': '005930', 'hts_kor_isnm': 'A', 'stck_prpr': '1', 'prdy_ctrt': '0', 'acml_vol': '10'},
                               {'mksc_shrn_iscd': '000660', 'hts_kor_isnm': 'B', 'stck_prpr': '1', 'prdy_ctrt': '0', 'acml_vol': '30'}]}
    rows = KRProvider(Adapter()).movers('shares')['rows']
    assert calls == ['0'] and [r['symbol'] for r in rows] == ['KR:000660', 'KR:005930']


def test_dividend_yield_statuses():
    from app.company import dividend_info
    class US:
        def __init__(self, metric): self.metric = metric
        def get(self, path, params, ttl): return {'metric': self.metric}
    market = lambda metric: type('M', (), {'us': US(metric)})()
    assert dividend_info('AAPL', market({'dividendYieldIndicatedAnnual': 2.4321})) | {} == {'yield': D('2.43'), 'status': 'paid', 'basis': '연간 배당수익률 · Finnhub'}
    assert dividend_info('TSLA', market({'dividendIndicatedAnnual': 0, 'currentDividendYieldTTM': None}))['status'] == 'none'
    assert dividend_info('SPY', market({}))['status'] == 'unavailable'
    today = time.strftime('%Y%m%d')
    class KR:
        def get(self, *args): return {'output1': [{'record_date': today, 'per_sto_divi_amt': '1000'}, {'record_date': today, 'per_sto_divi_amt': '430'}]}
    kr = type('M', (), {'kr': KR(), 'quote': lambda self, s: {'native_price': D(50000)}})()
    assert dividend_info('KR:005930', kr)['yield'] == D('2.86')
    class Empty:
        def get(self, *args): return {'output1': []}
    assert dividend_info('KR:035720', type('M', (), {'kr': Empty()})())['status'] == 'none'


def test_reads_and_previews_do_not_spend_the_trade_limit(client):
    headers = {'x-csrf-token': register(client, 'trader')}
    for _ in range(35):
        assert client.get('/api/limit-orders').status_code == 200
        assert client.post('/api/fx/preview', headers=headers, json={'source': 'USD', 'amount': '1'}).status_code == 200
    assert client.post('/api/fx/exchange', headers=headers, json={'source': 'USD', 'amount': '1', 'request_id': str(uuid4())}).status_code == 200

def test_fx_share_amounts_use_currency_units(client):
    headers = {'x-csrf-token': register(client, 'trader')}
    with Session.begin() as db: db.get(Wallet, (uid_of('trader'), 'KRW')).balance = D(12345)
    assert client.get('/api/fx/share?source=USD&percent=100').json()['amount'] == 100000
    assert D(str(client.get('/api/fx/share?source=USD&percent=5').json()['amount'])) == 5000
    assert client.get('/api/fx/share?source=KRW&percent=50').json()['amount'] == 6172  # whole won, rounded down
    assert client.get('/api/fx/share?source=KRW&percent=30').status_code == 422
    assert client.get('/api/fx/share?source=EUR&percent=50').status_code == 422
    # The full-balance amount is exchangeable as is (the fee is taken from it).
    body = {'source': 'KRW', 'amount': str(client.get('/api/fx/share?source=KRW&percent=100').json()['amount']), 'request_id': str(uuid4())}
    assert client.post('/api/fx/exchange', headers=headers, json=body).status_code == 200
    assert balances(uid_of('trader'))['KRW'] == 0


def test_maintenance_shortcut_posts_the_template_and_blocks_nothing(client):
    headers, uid = admin_and_user(client)
    assert client.get('/api/notice').json() == {'notice': None, 'maintenance': False}
    other, other_headers = second_client('plain')
    assert other.post('/api/admin/maintenance', headers=other_headers, json={'enabled': True}).status_code == 403
    assert client.post('/api/admin/maintenance', headers=headers, json={'enabled': True}).json() == {'maintenance': True}
    anonymous = TestClient(main.app, base_url='https://testserver')
    shown = anonymous.get('/api/notice').json()
    assert shown['maintenance'] is True and shown['notice']['title'] == '서버 점검 예정' and shown['notice']['label'] == '서버 점검 예고'
    anonymous.close()
    assert client.get('/api/admin').json()['maintenance'] is True
    # Only a notice: users keep trading while it is shown.
    assert other.get('/api/portfolio').status_code == 200
    assert other.post('/api/orders', headers=other_headers, json={'symbol': 'AAPL', 'side': 'buy', 'quantity': 1, 'request_id': str(uuid4())}).status_code == 200
    assert client.post('/api/admin/maintenance', headers=headers, json={'enabled': False}).json() == {'maintenance': False}
    assert other.get('/api/notice').json() == {'notice': None, 'maintenance': False}
    assert [r['action'] for r in client.get('/api/admin/audit').json()][:2] == ['notice_clear', 'notice_post']
    other.close()


def test_notices_from_templates_or_custom_text(client):
    headers, uid = admin_and_user(client)
    templates = client.get('/api/admin').json()['notice_templates']
    assert set(templates) == {'maintenance', 'update', 'general'} and templates['general']['body'] == ''
    other, other_headers = second_client('plain')
    body = {'kind': 'general', 'title': ' 이벤트 안내 ', 'body': '이번 주 수익률 1위에게\n가상 지원금을 드립니다.\x07'}
    assert other.post('/api/admin/notice', headers=other_headers, json=body).status_code == 403
    posted = client.post('/api/admin/notice', headers=headers, json=body).json()['notice']
    assert posted['title'] == '이벤트 안내' and posted['body'] == '이번 주 수익률 1위에게\n가상 지원금을 드립니다.' and posted['label'] == '일반 공지'
    assert other.get('/api/notice').json()['notice']['title'] == '이벤트 안내'
    assert other.get('/api/notice').json()['maintenance'] is False
    # Registering replaces the current notice; only one is active.
    client.post('/api/admin/notice', headers=headers, json={'kind': 'maintenance', 'title': templates['maintenance']['title'], 'body': templates['maintenance']['body']})
    assert other.get('/api/notice').json()['maintenance'] is True
    from app.db import SiteNotice
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(SiteNotice).where(SiteNotice.active.is_(True))) == 1
        assert db.scalar(select(func.count()).select_from(SiteNotice)) == 2
    # Markup is stored as text; the page renders it with textContent.
    client.post('/api/admin/notice', headers=headers, json={'kind': 'general', 'title': '<b>x</b>', 'body': '<script>alert(1)</script>'})
    assert other.get('/api/notice').json()['notice']['body'] == '<script>alert(1)</script>'
    for bad in ({'kind': 'general', 'title': '', 'body': 'x'}, {'kind': 'general', 'title': 'x', 'body': '   '},
                {'kind': 'general', 'title': 'x' * 61, 'body': 'x'}, {'kind': 'general', 'title': 'x', 'body': 'x' * 501},
                {'kind': 'event', 'title': 'x', 'body': 'x'}):
        assert client.post('/api/admin/notice', headers=headers, json=bad).status_code == 422
    assert other.post('/api/admin/notice/clear', headers=other_headers).status_code == 403
    assert client.post('/api/admin/notice/clear', headers=headers).json() == {'cleared': True, 'notice': None}
    assert client.post('/api/admin/notice/clear', headers=headers).json()['cleared'] is False
    assert other.get('/api/notice').json() == {'notice': None, 'maintenance': False}
    other.close()


# 한글 종목명 검색 ---------------------------------------------------------

US_MASTER_SAMPLE = '\n'.join('\t'.join(fields) for fields in [
    ['US', '22', 'NAS', '나스닥', 'TSLA', 'NASTSLA', '테슬라', 'TESLA INC', '2', 'USD', '4', ''],
    ['US', '22', 'NAS', '나스닥', 'TSLL', 'NASTSLL', '디렉시온 테슬라 2배 ETF', 'DIREXION DAILY TSLA BULL 2X', '3', 'USD', '4', ''],
    ['US', '22', 'NAS', '나스닥', 'RETO', 'NASRETO', '리토 에코 솔루션스', 'RETO ECO SOLUTIONS INC', '2', 'USD', '4', ''],
    ['US', '21', 'NYS', '뉴욕', 'BRK/B', 'NYSBRK/B', '버크셔 해서웨이 B', 'BERKSHIRE HATHAWAY INC', '2', 'USD', '4', ''],
    ['US', '22', 'NAS', '나스닥', 'PLTR', 'NASPLTR', '팔란티어 테크', 'PALANTIR TECH INC', '2', 'USD', '4', ''],
]).encode('cp949')


def test_us_master_parses_korean_names_and_skips_invalid_symbols():
    from app.us_symbols import parse_master
    rows = parse_master(US_MASTER_SAMPLE, 'NAS')
    assert [r['symbol'] for r in rows] == ['TSLA', 'TSLL', 'RETO', 'PLTR']
    assert rows[0] == {'symbol': 'TSLA', 'name': '테슬라', 'english': 'TESLA INC', 'exchange': 'NAS', 'etf': False}
    assert rows[1]['etf'] is True


def us_market(monkeypatch):
    from app.multi_market import MultiMarket
    from app.us_symbols import parse_master
    monkeypatch.setattr('app.us_symbols._rows', lambda: parse_master(US_MASTER_SAMPLE, 'NAS'))
    market = MultiMarket(); market.us.key = 'test'
    finnhub = []
    def search(query):
        finnhub.append(query)
        return [{'symbol': 'TSLA', 'name': 'TESLA INC'}] if query.lower() in ('tesla', 'tsla') else []
    monkeypatch.setattr(market.us, 'search', search)
    return market, finnhub


def test_korean_names_find_us_listings_without_finnhub(monkeypatch):
    market, finnhub = us_market(monkeypatch)
    rows = market.search('테슬라', 'us')
    assert [r['symbol'] for r in rows] == ['TSLA', 'TSLL']  # exact name first, ETF after
    assert rows[0] | {} == {'symbol': 'TSLA', 'name': '테슬라', 'category': 'us', 'currency': 'USD'}
    assert [r['symbol'] for r in market.search('리토 에코', 'us')] == ['RETO']
    assert [r['symbol'] for r in market.search('리토에코솔루션스', 'us')] == ['RETO']  # spaces ignored
    assert [r['symbol'] for r in market.search('팔란티어', 'all')] == ['PLTR']
    # Existing aliases still come first and are not duplicated.
    assert [r['symbol'] for r in market.search('애플', 'us')] == ['AAPL']
    assert finnhub == []  # Korean queries never reach Finnhub, which cannot match them
    market.close()


def test_english_code_and_korean_market_searches_unchanged(monkeypatch):
    market, finnhub = us_market(monkeypatch)
    assert [r['symbol'] for r in market.search('Tesla', 'us')] == ['TSLA']
    assert market.search('Tesla', 'us')[0]['name'] == 'TESLA INC'
    assert [r['symbol'] for r in market.search('TSLA', 'us')] == ['TSLA']
    assert finnhub == ['Tesla', 'Tesla', 'TSLA']
    # Korean-market search does not add US listings.
    assert all(r['symbol'].startswith('KR:') for r in market.search('테슬라', 'kr'))
    assert [r['symbol'] for r in market.search('삼성', 'kr')] == ['KR:005930']
    market.close()



def test_membership_days_count_signup_day_as_one_in_korea_time():
    from datetime import datetime, timezone
    from app.accounts import membership_days
    utc = lambda *a: datetime(*a, tzinfo=timezone.utc)
    now = utc(2026, 9, 25, 1, 0)                            # 10:00 on 9/25 in Seoul
    assert membership_days(utc(2026, 9, 25, 0, 30), now) == 1  # same Seoul day
    assert membership_days(utc(2026, 9, 24, 14, 59), now) == 2  # 23:59 on 9/24 Seoul
    assert membership_days(utc(2026, 9, 24, 15, 0), now) == 1   # 00:00 on 9/25 Seoul
    assert membership_days(utc(2026, 9, 1, 3, 0), now) == 25
    assert membership_days(utc(2026, 9, 26, 0, 0), now) == 1    # clock skew never shows 0
    assert membership_days(None, now) is None
