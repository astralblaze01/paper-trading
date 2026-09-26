"""Additive, idempotent migration: preserve existing USD accounts and trades."""
from sqlalchemy import text
from .db import MIGRATION_LOCK

def migrate(engine):
    with engine.begin() as db:
        db.execute(text('SELECT pg_advisory_xact_lock(:k)'), {'k': MIGRATION_LOCK})
        db.execute(text("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS currency VARCHAR(3) NOT NULL DEFAULT 'USD'"))
        db.execute(text('ALTER TABLE transactions ADD COLUMN IF NOT EXISTS native_price NUMERIC(20,4)'))
        db.execute(text('ALTER TABLE transactions ADD COLUMN IF NOT EXISTS fx_rate NUMERIC(24,12) NOT NULL DEFAULT 1'))
        db.execute(text('ALTER TABLE transactions ADD COLUMN IF NOT EXISTS fx_date VARCHAR(10)'))
        db.execute(text('UPDATE transactions SET native_price = price WHERE native_price IS NULL'))
        db.execute(text('ALTER TABLE transactions ALTER COLUMN native_price SET NOT NULL'))
        db.execute(text('CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())'))
        db.execute(text("ALTER TABLE weekly_state ADD COLUMN IF NOT EXISTS currency VARCHAR(3) NOT NULL DEFAULT 'USD'"))
        db.execute(text('ALTER TABLE report_prices ADD COLUMN IF NOT EXISTS native_price NUMERIC(20,4)'))
        if not db.scalar(text('SELECT 1 FROM schema_migrations WHERE version=2')):
            statements = [
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT false",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT true",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS initial_usd NUMERIC(24,4) NOT NULL DEFAULT 100000",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS initial_krw NUMERIC(24,4)",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS initial_fx_date VARCHAR(10)",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS baseline_note VARCHAR(40) NOT NULL DEFAULT 'migration-first-valid-fx'",
                "ALTER TABLE positions ADD COLUMN IF NOT EXISTS native_average_cost NUMERIC(24,10)",
                "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS gross_amount NUMERIC(24,4)",
                "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS fee NUMERIC(24,4) NOT NULL DEFAULT 0",
                "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS tax NUMERIC(24,4) NOT NULL DEFAULT 0",
                "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS net_amount NUMERIC(24,4)",
                "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS fee_bps NUMERIC(10,4) NOT NULL DEFAULT 0",
                "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS tax_bps NUMERIC(10,4) NOT NULL DEFAULT 0",
                "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS realized_pnl NUMERIC(24,4) NOT NULL DEFAULT 0",
                "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS accounting_version INTEGER NOT NULL DEFAULT 1",
                "INSERT INTO wallets(user_id,currency,balance) SELECT id,'USD',cash FROM users ON CONFLICT DO NOTHING",
                "INSERT INTO wallets(user_id,currency,balance) SELECT id,'KRW',0 FROM users ON CONFLICT DO NOTHING",
                "UPDATE transactions SET gross_amount=native_price*quantity, net_amount=native_price*quantity WHERE gross_amount IS NULL",
                "UPDATE positions SET native_average_cost=average_cost WHERE native_average_cost IS NULL AND symbol NOT LIKE 'KR:%'",
            ]
            for statement in statements: db.execute(text(statement))
            # Reconstruct native weighted cost of legacy KR positions without rewriting trade records.
            from decimal import Decimal
            positions = db.execute(text("SELECT user_id,symbol,quantity FROM positions WHERE native_average_cost IS NULL")).all()
            for uid, symbol, remaining in positions:
                quantity, average = 0, Decimal(0)
                for trade in db.execute(text('SELECT side,quantity,native_price FROM transactions WHERE user_id=:u AND symbol=:s ORDER BY id'), {'u':uid,'s':symbol}):
                    if trade.side == 'buy':
                        average = (average*quantity + trade.native_price*trade.quantity)/(quantity+trade.quantity)
                        quantity += trade.quantity
                    else: quantity -= trade.quantity
                if quantity != remaining or quantity < 0 or average <= 0:
                    raise RuntimeError('Cannot safely reconstruct legacy native position cost; migration rolled back')
                db.execute(text('UPDATE positions SET native_average_cost=:a WHERE user_id=:u AND symbol=:s'), {'a':average,'u':uid,'s':symbol})
            db.execute(text('INSERT INTO schema_migrations(version) VALUES (2)'))
        if not db.scalar(text('SELECT 1 FROM schema_migrations WHERE version=3')):
            db.execute(text("ALTER TABLE limit_orders ADD COLUMN IF NOT EXISTS order_type VARCHAR(8) NOT NULL DEFAULT 'limit'"))
            db.execute(text("ALTER TABLE limit_orders ADD COLUMN IF NOT EXISTS use_max BOOLEAN NOT NULL DEFAULT false"))
            db.execute(text('INSERT INTO schema_migrations(version) VALUES (3)'))

        if not db.scalar(text('SELECT 1 FROM schema_migrations WHERE version=4')):
            db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS net_contributions_krw NUMERIC(24,4) NOT NULL DEFAULT 0"))
            db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS performance_since TIMESTAMPTZ"))
            db.execute(text('INSERT INTO schema_migrations(version) VALUES (4)'))

        if not db.scalar(text('SELECT 1 FROM schema_migrations WHERE version=5')):
            db.execute(text('ALTER TABLE users ADD COLUMN IF NOT EXISTS records_since TIMESTAMPTZ'))
            db.execute(text('INSERT INTO schema_migrations(version) VALUES (5)'))

        if not db.scalar(text('SELECT 1 FROM schema_migrations WHERE version=6')):
            db.execute(text('CREATE INDEX IF NOT EXISTS idx_transactions_user_created_at ON transactions(user_id, created_at DESC)'))
            db.execute(text('CREATE INDEX IF NOT EXISTS idx_transactions_symbol ON transactions(symbol)'))
            db.execute(text('CREATE INDEX IF NOT EXISTS idx_limit_orders_user_status ON limit_orders(user_id, status)'))
            db.execute(text('CREATE INDEX IF NOT EXISTS idx_limit_orders_user_created_at ON limit_orders(user_id, created_at DESC)'))
            db.execute(text('CREATE INDEX IF NOT EXISTS idx_limit_orders_symbol ON limit_orders(symbol)'))
            db.execute(text('INSERT INTO schema_migrations(version) VALUES (6)'))

        if not db.scalar(text('SELECT 1 FROM schema_migrations WHERE version=7')):
            # Transfers survive a withdrawal with the departed side detached.
            db.execute(text('ALTER TABLE wallet_transfers ALTER COLUMN sender_id DROP NOT NULL'))
            db.execute(text('ALTER TABLE wallet_transfers ALTER COLUMN recipient_id DROP NOT NULL'))
            db.execute(text('INSERT INTO schema_migrations(version) VALUES (7)'))

        if not db.scalar(text('SELECT 1 FROM schema_migrations WHERE version=8')):
            # The on/off maintenance switch became a notice with a type.
            if db.scalar(text("SELECT value FROM settings WHERE key='MAINTENANCE_NOTICE'"))=='on':
                from .notices import TEMPLATES
                template=TEMPLATES['maintenance']
                db.execute(text("INSERT INTO site_notices(kind,title,body,active,posted_at) VALUES ('maintenance',:t,:b,true,now())"),{'t':template['title'],'b':template['body']})
            db.execute(text("DELETE FROM settings WHERE key='MAINTENANCE_NOTICE'"))
            db.execute(text('INSERT INTO schema_migrations(version) VALUES (8)'))

        if not db.scalar(text('SELECT 1 FROM schema_migrations WHERE version=9')):
            # Sign-up time was never stored. Estimate it for existing accounts
            # from their earliest recorded activity, including rows an admin
            # reset moved into season_archives and admin actions on the
            # account; accounts that predate the multi-currency migration also
            # existed by their migration FX date.
            db.execute(text('ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ'))
            db.execute(text("""UPDATE users u SET created_at = COALESCE(LEAST(
                (SELECT min(created_at) FROM transactions WHERE user_id=u.id),
                (SELECT min(created_at) FROM fx_transactions WHERE user_id=u.id),
                (SELECT min(created_at) FROM popularity_events WHERE user_id=u.id),
                (SELECT min(created_at) FROM watchlists WHERE user_id=u.id),
                (SELECT min(created_at) FROM limit_orders WHERE user_id=u.id),
                (SELECT min(created_at) FROM wallet_transfers WHERE sender_id=u.id OR recipient_id=u.id),
                (SELECT min(created_at) FROM admin_audits WHERE target_id=u.id),
                (SELECT min(created_at) FROM season_archives WHERE user_id=u.id),
                (SELECT min((item->>'created_at')::timestamptz) FROM season_archives a,
                    jsonb_array_elements(COALESCE(a.data->'transactions','[]'::jsonb) || COALESCE(a.data->'fx_transactions','[]'::jsonb)
                        || COALESCE(a.data->'popularity_events','[]'::jsonb) || COALESCE(a.data->'watchlists','[]'::jsonb)
                        || COALESCE(a.data->'limit_orders','[]'::jsonb) || COALESCE(a.data->'wallet_transfers','[]'::jsonb)) item
                    WHERE a.user_id=u.id AND item ? 'created_at'),
                CASE WHEN u.baseline_note LIKE 'migration%' AND u.initial_fx_date IS NOT NULL THEN (u.initial_fx_date || ' 00:00:00+09')::timestamptz END
            ), now()) WHERE created_at IS NULL"""))
            db.execute(text('ALTER TABLE users ALTER COLUMN created_at SET DEFAULT now()'))
            db.execute(text('INSERT INTO schema_migrations(version) VALUES (9)'))

        if not db.scalar(text('SELECT 1 FROM schema_migrations WHERE version=10')):
            # Research metadata on fills; snapshot tables come from create_all.
            for column in ('order_requested_at TIMESTAMPTZ', 'market_session VARCHAR(16)', 'quote_source VARCHAR(40)',
                           'price_mode VARCHAR(24)', 'quote_stale BOOLEAN'):
                db.execute(text(f'ALTER TABLE transactions ADD COLUMN IF NOT EXISTS {column}'))
            db.execute(text('CREATE INDEX IF NOT EXISTS idx_performance_snapshots_date ON performance_snapshots(snapshot_date)'))
            db.execute(text('INSERT INTO schema_migrations(version) VALUES (10)'))

        if not db.scalar(text('SELECT 1 FROM schema_migrations WHERE version=11')):
            db.execute(text('ALTER TABLE transactions ADD COLUMN IF NOT EXISTS venue VARCHAR(12)'))
            db.execute(text('INSERT INTO schema_migrations(version) VALUES (11)'))

        if not db.scalar(text('SELECT 1 FROM schema_migrations WHERE version=12')):
            # The USD-basis return needs outside money in USD at the rate it came in.
            # Older grants were only kept in KRW; convert them at the account's starting rate.
            db.execute(text('ALTER TABLE users ADD COLUMN IF NOT EXISTS net_contributions_usd NUMERIC(24,4) NOT NULL DEFAULT 0'))
            db.execute(text("""UPDATE users SET net_contributions_usd = round(net_contributions_krw * initial_usd / initial_krw, 4)
                               WHERE net_contributions_krw <> 0 AND initial_krw > 0"""))
            db.execute(text('ALTER TABLE performance_snapshots ADD COLUMN IF NOT EXISTS initial_equity_usd NUMERIC(24,4)'))
            db.execute(text('ALTER TABLE performance_snapshots ADD COLUMN IF NOT EXISTS net_contributions_usd NUMERIC(24,4)'))
            # Only snapshots taken under the account's current baseline can borrow its USD principal.
            db.execute(text("""UPDATE performance_snapshots s SET initial_equity_usd = u.initial_usd,
                                   net_contributions_usd = round(s.net_contributions_krw * u.initial_usd / u.initial_krw, 4)
                               FROM users u WHERE s.user_id = u.id AND s.initial_equity_usd IS NULL
                                   AND u.initial_krw > 0 AND s.initial_equity_krw = u.initial_krw"""))
            db.execute(text('INSERT INTO schema_migrations(version) VALUES (12)'))

        if not db.scalar(text('SELECT 1 FROM schema_migrations WHERE version=13')):
            # KRW per USD at the fill, so each holding has a cost in both currencies.
            db.execute(text('ALTER TABLE transactions ADD COLUMN IF NOT EXISTS usd_krw NUMERIC(24,12)'))
            backfill_trade_rates(db)
            db.execute(text('INSERT INTO schema_migrations(version) VALUES (13)'))

        if not db.scalar(text('SELECT 1 FROM schema_migrations WHERE version=14')):
            # Reservation orders record when and at what price they filled.
            db.execute(text('ALTER TABLE limit_orders ADD COLUMN IF NOT EXISTS filled_at TIMESTAMPTZ'))
            db.execute(text('ALTER TABLE limit_orders ADD COLUMN IF NOT EXISTS filled_price NUMERIC(20,4)'))
            db.execute(text('INSERT INTO schema_migrations(version) VALUES (14)'))


