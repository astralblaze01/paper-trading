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


# 이체 ---------------------------------------------------------------------

def test_transfer_usd_and_krw_update_both_sides_and_history(client):
    sender_headers = {'x-csrf-token': register(client, 'sender')}
    other, _ = second_client('receiver')
    sender, receiver = uid_of('sender'), uid_of('receiver')
    with Session.begin() as db: db.get(Wallet, (sender, 'KRW')).balance = D(500000)
    for currency, amount in (('USD', '100'), ('KRW', '50000')):
        body = {'recipient': 'receiver', 'currency': currency, 'amount': amount}
        preview = client.post('/api/transfers/preview', headers=sender_headers, json=body).json()
        assert D(str(preview['total'])) == D(amount) * D('1.001')
        assert client.post('/api/transfers', headers=sender_headers, json=body | {'request_id': str(uuid4())}).status_code == 200
    assert balances(sender) == {'USD': D('99899.9'), 'KRW': D('449950')}
    assert balances(receiver) == {'USD': D('100100'), 'KRW': D('50000')}
    # A fresh session (page reload) reads the same persisted balances.
    assert client.get('/api/wallets').json() == {'USD': 99899.9, 'KRW': 449950}
    assert [r['direction'] for r in other.get('/api/transfers').json()] == ['received', 'received']
    other.close()


@pytest.mark.parametrize('body,status,text', [
    ({'recipient': 'nobody', 'currency': 'USD', 'amount': '1'}, 404, '찾을 수 없습니다'),
    ({'recipient': 'sender', 'currency': 'USD', 'amount': '1'}, 422, '본인'),
    ({'recipient': 'receiver', 'currency': 'USD', 'amount': '0'}, 422, None),
    ({'recipient': 'receiver', 'currency': 'USD', 'amount': '-5'}, 422, None),
    ({'recipient': 'receiver', 'currency': 'EUR', 'amount': '1'}, 422, None),
    ({'recipient': 'receiver', 'currency': 'USD', 'amount': '100000'}, 409, '잔액이 부족'),
    ({'recipient': 'receiver', 'currency': 'KRW', 'amount': '1'}, 409, '잔액이 부족'),
])
def test_invalid_transfers_change_nothing(client, body, status, text):
    headers = {'x-csrf-token': register(client, 'sender')}
    other, _ = second_client('receiver'); other.close()
    before = balances(uid_of('sender')), balances(uid_of('receiver'))
    r = client.post('/api/transfers', headers=headers, json=body | {'request_id': str(uuid4())})
    assert r.status_code == status, r.text
    if text: assert text in r.json()['detail']
    assert (balances(uid_of('sender')), balances(uid_of('receiver'))) == before
    with Session() as db: assert not db.scalar(select(func.count()).select_from(WalletTransfer))


def test_transfer_storage_failure_rolls_back_both_wallets(client, monkeypatch):
    headers = {'x-csrf-token': register(client, 'sender')}
    other, _ = second_client('receiver'); other.close()
    before = balances(uid_of('sender')), balances(uid_of('receiver'))
    def failing(**kwargs): raise RuntimeError('simulated storage failure')
    # Fails after both balances were changed inside the transaction.
    monkeypatch.setattr('app.transfers.WalletTransfer', failing)
    from app.transfers import TransferOrder, transfer
    with pytest.raises(RuntimeError):
        transfer(uid_of('sender'), TransferOrder(recipient='receiver', currency='USD', amount=D(100), request_id=uuid4()), main.fx)
    assert (balances(uid_of('sender')), balances(uid_of('receiver'))) == before


def test_transfer_share_amounts_fit_balance_including_fee(client):
    register(client, 'sender')
    for percent, expected in ((100, D('99900.0999')), (50, D('49950.0499')), (5, D('4995.0049'))):
        r = client.get(f'/api/transfers/share?currency=USD&percent={percent}').json()
        amount = D(str(r['amount']))
        assert amount == expected
        fee = (amount * D('0.001')).quantize(D('.0001'), rounding='ROUND_CEILING')
        assert amount + fee <= D(100000) * percent / 100
    assert client.get('/api/transfers/share?currency=KRW&percent=100').json()['amount'] == 0
    assert client.get('/api/transfers/share?currency=USD&percent=30').status_code == 422


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
    history = other.get('/api/transfers').json()
    assert history[0]['counterparty'] == '탈퇴한 사용자' and history[0]['direction'] == 'received'
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
    assert client.get('/api/profile').json() | {} == {'username': 'painter', 'bio': '', 'image_version': 0, 'bio_max_length': 160}
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
    assert other.post('/api/transfers', headers=other_headers, json={'recipient': 'leaver', 'currency': 'USD', 'amount': '10', 'request_id': str(uuid4())}).status_code == 200
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
    assert other.get('/api/transfers').json()[0]['counterparty'] == '탈퇴한 사용자'
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
