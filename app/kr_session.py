"""Canonical Korean sessions (Asia/Seoul) across KRX and NXT.

Standard timetable, checked against the 2026-09-23 KIS minute bars:
- 08:00-08:50 pre_market   NXT pre-market only (KRX has no pre-market prints)
- 09:00-15:30 regular      KRX; NXT main market 09:00:30-15:20
- 15:30-15:40 closed       break between the sessions
- 15:40-20:00 after_hours  NXT after-market from 15:40, KRX after-market
                           16:00-20:00 (continuous matching since 2026-09-14).
                           ETF/ETN are excluded on both venues.
Trading days come from the KIS holiday API. Intraday hours are this timetable
(special opening days are not published through the API used here), so
`schedule_verified` is always False. The clock only names the session;
whether a symbol can trade is decided from actual prints (quote_policy).
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SEOUL = ZoneInfo('Asia/Seoul')
LABELS = {'pre_market': '프리장', 'regular': '정규장', 'after_hours': '애프터장',
          'closed': '장마감', 'unknown': '장 상태 확인 불가'}
OPEN = ('pre_market', 'regular', 'after_hours')


def clock_session(now=None, trading_day=True):
    local = (now or datetime.now(timezone.utc)).astimezone(SEOUL)
    if not trading_day or local.weekday() >= 5:
        return 'closed'
    minutes = local.hour * 60 + local.minute
    if 8 * 60 <= minutes < 8 * 60 + 50:
        return 'pre_market'
    if 9 * 60 <= minutes < 15 * 60 + 30:
        return 'regular'
    if 15 * 60 + 40 <= minutes < 20 * 60:
        return 'after_hours'
    return 'closed'


def venues(session, now=None):
    """Venues whose timetable is open, for display. Prices are UNIFIED regardless."""
    local = (now or datetime.now(timezone.utc)).astimezone(SEOUL)
    if session == 'pre_market':
        return ['NXT']
    if session == 'regular':
        return ['KRX', 'NXT']
    if session == 'after_hours':
        return ['NXT', 'KRX'] if local.hour >= 16 else ['NXT']
    return []
