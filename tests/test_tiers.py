"""Tiers by ranking share, the morning rank for the daily ▲/▼, and profit amounts in the performance series."""
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from sqlalchemy import select

from test_service import database, client, register  # noqa: F401  (shared fixtures)
from app import main
from app.db import Session, User, PerformanceSnapshot
from app.kr_session import SEOUL
from app.performance_snapshots import series
from app.tiers import tier_for, cutoffs


def test_tier_shares():
    assert Counter(tier_for(r, 1) for r in range(1, 2)) == {'grandmaster': 1}
    assert [tier_for(r, 10) for r in range(1, 11)] == ['grandmaster', 'master', 'diamond', 'platinum', 'gold',
                                                       'silver', 'silver', 'silver', 'bronze', 'bronze']
    assert Counter(tier_for(r, 100) for r in range(1, 101)) == {'grandmaster': 1, 'master': 1, 'diamond': 3, 'platinum': 15, 'gold': 25, 'silver': 30, 'bronze': 25}
    assert cutoffs(3) == [('grandmaster', 1), ('master', 2), ('diamond', 3), ('platinum', 3), ('gold', 3), ('silver', 3), ('bronze', 3)]


def snap(uid, day, equity_usd, initial_usd=100000, contributions_usd=0):
    with Session.begin() as db:
        db.add(PerformanceSnapshot(user_id=uid, snapshot_date=day, snapshot_at=datetime.now(timezone.utc), scheduled_for=datetime.now(timezone.utc),
                                   equity_krw=D(equity_usd) * 1000, equity_usd=D(equity_usd), cash_krw=0, cash_usd=0,
                                   net_contributions_krw=D(contributions_usd) * 1000, net_contributions_usd=D(contributions_usd),
                                   initial_equity_krw=D(initial_usd) * 1000, initial_equity_usd=D(initial_usd), cumulative_return_pct=0,
                                   fx_rate=D(1000), fx_date='2026-09-24', positions={}, quote_metadata={}, quality={}, stale=False,
                                   baseline={'initial_equity_krw': str(D(initial_usd) * 1000), 'performance_since': None}, created_at=datetime.now(timezone.utc)))


def test_ranking_rows_carry_tier_and_morning_rank(client):
    main._ranking_cache.clear()
    for name in ('alice', 'bob', 'carol'): register(client, name)
    with Session() as db: ids = {u.username: u.id for u in db.scalars(select(User))}
    today = datetime.now(timezone.utc).astimezone(SEOUL).date()
    # This morning carol led and alice was last; now all three hold $100,000 and rank by name.
    snap(ids['carol'], today, 120000); snap(ids['bob'], today, 110000); snap(ids['alice'], today, 90000)
    snap(ids['alice'], today - timedelta(1), 200000)          # an older day is ignored
    rows = client.get('/api/ranking').json()['rows']
    assert [(r['username'], r['rank'], r['previous_rank'], r['tier']) for r in rows] == [
        ('alice', 1, 3, 'grandmaster'), ('bob', 2, 2, 'master'), ('carol', 3, 1, 'diamond')]


def test_series_has_profit_amounts_per_basis():
    with Session.begin() as db:
        u = User(username='pnl', password_hash='x'); db.add(u); db.flush(); uid = u.id
    day = datetime(2026, 9, 1).date()
    snap(uid, day, 100000); snap(uid, day + timedelta(1), 106000, contributions_usd=5000)
    points = series(uid, day, day + timedelta(1))['snapshots']
    assert [(p['pnl_krw'], p['pnl_usd']) for p in points] == [(0, 0), (1000000, 1000)]
