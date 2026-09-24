"""Create isolated test DB without exposing the database password in argv."""
import os
import subprocess
from sqlalchemy import create_engine, URL, text
url=URL.create('postgresql+psycopg',username='paper',password=os.environ['DB_PASSWORD'],host=os.getenv('DB_HOST','db'),database='postgres')
engine=create_engine(url,isolation_level='AUTOCOMMIT')
with engine.connect() as db:
    if not db.scalar(text("SELECT 1 FROM pg_database WHERE datname = 'paper_test'")):
        db.execute(text('CREATE DATABASE paper_test'))
env=dict(os.environ,DATABASE_URL=url.set(database='paper_test').render_as_string(hide_password=False),REDIS_URL='',MARKET_CACHE_MODE='direct')
raise SystemExit(subprocess.call(['python','-m','pytest','-q','-p','no:cacheprovider','tests'],env=env))
