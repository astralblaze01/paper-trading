"""Tiers by place in the asset ranking, and each account's rank at the day's baseline.

A tier is the share of ranked accounts at or above a place. Each cut-off is
rounded up and lies at least one place below the tier above it (never past the
last place), so the top tiers are not empty in a small field: with 10 accounts,
1st is 그랜드마스터, 2nd 마스터, 3rd 다이아몬드, 4th 플래티넘, 5th 골드, 6th-8th 실버
and 9th-10th 브론즈. With 100 accounts: 1 그랜드마스터, 1 마스터, 3 다이아몬드,
15 플래티넘, 25 골드, 30 실버, 25 브론즈.

The daily rank change compares today's place with the place in the latest daily
performance snapshot (taken each morning, Korea time), ranked the same way:
USD equity, highest first, then username.
"""
from math import ceil

from sqlalchemy import func, select

from .db import PerformanceSnapshot

# (key, label, cumulative share of ranked accounts)
TIERS = (('grandmaster', '그랜드마스터', 0.01), ('master', '마스터', 0.02), ('diamond', '다이아몬드', 0.05), ('platinum', '플래티넘', 0.20), ('gold', '골드', 0.45),
         ('silver', '실버', 0.75), ('bronze', '브론즈', 1.0))


def cutoffs(total):
    """The last place of each tier among `total` ranked accounts."""
    last, result = 0, []
    for key, _, share in TIERS:
        last = min(total, max(ceil(total * share), last + 1))
        result.append((key, last))
    return result


def tier_for(rank, total):
    """The tier of 1-based `rank` among `total` ranked accounts."""
    for key, last in cutoffs(total):
        if rank <= last:
            return key
    return TIERS[-1][0]


def previous_ranks(db, user_ids, today):
    """{user_id: rank} in the latest snapshot on or before `today`, among `user_ids`, and that date."""
    ids = list(user_ids)
    if not ids:
        return {}, None
    day = db.scalar(select(func.max(PerformanceSnapshot.snapshot_date)).where(
        PerformanceSnapshot.snapshot_date <= today, PerformanceSnapshot.user_id.in_(ids)))
    if day is None:
        return {}, None
    from .db import User
    rows = db.execute(select(PerformanceSnapshot.user_id, PerformanceSnapshot.equity_usd, User.username)
                      .join(User, User.id == PerformanceSnapshot.user_id)
                      .where(PerformanceSnapshot.snapshot_date == day, PerformanceSnapshot.user_id.in_(ids))).all()
    ordered = sorted(rows, key=lambda r: (-r.equity_usd, r.username))
    return {r.user_id: i + 1 for i, r in enumerate(ordered)}, day
