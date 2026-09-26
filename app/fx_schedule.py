"""ECB daily reference publication windows, using the TARGET calendar."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

FRANKFURT = ZoneInfo('Europe/Berlin')


def easter(year):
    # Gregorian computus; TARGET closes on Good Friday and Easter Monday.
    from datetime import date
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19*a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2*e + 2*i - h - k) % 7
    m = (a + 11*h + 22*l) // 451
    n = h + l - 7*m + 114
    return date(year, n // 31, n % 31 + 1)


def publication_day(day):
    return (day.weekday() < 5 and (day.month, day.day) not in ((1, 1), (5, 1), (12, 25), (12, 26))
            and day not in (easter(day.year)-timedelta(days=2), easter(day.year)+timedelta(days=1)))


def refresh_seconds(rate_day, now=None):
    """Retry a missing publication in 30 minutes; otherwise wait until 16:15 local.

    A buffer after the usual 16:00 publication allows the distributor to update.
    The reference date, not the HTTP fetch time, decides whether data is new.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(FRANKFURT)
    due = now.replace(hour=16, minute=15, second=0, microsecond=0)
    while due > now or not publication_day(due.date()):
        due -= timedelta(days=1)
    if rate_day < due.date():
        return 1800
    upcoming = due + timedelta(days=1)
    while not publication_day(upcoming.date()):
        upcoming += timedelta(days=1)
    return max(60, int((upcoming.astimezone(timezone.utc)-now.astimezone(timezone.utc)).total_seconds()))
