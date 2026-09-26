import os
import re
import secrets
import logging
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from threading import RLock
from typing import Literal
from uuid import UUID
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException, Depends, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel, Field, ConfigDict, field_validator
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from .db import Base, engine, Session, User, Transaction, LimitOrder
from .market import MarketError
from .multi_market import MultiMarket
from .instruments import SYMBOL_PATTERN, valid_symbol, CATEGORIES, market_of
from .migrations import migrate
from .trading import execute_order, filled_replay
from .money import MAX_ORDER_QUANTITY, wallets, initial_amount
from .fx import FxService
from .portfolio import portfolio as wallet_portfolio, initialize_equity, RETURN_BASIS
from .weekly import report_list, tick
from .limits import process as process_limit_orders
from .accounts import membership_days, profile_of, profile_versions
from .performance_snapshots import capture_daily_snapshots, period_range, series, SEOUL as SNAPSHOT_ZONE
from .routes import event
from .branding import BRAND_NAME, STORAGE_NAMESPACE
from .redis_cache import redis_cache
from .market_stream import QuoteHub, enabled as quote_sse_enabled
from .quote_policy import max_age as quote_max_age
from .kr_session import SEOUL
from .logging_config import configure_logging
from .security import limiter, worker_token, WORKER_TOKEN_HEADER, SESSION_COOKIE, SESSION_MAX_AGE

configure_logging()
request_log = logging.getLogger('request')

secret = os.environ['SESSION_SECRET']
if len(secret) < 32: raise RuntimeError('SESSION_SECRET must have at least 32 characters')
market = MultiMarket()
fx = FxService(market.fx)
hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
dummy_hash = hasher.hash(secrets.token_urlsafe(32))
RANKING_INTERVAL_SECONDS = 10
RANKING_INTERVAL = timedelta(seconds=RANKING_INTERVAL_SECONDS)
_ranking_lock = RLock()
# Ranking is deliberately process-local. The response is a view of the
# database, and the ten-second boundary keeps browser polling from turning
# into an external-provider call for every request.
_ranking_cache = {}

# Sessions this service can model for new market orders. Being listed is not
# enough to fill: the market must be open and tradable, and the quote must be
# a verified, fresh print of the current session (trading.validate_quote).
SUPPORTED_ORDER_SESSIONS = {'KR': {'pre_market', 'regular', 'after_hours'},
                            'US': {'overnight', 'pre_market', 'regular', 'after_hours'}}
# Test doubles and legacy providers report only a label.
LEGACY_LABELS = {'정규장': 'regular', '장전': 'pre_market', '장후': 'after_hours', '프리장': 'pre_market',
                 '애프터장': 'after_hours', '데이마켓': 'overnight', '장마감': 'closed', '휴장': 'closed',
                 '장 상태 확인 불가': 'unknown'}
# The LEGACY_LABELS of trading sessions, for statuses without an 'open' flag.
OPEN_LABELS = {'정규장', '장전', '장후', '데이마켓', '프리장', '애프터장'}


