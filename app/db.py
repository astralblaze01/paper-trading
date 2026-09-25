import os
from decimal import Decimal
from datetime import datetime
from datetime import date
from sqlalchemy import create_engine, String, Numeric, Integer, ForeignKey, DateTime, Date, Boolean, CheckConstraint, UniqueConstraint, Index, URL, LargeBinary, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

url = os.getenv('DATABASE_URL') or URL.create('postgresql+psycopg', username='paper', password=os.environ['DB_PASSWORD'], host=os.getenv('DB_HOST', 'db'), database='paper')
engine = create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=20, pool_recycle=1800)
Session = sessionmaker(engine, expire_on_commit=False)
# PostgreSQL advisory lock keys, shared by every process that uses the database.
MIGRATION_LOCK = 74923101  # one migrator at a time
ACCOUNT_LOCK = 74923102    # account-wide writes and the weekly publisher never interleave
SNAPSHOT_LOCK = 74923103   # one daily snapshot run at a time
class Base(DeclarativeBase): pass
class User(Base):
    __tablename__ = 'users'
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    cash: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=Decimal('100000'))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default='false')
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default='true')
    initial_usd: Mapped[Decimal] = mapped_column(Numeric(24,4), default=Decimal(100000), server_default='100000')
    initial_krw: Mapped[Decimal | None] = mapped_column(Numeric(24,4), nullable=True)
    initial_fx_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    net_contributions_krw: Mapped[Decimal] = mapped_column(Numeric(24,4), default=Decimal(0), server_default='0')
    records_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    performance_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    baseline_note: Mapped[str] = mapped_column(String(40), default='registration', server_default='registration')
    # Sign-up time. Accounts older than this column carry an estimate (migration 9).
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=True)
    __table_args__ = (CheckConstraint('cash >= 0'),)

def lock_user(db, user_id):
    """The user row FOR UPDATE, or None: the per-account mutex every balance or position change takes first."""
    return db.scalar(select(User).where(User.id == user_id).with_for_update())

class Position(Base):
    __tablename__ = 'positions'
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    quantity: Mapped[int] = mapped_column(Integer)
    average_cost: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    native_average_cost: Mapped[Decimal | None] = mapped_column(Numeric(24,10), nullable=True)
    __table_args__ = (CheckConstraint('quantity >= 0'), CheckConstraint('average_cost >= 0'))
class Transaction(Base):
    __tablename__ = 'transactions'
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    request_id: Mapped[str] = mapped_column(String(36))
    symbol: Mapped[str] = mapped_column(String(16))
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    currency: Mapped[str] = mapped_column(String(3), default='USD', server_default='USD')
    native_price: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    fx_rate: Mapped[Decimal] = mapped_column(Numeric(24, 12), default=Decimal(1), server_default='1')
    fx_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    quote_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    gross_amount: Mapped[Decimal | None] = mapped_column(Numeric(24,4), nullable=True)
    fee: Mapped[Decimal] = mapped_column(Numeric(24,4), default=Decimal(0), server_default='0')
    tax: Mapped[Decimal] = mapped_column(Numeric(24,4), default=Decimal(0), server_default='0')
    net_amount: Mapped[Decimal | None] = mapped_column(Numeric(24,4), nullable=True)
    fee_bps: Mapped[Decimal] = mapped_column(Numeric(10,4), default=Decimal(0), server_default='0')
    tax_bps: Mapped[Decimal] = mapped_column(Numeric(10,4), default=Decimal(0), server_default='0')
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(24,4), default=Decimal(0), server_default='0')
    accounting_version: Mapped[int] = mapped_column(Integer, default=2, server_default='2')
    # Research metadata (migration 10). created_at is the fill time; the
    # request time is kept separately so request-to-fill latency is measurable.
    # NULL on trades recorded before these columns existed.
    order_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    market_session: Mapped[str | None] = mapped_column(String(16), nullable=True)
    venue: Mapped[str | None] = mapped_column(String(12), nullable=True)   # US, UNIFIED (KRX+NXT), ...
    quote_source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    price_mode: Mapped[str | None] = mapped_column(String(24), nullable=True)
    quote_stale: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    __table_args__ = (UniqueConstraint('user_id', 'request_id'), CheckConstraint('quantity > 0'), CheckConstraint('price > 0'), CheckConstraint("side IN ('buy', 'sell')"))

