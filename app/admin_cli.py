"""Explicit local account roles.

    python -m app.admin_cli USERNAME        # make an existing account an administrator
    python -m app.admin_cli --ai USERNAME   # mark an existing account as an AI trader
"""
import sys
from sqlalchemy import select
from .db import Session, User
if __name__=='__main__':
    args=sys.argv[1:]
    ai=args[:1]==['--ai']
    if ai: args=args[1:]
    if len(args)!=1: raise SystemExit('Usage: python -m app.admin_cli [--ai] EXISTING_USERNAME')
    with Session.begin() as db:
        u=db.scalar(select(User).where(User.username==args[0]).with_for_update())
        if not u: raise SystemExit('User not found; register first')
        if ai:
            if u.is_admin: raise SystemExit('An administrator cannot be an AI trader')
            u.is_ai=True
            print('AI trader enabled for existing user:',u.username)
        else:
            if u.is_ai: raise SystemExit('An AI trader cannot be an administrator')
            u.is_admin=True
            print('Admin enabled for existing user:',u.username)