def _ranking_bucket(now=None):
    local = (now or datetime.now(timezone.utc)).astimezone(SEOUL)
    return local.replace(second=(local.second // RANKING_INTERVAL_SECONDS) * RANKING_INTERVAL_SECONDS, microsecond=0)


def _next_ranking_boundary(now=None):
    return (_ranking_bucket(now) + RANKING_INTERVAL).astimezone(timezone.utc)


def _ranking_market_state():
    """Return market state without making a closed market look live."""
    providers = getattr(market, 'providers', {}) or {}
    statuses = []
    for code in ('KR', 'US'):
        provider = providers.get(code)
        if provider is None or not hasattr(provider, 'market_status'):
            continue
        try:
            status = dict(provider.market_status() or {})
        except Exception:
            status = {'label': '장 상태 확인 불가', 'verified': False}
        status['market'] = code
        statuses.append(status)
    # Test doubles and legacy adapters without market_status should continue
    # to calculate rankings; production providers always expose the status.
    if not statuses:
        return {'open': None, 'unknown': True, 'labels': []}
    unknown = any(s.get('session', LEGACY_LABELS.get(s.get('label'))) == 'unknown' for s in statuses)
    is_open = any(s['open'] if 'open' in s else s.get('label') in OPEN_LABELS for s in statuses)
    return {'open': None if unknown and not is_open else is_open,
            'unknown': unknown, 'labels': statuses}

@asynccontextmanager
async def lifespan(app):
    Base.metadata.create_all(engine)
    migrate(engine)
    # Scheduled work is driven by the separate worker container (/internal/jobs).
    app.state.quote_hub = QuoteHub(assess=getattr(market, 'assess', None))
    if quote_sse_enabled(): await app.state.quote_hub.start()
    try:
        yield
    finally:
        await app.state.quote_hub.stop()
        market.client.close()

# The docs UIs are always off; the schema document is only served when a
# development setup asks for it, so production does not list every route.
openapi_url = '/openapi.json' if os.getenv('OPENAPI_ENABLED', 'false').lower() == 'true' else None
app = FastAPI(title=BRAND_NAME, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=openapi_url)
app.add_middleware(SessionMiddleware, secret_key=secret, session_cookie=SESSION_COOKIE, max_age=SESSION_MAX_AGE, same_site='strict', https_only=os.getenv('COOKIE_SECURE', 'false').lower() == 'true')
app.mount('/static', StaticFiles(directory='app/static'), name='static')

# Requests per client and category within the limiter's 60-second window.
RATE_LIMITS = {'auth': 20, 'trade': 30, 'market': 120, 'event': 60, 'profile': 30}

def _request_id(request):
    supplied = request.headers.get('x-request-id', '')
    return supplied if re.fullmatch(r'[A-Za-z0-9._-]{1,80}', supplied) else secrets.token_hex(12)

def _rate_limit_category(method, path):
    if path in ('/api/login', '/api/register', '/api/account/delete'): return 'auth'
    if path.startswith('/api/profile'): return 'profile'
    # Only executions spend the 'trade' budget; previews and reads (the
    # portfolio refresh lists limit orders) must not starve real orders.
    if method == 'POST' and path in ('/api/orders', '/api/fx/exchange', '/api/limit-orders'): return 'trade'
    if path.startswith(('/api/search', '/api/quote', '/api/candles', '/api/explore', '/api/order-preview', '/api/fx/preview',
                        '/api/fx/share', '/api/market-status', '/api/company', '/api/portfolios', '/api/performance')):
        return 'market'
    if path == '/api/popularity': return 'event'
    return None

@app.middleware('http')
async def request_context(request, call_next):
    path=request.url.path
    started=time.monotonic()
    request_id=_request_id(request)
    category=_rate_limit_category(request.method,path)
    if category:
        identity=request.headers.get('x-real-ip') or (request.client.host if request.client else 'unknown')
        if not limiter.allow((identity,category),RATE_LIMITS[category]):
            return JSONResponse(status_code=429,content={'detail':'요청이 너무 많습니다. 잠시 후 다시 시도하세요.'},headers={'Retry-After':'60','X-Request-ID':request_id})
    response = await call_next(request)
    response.headers['X-Request-ID']=request_id
    if path.startswith('/api') and 'cache-control' not in response.headers:
        response.headers['Cache-Control'] = 'no-store'
    request_log.info('request completed',extra={'request_id':request_id,'user_id':request.scope.get('session',{}).get('uid'),'method':request.method,'path':path,'status_code':response.status_code,'duration_ms':round((time.monotonic()-started)*1000,2)})
    return response

@app.exception_handler(MarketError)
async def market_error(request, exc):
    return JSONResponse(status_code=503, content={'detail': str(exc)})

def csrf(request: Request):
    token = request.session.get('csrf', '')
    if not token or not secrets.compare_digest(token, request.headers.get('x-csrf-token', '')):
        raise HTTPException(403, '세션이 만료되었습니다. 페이지를 새로고침하세요.')

def current_user(request: Request):
    uid = request.session.get('uid')
    with Session() as db:
        user = db.get(User, uid) if uid else None
        if not user: raise HTTPException(401, '로그인이 필요합니다.')
        if not user.active: raise HTTPException(403, '정지된 계정입니다.')
        return user.id

class Credentials(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: str = Field(min_length=3, max_length=32, pattern=r'^[가-힣a-zA-Z0-9_]+$')
    password: str = Field(min_length=8, max_length=128)
    @field_validator('username')
    @classmethod
    def normalize(cls, v): return v.lower()

class Registration(Credentials):
    password_confirm: str = Field(max_length=128)

class Order(BaseModel):
    model_config = ConfigDict(extra='forbid')
    symbol: str = Field(pattern=SYMBOL_PATTERN)
    side: Literal['buy', 'sell']
    quantity: int = Field(gt=0, le=MAX_ORDER_QUANTITY, strict=True)
    request_id: UUID
    use_max: bool = False

@app.get('/')
def index():
    html = Path('app/static/index.html').read_text()
    return HTMLResponse(html.replace('{{BRAND_NAME}}', BRAND_NAME).replace('{{STORAGE_NAMESPACE}}', STORAGE_NAMESPACE), headers={'Cache-Control':'no-cache'})

@app.get('/health')
def health():
    with engine.connect() as db: db.execute(text('SELECT 1'))
    redis_status='ok' if redis_cache.ping() else 'unavailable'
    return {'status':'ok' if redis_status=='ok' else 'degraded','database':'ok','redis':redis_status,'mode':'paper-only'}

@app.get('/api/session')
def session(request: Request):
    if 'csrf' not in request.session: request.session['csrf'] = secrets.token_urlsafe(32)
    with Session() as db:
        user = db.get(User, request.session['uid']) if request.session.get('uid') else None
        return {'quote_sse_enabled': quote_sse_enabled(),
                'active': bool(user and user.active),
                'quote_max_age': {'US': quote_max_age('US'), 'KR': quote_max_age('KR')},
                'csrf': request.session['csrf'],
                'username': user.username if user else None,
                'is_admin': bool(user and user.is_admin),
                'market_configured': bool(market.key),
                'providers': market.status() if hasattr(market, 'status') else {'us': bool(market.key), 'kr': False}}

@app.post('/api/register', dependencies=[Depends(csrf)])
def register(data: Registration, request: Request):
    if not secrets.compare_digest(data.password.encode(), data.password_confirm.encode()):
        raise HTTPException(422, '비밀번호가 일치하지 않습니다.')
    try:
        with Session.begin() as db:
            amount = initial_amount(db)
            user = User(username=data.username, password_hash=hasher.hash(data.password), cash=amount, initial_usd=amount, created_at=datetime.now(timezone.utc))
            db.add(user)
            db.flush()
            uid = user.id
            wallets(db, user)
    except IntegrityError:
        raise HTTPException(409, '이미 사용 중인 사용자 이름입니다.')
    try: initialize_equity(uid, fx)
    except MarketError: pass
    # Registration does not sign in; the new account logs in from the start page.
    return {'ok': True, 'username': data.username}

@app.post('/api/login', dependencies=[Depends(csrf)])
def login(data: Credentials, request: Request):
    with Session() as db:
        user = db.scalar(select(User).where(User.username == data.username))
        try: hasher.verify(user.password_hash if user else dummy_hash, data.password)
        except (VerificationError, InvalidHashError): raise HTTPException(401, '사용자 이름 또는 비밀번호가 올바르지 않습니다.')
        if not user: raise HTTPException(401, '사용자 이름 또는 비밀번호가 올바르지 않습니다.')
        request.session.clear()
        request.session.update(uid=user.id, csrf=secrets.token_urlsafe(32))
    return {'ok': True}

@app.post('/api/logout', dependencies=[Depends(csrf)])
def logout(request: Request):
    request.session.clear()
    return {'ok': True}

@app.get('/api/search')
def search(q: str = Query('', max_length=60), category: str = Query('all'), uid=Depends(current_user)):
    if category not in CATEGORIES: raise HTTPException(422, '잘못된 자산 분류입니다.')
    return market.search(q, category)

@app.get('/api/quote/{symbol}')
def quote(symbol: str, uid=Depends(current_user)):
    if not valid_symbol(symbol): raise HTTPException(422, '잘못된 종목 코드입니다.')
    redis_cache.request_stream(symbol)  # the detail page is looking at it
    return market.quote(symbol)

@app.get('/api/market-stream/{symbol}')
async def market_stream(symbol: str, request: Request, uid=Depends(current_user)):
    return await app.state.quote_hub.response(request, symbol, uid, current_user)

@app.get('/api/market-overview')
def market_overview(uid=Depends(current_user)):
    rows=[]
    for code in ('KR','US'):
        status=dict(market.providers[code].market_status())
        status['market']=code
        rows.append(status)
    return {'markets':rows,'refreshed_at':datetime.now(timezone.utc),'refresh_seconds':60}

def closed_market_message(symbol):
    """Why the market cannot take an order now, or None to go on to the quote checks."""
    code = market_of(symbol)
    provider = (getattr(market, 'providers', {}) or {}).get(code)
    if provider is None or not hasattr(provider, 'market_status'): return None
    try: status = provider.market_status() or {}
    except (MarketError, KeyError, TypeError): status = {}
    session = status.get('session') or LEGACY_LABELS.get(status.get('label'), 'unknown')
    name = '한국' if code == 'KR' else '미국'
    if session == 'unknown':
        return f'{name} 시장 상태를 확인할 수 없어 주문할 수 없습니다. 잠시 후 다시 시도해주세요.'
    if status.get('label') == '휴장':
        return f'오늘은 {name} 주식시장 휴장일이므로 주문할 수 없습니다. 다음 거래일 장 운영 시간에 다시 주문해주세요.'
    if session not in SUPPORTED_ORDER_SESSIONS[code] or status.get('open') is False:
        return f'현재 {name} 주식시장이 휴장 중({status.get("label", "장마감")})이므로 주문할 수 없습니다. 장 운영 시간에 다시 주문해주세요.'
    if status.get('tradable') is False:
        return f'{name} {status.get("label")}은 열려 있지만 주문에 쓸 시세를 확인할 수 없어 주문할 수 없습니다.'
    return None

@app.post('/api/orders', dependencies=[Depends(csrf)])
def order(data: Order, uid=Depends(current_user)):
    requested_at=datetime.now(timezone.utc)
    with Session() as db:
        queued=db.scalar(select(LimitOrder).where(LimitOrder.user_id==uid,LimitOrder.request_id==str(data.request_id)))
        if queued:
            same_request=queued.order_type=='market' and (queued.symbol,queued.side,queued.quantity,queued.use_max)==(data.symbol,data.side,data.quantity,data.use_max)
            if not same_request: raise HTTPException(409,'동일 주문 ID에 다른 요청을 사용할 수 없습니다.')
            return {'id':queued.id,'pending':queued.status=='pending','status':queued.status,'replayed':True}
        # A request that already filled is answered before the market checks: the
        # replay trades nothing, so it must not depend on the market being open now.
        filled=filled_replay(db,uid,data)
    if filled: return filled
    closed=closed_market_message(data.symbol)
    if closed: raise HTTPException(409, closed)
    redis_cache.request_stream(data.symbol)
    # New market orders either settle immediately against a current provider
    # quote or fail clearly. They are never silently converted into a queue.
    result=execute_order(uid, data, market, requested_at=requested_at)
    event(uid,data.symbol,'order')
    return result

@app.get('/api/portfolio')
def portfolio(uid=Depends(current_user)):
    return wallet_portfolio(uid, market, fx)

def _public_user(db, username):
    """The account behind a public portfolio or performance page: active and not an admin.

    Usernames are stored lowercase (see Credentials), so any casing names the same account,
    as it does for login and /api/users/{username}/avatar."""
    return db.scalar(select(User).where(User.username == username.lower(), User.active.is_(True), User.is_admin.is_(False)))

# Explicit read-only projection. No internal IDs, credentials, admin memo,
# transactions or order IDs.
PUBLIC_PORTFOLIO_FIELDS = ('username', 'wallets', 'positions', 'equity', 'equity_usd', 'base_currency', 'pnl',
                           'return_pct', 'return_basis', 'fx', 'errors', 'stale',
                           'initial_equity', 'initial_fx_date', 'initial_fx_effect', 'other_pnl',
                           'pnl_usd', 'return_pct_usd', 'initial_usd')

@app.get('/api/portfolios/{username}')
def public_portfolio(username: str, uid=Depends(current_user)):
    with Session() as db:
        target=_public_user(db,username)
        if not target: raise HTTPException(404,'공개 포트폴리오를 찾을 수 없습니다.')
        target_id=target.id
        profile=profile_of(db,target_id)
        member={'member_since':target.created_at,'member_days':membership_days(target.created_at)}
    p=wallet_portfolio(target_id,market,fx)
    return {k:p[k] for k in PUBLIC_PORTFOLIO_FIELDS}|{'profile':profile}|member


TRANSACTION_FIELDS = ('id', 'symbol', 'side', 'quantity', 'price', 'currency', 'native_price', 'fx_rate', 'fx_date',
                      'quote_time', 'created_at', 'gross_amount', 'fee', 'tax', 'net_amount', 'realized_pnl',
                      'accounting_version', 'order_requested_at', 'market_session', 'venue', 'quote_source',
                      'price_mode', 'quote_stale')
TRANSACTIONS_PAGE_SIZE = 50

@app.get('/api/transactions')
def transactions(page: int = Query(1, ge=1), uid=Depends(current_user)):
    with Session() as db:
        rows = db.scalars(select(Transaction).where(Transaction.user_id == uid).order_by(Transaction.id.desc())
                          .offset((page-1)*TRANSACTIONS_PAGE_SIZE).limit(TRANSACTIONS_PAGE_SIZE))
        return [{field: getattr(t, field) for field in TRANSACTION_FIELDS} for t in rows]

RANKING_HOLD = '시세 또는 기준환율을 확인할 수 없어 랭킹을 보류합니다.'

def _ranking_payload(rows, errors, incomplete, updated_at, stale):
    """The cached snapshot: everything but the per-request state."""
    return {'rows': rows, 'errors': errors, 'incomplete': incomplete, 'base_currency': 'USD',
            'updated_at': updated_at, 'return_basis': RETURN_BASIS, 'stale': stale,
            'refresh_interval_seconds': RANKING_INTERVAL_SECONDS}

def _ranking_response(payload, state, next_boundary, refreshed, **overrides):
    return payload | overrides | {'refreshed': refreshed, 'market_open': state['open'],
                                  'market_status': state['labels'], 'next_refresh_at': next_boundary.isoformat()}

def _drop_ineligible(payload, names):
    """Remove suspended, promoted or deleted accounts from a snapshot in place and renumber the rest."""
    payload['rows'] = [row | {'rank': i + 1} for i, row in enumerate(row for row in payload['rows'] if row['username'] in names)]

@app.get('/api/ranking')
def ranking(uid=Depends(current_user)):
    now = datetime.now(timezone.utc)
    bucket = _ranking_bucket(now)
    next_boundary = _next_ranking_boundary(now)
    state = _ranking_market_state()
    # Keyed by the market object, so a replaced main.market (tests, the browser
    # fixture) never serves a snapshot valued by another market.
    cache_key = market
    with _ranking_lock:
        cached = _ranking_cache.get(cache_key)
        with Session() as db:
            eligible=list(db.execute(select(User.id,User.username).where(User.active.is_(True),User.is_admin.is_(False))))
        if cached: _drop_ineligible(cached['payload'], {name for _,name in eligible})
        # A closed holiday/weekend must not create a new ranking snapshot.  We
        # still return the last valid rows with their original as-of time.
        if cached and cached['payload'].get('updated_at') and state['open'] is False:
            return _ranking_response(cached['payload'], state, next_boundary, False)
        # Multiple browsers in the same ten-second window share one calculation.
        if cached and cached['bucket'] == bucket:
            return _ranking_response(cached['payload'], state, next_boundary, False)
        # A failed calculation is shared the same way until the window ends, so an
        # outage does not revalue every account on every request.
        if cached and cached.get('failed_bucket') == bucket:
            return _ranking_response(cached['payload'], state, next_boundary, False,
                                     errors=[RANKING_HOLD], incomplete=True, stale=True)

        ids=[user_id for user_id,_ in eligible]
        values=[wallet_portfolio(i,market,fx) for i in ids]
        if any(v['return_pct'] is None or v.get('equity_usd') is None for v in values):
            if cached:
                # Keep serving the last good rows; remember the failure for this window.
                cached['failed_bucket'] = bucket
                return _ranking_response(cached['payload'], state, next_boundary, False,
                                         errors=[RANKING_HOLD], incomplete=True, stale=True)
            payload=_ranking_payload([], [RANKING_HOLD], incomplete=True, updated_at=None, stale=True)
            _ranking_cache[cache_key] = {'bucket': bucket, 'payload': payload}
            return _ranking_response(payload, state, next_boundary, True)
        # Rank by total value in USD; the cumulative return is display only.
        ranked=sorted(zip(ids,values),key=lambda pair:(-pair[1]['equity_usd'],pair[1]['username']))
        with Session() as db: versions=profile_versions(db,ids)
        rows=[{'rank':i+1,'username':v['username'],'equity':v['equity'],'equity_usd':v['equity_usd'],
               'return_pct':v['return_pct'],'return_pct_usd':v['return_pct_usd'],'stale':v['stale'],'fx':v['fx'],
               'image_version':versions.get(i_id,0)}
              for i,(i_id,v) in enumerate(ranked)]
        payload=_ranking_payload(rows, [], incomplete=False, updated_at=now.isoformat(), stale=any(v['stale'] for v in values))
        _ranking_cache[cache_key] = {'bucket': bucket, 'payload': payload}
        return _ranking_response(payload, state, next_boundary, True)


def performance_view(target_id, username, period, start, end):
    today = datetime.now(timezone.utc).astimezone(SNAPSHOT_ZONE).date()
    try:
        if start or end:
            first = date.fromisoformat(start) if start else date.min
            last = date.fromisoformat(end) if end else today
        else:
            first, last = period_range(period, today)
    except ValueError: raise HTTPException(422, '기간은 1W, 1M, 3M, 1Y, YTD, ALL 또는 YYYY-MM-DD 형식입니다.')
    if first > last: raise HTTPException(422, '시작일이 종료일보다 늦습니다.')
    return {'username': username, 'period': None if start or end else period, 'from': first if first != date.min else None,
            'to': last, 'timezone': 'Asia/Seoul', 'return_basis': 'KRW 평가액 · 외부 입출금 보정'} | series(target_id, first, last)

PerformancePeriod = Literal['1W', '1M', '3M', '1Y', 'YTD', 'ALL']

@app.get('/api/performance/me')
def my_performance(period: PerformancePeriod = '1M', start: str | None = Query(None, alias='from'), end: str | None = Query(None, alias='to'), uid=Depends(current_user)):
    with Session() as db: username = db.get(User, uid).username
    return performance_view(uid, username, period, start, end)

@app.get('/api/performance/{username}')
def public_performance(username: str, period: PerformancePeriod = '1M', start: str | None = Query(None, alias='from'), end: str | None = Query(None, alias='to'), uid=Depends(current_user)):
    with Session() as db:
        target = _public_user(db, username)
        if not target: raise HTTPException(404, '공개 성과 기록을 찾을 수 없습니다.')
        target_id, username = target.id, target.username
    return performance_view(target_id, username, period, start, end)

@app.get('/api/weekly')
def weekly(page: int = Query(1, ge=1), uid=Depends(current_user)):
    return report_list(page)


from .routes import install
import sys
install(app, sys.modules[__name__])
from .accounts import install_accounts
install_accounts(app, sys.modules[__name__])


@app.post('/internal/jobs')
def internal_jobs(request: Request):
    if not secrets.compare_digest(request.headers.get(WORKER_TOKEN_HEADER,''),worker_token(secret)): raise HTTPException(403,'Forbidden')
    filled=process_limit_orders(market)
    weekly_result=tick(market,fx=fx) if os.getenv('WEEKLY_ENABLED','true').lower()=='true' else 'disabled'
    try: snapshots=capture_daily_snapshots(market,fx)
    except Exception:
        request_log.exception('daily snapshot failed'); snapshots='error'
    return {'filled':filled,'weekly':weekly_result,'snapshots':snapshots}