def backfill_trade_rates(db):
    """Give older fills the reference rate the app itself was using then.

    Known daily rates come from the app's own records: daily snapshots, the
    account starting rates, and KRW fills (their fx_rate is USD per KRW). The
    ECB publishes day D's rate at about 14:00 UTC, so a fill at time t used
    the latest rate dated on or before (t - 14h). Fills older than every known
    rate take the earliest one."""
    from datetime import timedelta
    known = {}
    for day, rate in db.execute(text("""SELECT fx_date, fx_rate FROM performance_snapshots WHERE fx_date IS NOT NULL
                                        UNION ALL SELECT fx_date, 1/fx_rate FROM transactions WHERE currency='KRW' AND fx_rate > 0 AND fx_date IS NOT NULL
                                        UNION ALL SELECT initial_fx_date, initial_krw/initial_usd FROM users
                                            WHERE initial_fx_date IS NOT NULL AND initial_krw > 0 AND initial_usd > 0""")):
        known.setdefault(str(day), rate)
    if not known: return
    days = sorted(known)
    for trade_id, created_at in db.execute(text('SELECT id, created_at FROM transactions WHERE usd_krw IS NULL')).all():
        cutoff = str((created_at - timedelta(hours=14)).date())
        usable = [d for d in days if d <= cutoff]
        rate = known[usable[-1] if usable else days[0]]
        db.execute(text('UPDATE transactions SET usd_krw=:r WHERE id=:i'), {'r': rate, 'i': trade_id})
