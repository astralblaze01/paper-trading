import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Literal
from uuid import UUID
from decimal import Decimal
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Request, HTTPException, Depends, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel, Field, ConfigDict, field_validator
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from .db import Base, engine, Session, User, Position, Transaction, LimitOrder
from .market import MarketError
from .multi_market import MultiMarket
from .instruments import SYMBOL_PATTERN, valid_symbol, instrument, CATEGORIES
from .migrations import migrate
from .trading import execute_order
from .money import wallets, initial_amount
from .fx import FxService
from .portfolio import portfolio as wallet_portfolio, initialize_equity, RETURN_BASIS
from .weekly import WeeklyWorker, report_list

secret = os.environ['SESSION_SECRET']
if len(secret) < 32: raise RuntimeError('SESSION_SECRET must have at least 32 characters')
market = MultiMarket()
fx = FxService(market.fx)
hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
dummy_hash = hasher.hash(secrets.token_urlsafe(32))
SEOUL = ZoneInfo('Asia/Seoul')
RANKING_INTERVAL = timedelta(minutes=30)
_ranking_lock = RLock()
# Ranking is deliberately process-local.  The response is a view of the
# database, and the half-hour boundary keeps provider calls bounded per web
# process while the returned timestamp makes the snapshot explicit.
_ranking_cache = {}


