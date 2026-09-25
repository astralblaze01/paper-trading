"""Canonical US equity sessions, computed in America/New_York wall-clock time.

Session boundaries are defined in New York time so that US daylight saving
changes move them automatically; Korean clock hours are never used. The clock
only names the session. Whether a session can trade is decided separately from
the quotes actually received for it (see quote_policy).
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo('America/New_York')
SESSIONS = ('overnight', 'pre_market', 'regular', 'after_hours', 'closed', 'unknown')
LABELS = {'overnight': '데이마켓', 'pre_market': '프리장', 'regular': '정규장',
          'after_hours': '애프터장', 'closed': '장마감', 'unknown': '장 상태 확인 불가'}
EXTENDED = {'pre_market', 'after_hours'}
_FINNHUB = {'pre-market': 'pre_market', 'regular': 'regular', 'post-market': 'after_hours'}


def clock_session(now=None):
    """Session from the New York clock alone (no holidays or early closes).

    Overnight runs from 20:00 to 04:00 on the nights before a weekday, i.e.
    Sunday evening through Friday 04:00."""
    local = (now or datetime.now(timezone.utc)).astimezone(NEW_YORK)
    minutes, weekday = local.hour * 60 + local.minute, local.weekday()
    if minutes >= 20 * 60:
        return 'overnight' if weekday in (6, 0, 1, 2, 3) else 'closed'
    if minutes < 4 * 60:
        return 'overnight' if weekday <= 4 else 'closed'
    if weekday >= 5:
        return 'closed'
    if minutes < 9 * 60 + 30:
        return 'pre_market'
    return 'regular' if minutes < 16 * 60 else 'after_hours'


def resolve_session(status=None, now=None):
    """Combine the clock with Finnhub's /stock/market-status response.

    Finnhub knows holidays and early closes for the day sessions but has no
    overnight session, so the overnight window always comes from the clock."""
    clock = clock_session(now)
    if not status:
        return clock
    local = (now or datetime.now(timezone.utc)).astimezone(NEW_YORK)
    if clock == 'overnight':
        # 00:00-04:00 on a holiday belongs to a trading day that does not exist.
        return 'closed' if status.get('holiday') and local.hour < 4 else 'overnight'
    if status.get('holiday'):
        return 'closed'
    return _FINNHUB.get(status.get('session'), 'closed' if clock != 'closed' else clock)
