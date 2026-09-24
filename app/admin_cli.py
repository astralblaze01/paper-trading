"""Explicit local promotion only: python -m app.admin_cli USERNAME."""
import sys
from sqlalchemy import select
from .db import Session, User
if __name__=='__main__':
    if len(sys.argv)!=2: raise SystemExit('Usage: python -m app.admin_cli EXISTING_USERNAME')
    with Session.begin() as db:
        u=db.scalar(select(User).where(User.username==sys.argv[1]).with_for_update())
        if not u: raise SystemExit('User not found; register first')
        u.is_admin=True
        print('Admin enabled for existing user:',u.username)