def _ranking_bucket(now=None):
    local = (now or datetime.now(timezone.utc)).astimezone(SEOUL)
    return local.replace(minute=(local.minute // 30) * 30, second=0, microsecond=0)


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
    open_labels = {'정규장', '장전', '장후'}
    unknown = any(s.get('label') == '장 상태 확인 불가' for s in statuses)
    is_open = any(s.get('label') in open_labels for s in statuses)
    return {'open': is_open if not (unknown and not is_open) else None,
            'unknown': unknown, 'labels': statuses}

@asynccontextmanager
async def lifespan(app):
    Base.metadata.create_all(engine)
    migrate(engine)
    worker = None  # Scheduled work is driven by the separate worker container.
    if worker: worker.start()
    try:
        yield
    finally:
        if worker: worker.stop()
        market.client.close()

app = FastAPI(title='ASTER', lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=secret, session_cookie='paper_session', max_age=43200, same_site='strict', https_only=os.getenv('COOKIE_SECURE', 'false').lower() == 'true')
app.mount('/static', StaticFiles(directory='app/static'), name='static')

@app.middleware('http')
async def no_cache(request, call_next):
    from .security import limiter
    path=request.url.path
    category = 'auth' if path in ('/api/login','/api/register') else 'trade' if path in ('/api/orders','/api/fx/exchange','/api/limit-orders','/api/transfers','/api/transfers/preview') else 'market' if path.startswith(('/api/search','/api/quote','/api/candles','/api/explore','/api/order-preview','/api/fx/preview','/api/market-status','/api/company','/api/portfolios')) else 'event' if path=='/api/popularity' else None
    if category:
        identity=request.headers.get('x-real-ip') or (request.client.host if request.client else 'unknown')
        limit={'auth':20,'trade':30,'market':120,'event':60}[category]
        if not limiter.allow((identity,category),limit):
            return JSONResponse(status_code=429,content={'detail':'요청이 너무 많습니다. 잠시 후 다시 시도하세요.'},headers={'Retry-After':'60'})
    response = await call_next(request)
    if request.url.path.startswith('/api'):
        response.headers['Cache-Control'] = 'no-store'
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
    username: str = Field(min_length=3, max_length=32, pattern=r'^[a-zA-Z0-9_]+$')
    password: str = Field(min_length=8, max_length=128)
    @field_validator('username')
    @classmethod
    def normalize(cls, v): return v.lower()

class Order(BaseModel):
    model_config = ConfigDict(extra='forbid')
    symbol: str = Field(pattern=SYMBOL_PATTERN)
    side: Literal['buy', 'sell']
    quantity: int = Field(gt=0, le=1000000, strict=True)
    request_id: UUID
    use_max: bool = False

@app.get('/')
def index(): return FileResponse('app/static/index.html')

@app.get('/health')
def health():
    with engine.connect() as db: db.execute(text('SELECT 1'))
    return {'status': 'ok', 'mode': 'paper-only'}

@app.get('/api/session')
def session(request: Request):
    if 'csrf' not in request.session: request.session['csrf'] = secrets.token_urlsafe(32)
    with Session() as db:
        user = db.get(User, request.session['uid']) if request.session.get('uid') else None
        return {'csrf': request.session['csrf'], 'username': user.username if user else None, 'is_admin': bool(user and user.is_admin), 'market_configured': bool(market.key), 'providers': market.status() if hasattr(market, 'status') else {'us': bool(market.key), 'kr': False}}

@app.post('/api/register', dependencies=[Depends(csrf)])
def register(data: Credentials, request: Request):
    try:
        with Session.begin() as db:
            amount = initial_amount(db)
            user = User(username=data.username, password_hash=hasher.hash(data.password), cash=amount, initial_usd=amount)
            db.add(user)
            db.flush()
            uid = user.id
            wallets(db, user)
    except IntegrityError:
        raise HTTPException(409, '이미 사용 중인 사용자 이름입니다.')
    try: initialize_equity(uid, fx)
    except MarketError: pass
    request.session.clear()
    request.session.update(uid=uid, csrf=secrets.token_urlsafe(32))
    return {'ok': True}

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
    return market.quote(symbol)

@app.post('/api/orders', dependencies=[Depends(csrf)])
def order(data: Order, uid=Depends(current_user)):
    with Session() as db:
        queued=db.scalar(select(LimitOrder).where(LimitOrder.user_id==uid,LimitOrder.request_id==str(data.request_id)))
        if queued:
            if queued.order_type!='market' or (queued.symbol,queued.side,queued.quantity,queued.use_max)!=(data.symbol,data.side,data.quantity,data.use_max):
                raise HTTPException(409,'동일 주문 ID에 다른 요청을 사용할 수 없습니다.')
            return {'id':queued.id,'pending':queued.status=='pending','status':queued.status,'replayed':True}
    try:
        result=execute_order(uid, data, market)
    except HTTPException as exc:
        if exc.status_code!=409 or exc.detail not in ('오래된 시세로는 주문할 수 없습니다.','시세가 만료되었습니다.'):
            raise
        from .limits import pending_market
        return pending_market(uid,data)
    from .routes import event
    event(uid,data.symbol,'order')
    return result

def valuation(user, positions, quotes):
    rows = []
    equity = user.cash
    complete = True
    for p in positions:
        q = quotes.get(p.symbol)
        value = q['price'] * p.quantity if q else None
        if value is None: complete = False
        else: equity += value
        rows.append({**instrument(p.symbol), 'quantity': p.quantity, 'average_cost': p.average_cost, 'quote': q, 'value': value, 'pnl': value - p.average_cost * p.quantity if value is not None else None})
    return {'username': user.username, 'cash': user.cash, 'positions': rows, 'equity': equity if complete else None, 'pnl': equity - Decimal(100000) if complete else None, 'return_pct': (equity / Decimal(100000) - 1) * 100 if complete else None, 'stale': any(q and q['stale'] for q in quotes.values())}

def get_quotes(symbols):
    quotes, errors = {}, []
    for symbol in sorted(symbols):
        try: quotes[symbol] = market.quote(symbol)
        except MarketError as exc: errors.append(f'{symbol}: {exc}')
    return quotes, errors

@app.get('/api/portfolio')
def portfolio(uid=Depends(current_user)):
    return wallet_portfolio(uid, market, fx)

@app.get('/api/portfolios/{username}')
def public_portfolio(username: str, uid=Depends(current_user)):
    with Session() as db:
        target=db.scalar(select(User).where(User.username==username,User.active.is_(True),User.is_admin.is_(False)))
        if not target: raise HTTPException(404,'공개 포트폴리오를 찾을 수 없습니다.')
        target_id=target.id
    p=wallet_portfolio(target_id,market,fx)
    # Explicit read-only projection. No transactions, account credentials or order IDs.
    return {k:p[k] for k in ('username','wallets','positions','equity','base_currency','pnl','return_pct','return_basis','fx','errors','stale')}


@app.get('/api/transactions')
def transactions(page: int = Query(1, ge=1), uid=Depends(current_user)):
    with Session() as db:
        rows = db.scalars(select(Transaction).where(Transaction.user_id == uid).order_by(Transaction.id.desc()).offset((page-1)*50).limit(50))
        return [{'id': t.id, 'symbol': t.symbol, 'side': t.side, 'quantity': t.quantity, 'price': t.price, 'currency': t.currency, 'native_price': t.native_price, 'fx_rate': t.fx_rate, 'fx_date': t.fx_date, 'quote_time': t.quote_time, 'created_at': t.created_at, 'gross_amount': t.gross_amount, 'fee': t.fee, 'tax': t.tax, 'net_amount': t.net_amount, 'realized_pnl': t.realized_pnl, 'accounting_version': t.accounting_version} for t in rows]

@app.get('/api/ranking')
def ranking(uid=Depends(current_user)):
    now = datetime.now(timezone.utc)
    bucket = _ranking_bucket(now)
    next_boundary = _next_ranking_boundary(now)
    state = _ranking_market_state()
    cache_key = market
    with _ranking_lock:
        cached = _ranking_cache.get(cache_key)
        # A closed holiday/weekend must not create a new ranking snapshot.  We
        # still return the last valid rows with their original as-of time.
        if cached and state['open'] is False:
            return cached['payload'] | {
                'refreshed': False,
                'market_open': False,
                'market_status': state['labels'],
                'next_refresh_at': next_boundary.isoformat(),
            }
        # Multiple browsers in the same half-hour share one calculation.
        if cached and cached['bucket'] == bucket:
            return cached['payload'] | {
                'refreshed': False,
                'market_open': state['open'],
                'market_status': state['labels'],
                'next_refresh_at': next_boundary.isoformat(),
            }

        with Session() as db:
            ids=list(db.scalars(select(User.id).where(User.active.is_(True),User.is_admin.is_(False))))
        values=[wallet_portfolio(i,market,fx) for i in ids]
        if any(v['return_pct'] is None for v in values):
            error='시세 또는 기준환율을 확인할 수 없어 랭킹을 보류합니다.'
            if cached:
                return cached['payload'] | {
                    'errors': [error], 'incomplete': True, 'refreshed': False,
                    'market_open': state['open'], 'market_status': state['labels'],
                    'next_refresh_at': next_boundary.isoformat(),
                }
            payload={'rows': [], 'errors': [error], 'incomplete': True,
                     'base_currency': 'KRW', 'updated_at': None,
                     'return_basis': RETURN_BASIS,
                     'refresh_interval_minutes': 30}
            _ranking_cache[cache_key] = {'bucket': bucket, 'payload': payload}
            return payload | {'refreshed': True, 'market_open': state['open'],
                              'market_status': state['labels'],
                              'next_refresh_at': next_boundary.isoformat()}
        values.sort(key=lambda x:(-x['return_pct'],x['username']))
        payload={'rows':[{'rank':i+1,'username':v['username'],'equity':v['equity'],
                         'return_pct':v['return_pct'],'stale':v['stale'],'fx':v['fx']}
                        for i,v in enumerate(values)],
                 'errors':[], 'incomplete':False, 'base_currency':'KRW',
                 'updated_at': now.isoformat(),
                 'return_basis': RETURN_BASIS,
                 'refresh_interval_minutes':30}
        _ranking_cache[cache_key] = {'bucket': bucket, 'payload': payload}
        return payload | {'refreshed': True, 'market_open': state['open'],
                          'market_status': state['labels'],
                          'next_refresh_at': next_boundary.isoformat()}


@app.get('/api/weekly')
def weekly(page: int = Query(1, ge=1), uid=Depends(current_user)):
    return report_list(page)


from .routes import install
import sys
install(app, sys.modules[__name__])
from .transfers import install_transfers
install_transfers(app, sys.modules[__name__])


@app.post('/internal/jobs')
def internal_jobs(request: Request):
    import hmac, hashlib
    expected=hmac.new(secret.encode(),b'paper-worker',hashlib.sha256).hexdigest()
    if not secrets.compare_digest(request.headers.get('x-worker-token',''),expected): raise HTTPException(403,'Forbidden')
    from .limits import process
    from .weekly import tick
    filled=process(market)
    weekly_result=tick(market,fx=fx) if os.getenv('WEEKLY_ENABLED','true').lower()=='true' else 'disabled'
    return {'filled':filled,'weekly':weekly_result}