# Weekly results and scheduler checkpoints survive application restarts.
from sqlalchemy.dialects.postgresql import JSONB

class WeeklyState(Base):
    __tablename__ = 'weekly_state'
    id: Mapped[int] = mapped_column(primary_key=True)
    baseline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    baseline: Mapped[dict] = mapped_column(JSONB, default=dict)
    currency: Mapped[str] = mapped_column(String(3), default='USD', server_default='USD')
    next_due: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_attempt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)

class WeeklyReport(Base):
    __tablename__ = 'weekly_reports'
    id: Mapped[int] = mapped_column(primary_key=True)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), unique=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    rows: Mapped[list] = mapped_column(JSONB)
    notes: Mapped[dict] = mapped_column(JSONB)

class ReportPrice(Base):
    __tablename__ = 'report_prices'
    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    price: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    quote_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    native_price: Mapped[Decimal | None] = mapped_column(Numeric(20,4), nullable=True)
    fx_date: Mapped[str | None] = mapped_column(String(10), nullable=True)

class Wallet(Base):
    __tablename__ = 'wallets'
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), primary_key=True)
    currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    balance: Mapped[Decimal] = mapped_column(Numeric(24,4), default=Decimal(0))
    __table_args__ = (CheckConstraint('balance >= 0'), CheckConstraint("currency IN ('USD','KRW')"))

class FxTransaction(Base):
    __tablename__ = 'fx_transactions'
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    request_id: Mapped[str] = mapped_column(String(36))
    source: Mapped[str] = mapped_column(String(3))
    target: Mapped[str] = mapped_column(String(3))
    amount: Mapped[Decimal] = mapped_column(Numeric(24,4))
    received: Mapped[Decimal] = mapped_column(Numeric(24,4))
    fee: Mapped[Decimal] = mapped_column(Numeric(24,4))
    rate: Mapped[Decimal] = mapped_column(Numeric(24,12))
    fee_bps: Mapped[Decimal] = mapped_column(Numeric(10,4))
    spread_bps: Mapped[Decimal] = mapped_column(Numeric(10,4))
    rate_date: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint('user_id','request_id'),)

class Watchlist(Base):
    __tablename__ = 'watchlists'
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

class PopularityEvent(Base):
    __tablename__ = 'popularity_events'
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    symbol: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(12))
    bucket: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    __table_args__ = (UniqueConstraint('user_id','symbol','kind','bucket'),)

class Settings(Base):
    __tablename__ = 'settings'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(128))

class SeasonArchive(Base):
    __tablename__ = 'season_archives'
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    label: Mapped[str] = mapped_column(String(80))
    data: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

class LimitOrder(Base):
    __tablename__ = 'limit_orders'
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    request_id: Mapped[str] = mapped_column(String(36))
    symbol: Mapped[str] = mapped_column(String(16))
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[int] = mapped_column(Integer)
    limit_price: Mapped[Decimal] = mapped_column(Numeric(20,4))
    order_type: Mapped[str] = mapped_column(String(8), default='limit', server_default='limit')
    use_max: Mapped[bool] = mapped_column(Boolean, default=False, server_default='false')
    status: Mapped[str] = mapped_column(String(16), default='pending', index=True)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint('user_id','request_id'), CheckConstraint('quantity > 0'), CheckConstraint('limit_price > 0'))

class AdminAudit(Base):
    __tablename__ = 'admin_audits'
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    target_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    request_id: Mapped[str] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(String(300))
    data: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint('actor_id','request_id'),)

class WalletTransfer(Base):
    __tablename__ = 'wallet_transfers'
    id: Mapped[int] = mapped_column(primary_key=True)
    # NULL once that side withdraws: the counterparty keeps its history.
    sender_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'), index=True, nullable=True)
    recipient_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'), index=True, nullable=True)
    request_id: Mapped[str] = mapped_column(String(36))
    currency: Mapped[str] = mapped_column(String(3))
    amount: Mapped[Decimal] = mapped_column(Numeric(24,4))
    fee: Mapped[Decimal] = mapped_column(Numeric(24,4))
    fee_bps: Mapped[Decimal] = mapped_column(Numeric(10,4))
    fx_rate: Mapped[Decimal] = mapped_column(Numeric(24,12))
    rate_date: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint('sender_id','request_id'), CheckConstraint('amount > 0'), CheckConstraint('fee >= 0'), CheckConstraint('sender_id != recipient_id'))

