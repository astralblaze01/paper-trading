"""Run only on a restored production backup in the dedicated migration database."""
import os
import sys
sys.path.insert(0,"/srv")
from sqlalchemy import URL,create_engine,text
url=URL.create('postgresql+psycopg',username='paper',password=os.environ['DB_PASSWORD'],host='db',database='paper_migration_test')
os.environ['DATABASE_URL']=url.render_as_string(hide_password=False)
from app.db import engine,Base
from app.migrations import migrate
assert engine.url.database=='paper_migration_test'
with engine.connect() as db:
    before=db.execute(text('SELECT id,username,password_hash,cash FROM users ORDER BY id')).all()
    trades=db.execute(text('SELECT id,user_id,symbol,quantity,price FROM transactions ORDER BY id')).all()
    positions=db.execute(text('SELECT user_id,symbol,quantity,average_cost FROM positions ORDER BY user_id,symbol')).all()
Base.metadata.create_all(engine)
migrate(engine)
migrate(engine)
with engine.connect() as db:
    assert before==db.execute(text('SELECT id,username,password_hash,cash FROM users ORDER BY id')).all()
    assert trades==db.execute(text('SELECT id,user_id,symbol,quantity,price FROM transactions ORDER BY id')).all()
    assert positions==db.execute(text('SELECT user_id,symbol,quantity,average_cost FROM positions ORDER BY user_id,symbol')).all()
    for uid,_,_,cash in before:
        assert db.scalar(text("SELECT balance FROM wallets WHERE user_id=:u AND currency='USD'"),{'u':uid})==cash
        assert db.scalar(text("SELECT balance FROM wallets WHERE user_id=:u AND currency='KRW'"),{'u':uid})==0
    assert db.scalar(text('SELECT count(*) FROM schema_migrations WHERE version=2'))==1
    assert db.scalar(text('SELECT count(*) FROM schema_migrations WHERE version=3'))==1
print(f'Migration verified twice: {len(before)} users, {len(trades)} trades, {len(positions)} positions, passwords and USD balances preserved.')
