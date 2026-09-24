"""Additive, idempotent migration: preserve existing USD accounts and trades."""
from sqlalchemy import text

def migrate(engine):
    with engine.begin() as db:
        db.execute(text('SELECT pg_advisory_xact_lock(74923101)'))
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