class UserAdminNote(Base):
    """Administrator-only memo. Kept out of users so no user projection can leak it."""
    __tablename__ = 'user_admin_notes'
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), primary_key=True)
    note: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

class UserProfile(Base):
    __tablename__ = 'user_profiles'
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), primary_key=True)
    bio: Mapped[str] = mapped_column(String(160), default='', server_default='')
    # 0 means no image; bumped on every change so avatar URLs can be cached.
    image_version: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

class UserProfileImage(Base):
    """Re-encoded WebP bytes; the uploaded file and its name are never stored."""
    __tablename__ = 'user_profile_images'
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), primary_key=True)
    content_type: Mapped[str] = mapped_column(String(20))
    data: Mapped[bytes] = mapped_column(LargeBinary)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

class SiteNotice(Base):
    """Notice shown to users. At most one is active; past ones stay as a record."""
    __tablename__ = 'site_notices'
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(60))
    body: Mapped[str] = mapped_column(String(500))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    posted_by: Mapped[int | None] = mapped_column(ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PerformanceSnapshot(Base):
    """One valuation per account per Korean calendar day: research raw data.

    Enough is stored to recompute the valuation without today's prices or FX:
    holdings, the prices and quote facts used, the reference rate and the
    return baseline. The cumulative return uses portfolio.performance_return."""
    __tablename__ = 'performance_snapshots'
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    snapshot_date: Mapped[date] = mapped_column(Date)            # Asia/Seoul
    snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))    # valuation time (UTC)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    equity_krw: Mapped[Decimal] = mapped_column(Numeric(24,4))
    equity_usd: Mapped[Decimal] = mapped_column(Numeric(24,4))
    cash_krw: Mapped[Decimal] = mapped_column(Numeric(24,4))
    cash_usd: Mapped[Decimal] = mapped_column(Numeric(24,4))
    net_contributions_krw: Mapped[Decimal] = mapped_column(Numeric(24,4))
    initial_equity_krw: Mapped[Decimal] = mapped_column(Numeric(24,4))
    cumulative_return_pct: Mapped[Decimal] = mapped_column(Numeric(20,8))
    fx_rate: Mapped[Decimal] = mapped_column(Numeric(24,12))      # KRW per USD (ECB reference)
    fx_date: Mapped[str] = mapped_column(String(10))
    positions: Mapped[dict] = mapped_column(JSONB)
    quote_metadata: Mapped[dict] = mapped_column(JSONB)
    quality: Mapped[dict] = mapped_column(JSONB)
    stale: Mapped[bool] = mapped_column(Boolean)
    oldest_quote_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    baseline: Mapped[dict] = mapped_column(JSONB)                 # return baseline in force that day
    method_version: Mapped[int] = mapped_column(Integer, default=1, server_default='1')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint('user_id', 'snapshot_date'), Index('idx_performance_snapshots_date', 'snapshot_date'))

class BenchmarkSnapshot(Base):
    """Daily benchmark price captured in the same run as the account snapshots."""
    __tablename__ = 'benchmark_snapshots'
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16))
    snapshot_date: Mapped[date] = mapped_column(Date)
    price: Mapped[Decimal] = mapped_column(Numeric(20,4))          # native currency
    currency: Mapped[str] = mapped_column(String(3))
    quote_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    price_mode: Mapped[str | None] = mapped_column(String(24), nullable=True)
    market_session: Mapped[str | None] = mapped_column(String(16), nullable=True)
    stale: Mapped[bool] = mapped_column(Boolean)
    fx_rate: Mapped[Decimal] = mapped_column(Numeric(24,12))      # KRW per USD used that day
    fx_date: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint('symbol', 'snapshot_date'),)

class SnapshotRun(Base):
    """Per-day run log: whether the day completed, and why accounts were skipped."""
    __tablename__ = 'snapshot_runs'
    snapshot_date: Mapped[date] = mapped_column(Date, primary_key=True)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_attempt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    eligible: Mapped[int] = mapped_column(Integer, default=0)
    succeeded: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[dict] = mapped_column(JSONB, default=dict)
    benchmarks: Mapped[dict] = mapped_column(JSONB, default=dict)
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
