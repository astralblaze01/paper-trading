"""Decide, at read time, whether a quote is a trade price for the current session.

Used for both markets: US sessions from us_session, Korean from kr_session.

A stored quote records facts: its trade timestamp, where it came from
(`origin` stream/rest), which sessions that source was verified to cover
(`valid_sessions`) and, for stream prices, the stream connection it arrived
on. This module turns those facts into session/price_mode/realtime/stale/
session_tradeable for *now*, so a price that was fine a minute ago stops
being tradeable the moment the session changes or the stream drops.

Freshness policy
- Stream price: realtime while the stream worker's heartbeat is younger than
  US_STREAM_MAX_AGE / KR_STREAM_MAX_AGE seconds and the symbol is still subscribed on the same
  connection. The last trade itself may be older (a quiet symbol): with the
  connection continuously up, no newer trade exists, so it is still the
  current price. Once the stream is unhealthy the price is judged like REST.
- REST price: tradeable while its trade time is at most US_MAX_QUOTE_AGE
  (Korea: MAX_QUOTE_AGE) old.
- Either way the trade time must fall in the current session, and the source
  must be verified for that session. A regular-session close is never an
  after-hours or overnight price.
"""
import os
import time
from datetime import datetime, timezone

from .us_session import clock_session
from . import instruments, kr_session

STREAM_MODE = {'overnight': 'overnight_stream', 'regular': 'trade_stream'}
REST_MODE = {'overnight': 'overnight_rest', 'regular': 'rest'}


def market_of(q):
    return instruments.market_of(str(q.get('symbol', '')))


def max_age(market):
    return int(os.getenv('MAX_QUOTE_AGE', '900') if market == 'KR' else os.getenv('US_MAX_QUOTE_AGE', '1800'))


def stream_healthy(stream, now=None, market='US'):
    now = time.time() if now is None else now
    if not stream or not stream.get('connected'):
        return False
    limit = os.getenv('KR_STREAM_MAX_AGE' if market == 'KR' else 'US_STREAM_MAX_AGE', '10')
    return now - float(stream.get('heartbeat') or 0) <= int(limit)


def trade_session(q):
    when = datetime.fromtimestamp(float(q['timestamp']), timezone.utc)
    return kr_session.clock_session(when) if market_of(q) == 'KR' else clock_session(when)


def assess(q, session, stream=None, now=None):
    """Return a copy of a quote with session state for `session` added.

    Quotes without `valid_sessions` come from test doubles or pre-upgrade
    caches; they keep their legacy behaviour and get no session fields."""
    if q.get('display_only'):
        return q | {'session': session, 'price_mode': 'cached', 'realtime': False,
                    'session_tradeable': False, 'tradeable': False, 'stale': True}
    if 'valid_sessions' not in q:
        return q
    now = time.time() if now is None else now
    q = dict(q)
    market = market_of(q)
    traded = trade_session(q)
    age = now - float(q['timestamp'])
    streaming = (q.get('origin') == 'stream' and stream_healthy(stream, now, market)
                 and q.get('stream_conn') == stream.get('conn')
                 and q['symbol'] in (stream.get('subscribed') or ()))
    q.update(market=market, session=session, trade_session=traded, realtime=False)
    if session in ('closed', 'unknown'):
        # Display only: the last real print, never a trade price.
        q.update(price_mode='cached', session_tradeable=False,
                 stale=bool(q.get('stale')) or age > max_age(market))
    elif traded != session or session not in q['valid_sessions']:
        q.update(price_mode='unavailable', session_tradeable=False, stale=True)
    elif streaming:
        q.update(price_mode=STREAM_MODE.get(session, 'extended_stream'), realtime=True,
                 session_tradeable=True, stale=False)
    else:
        fresh = age <= max_age(market)
        mode = REST_MODE.get(session, 'extended_rest') if q.get('origin') == 'rest' else 'cached'
        q.update(price_mode=mode, session_tradeable=fresh, stale=not fresh)
    q['tradeable'] = q['session_tradeable']
    return q


def session_price_mode(session, stream, rest_ok, market='US'):
    """Market-level price mode for the overview: what the best symbol can get."""
    if session in ('closed', 'unknown'):
        return 'cached'
    if stream_healthy(stream, market=market):
        return STREAM_MODE.get(session, 'extended_stream')
    if rest_ok:
        return REST_MODE.get(session, 'extended_rest')
    return 'unavailable'


def rejection(q):
    """User-facing reason a quote cannot fill an order now, or None."""
    if q.get('display_only'):
        return '복구한 참고 시세로는 주문할 수 없습니다. 새 시세를 기다려 주세요.'
    if q.get('session_tradeable', True):
        return None
    session, market = q.get('session'), q.get('market') or market_of(q)
    country = '한국' if market == 'KR' else '미국'
    names = {'overnight': '데이마켓', 'pre_market': '프리장', 'regular': '정규장', 'after_hours': '애프터장'}
    if session in names:
        return f'현재 {country} {names[session]} 체결 시세를 확인할 수 없어 주문할 수 없습니다.'
    return f'현재 {country} 주식시장이 장마감 상태이므로 주문할 수 없습니다.'
