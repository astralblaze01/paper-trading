"""US REST quotes chosen by session; every result names the sessions its source covers.

Verified against live responses (scripts/check_us_day_market.py):
- Finnhub /quote: regular-session prints only. Outside regular hours it keeps
  returning the 16:00 close, so it is valid for 'regular' alone.
- KIS 1-minute bars on the primary exchange (NAS/NYS/AMS): the response states
  a 04:00-20:00 ET window and carries after-hours prints, so they cover
  pre-market, regular and after-hours.
- KIS 1-minute bars on the day-market venue (BAQ/BAY/BAA): 20:00-04:00 ET
  overnight prints only.
- KIS current-price (HHDFS00000300) has no trade time and on NAS returns the
  regular close during after-hours, so it only supplies the change base.

Bars are timestamped with the start of their minute (full date and time), so
a bar never looks fresher than the trade it contains.
"""
import os
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation

from .market import MarketError
from .redis_cache import redis_cache
from .us_session import NEW_YORK

PATH = '/uapi/overseas-price/v1/quotations/'
PRIMARY = ('NAS', 'NYS', 'AMS')
DAY_VENUE = {'NAS': 'BAQ', 'NYS': 'BAY', 'AMS': 'BAA'}
PRIMARY_SESSIONS = ['pre_market', 'regular', 'after_hours']


class NoSessionData(MarketError):
    """The source works but has no print for this symbol (symbol-level, not an outage)."""


def record_health(name, ok, error=None):
    redis_cache.set_json(f'market:rest-health:{name}', {'ok': ok, 'at': time.time(), 'error': error}, 900)


def rest_health(name, now=None, window=300):
    value = redis_cache.get_json(f'market:rest-health:{name}') or {}
    return bool(value.get('ok')) and (now or time.time()) - float(value.get('at') or 0) <= window, value


class USQuotes:
    def __init__(self, finnhub, kis):
        self.finnhub, self.kis = finnhub, kis

    def exchange(self, symbol):
        """Primary exchange from the KIS master, or probed once and remembered."""
        key = f'market:us-exchange:{symbol}'
        known = redis_cache.get_json(key)
        if known in PRIMARY:
            return known
        from .us_symbols import _rows
        for row in _rows():
            if row.get('symbol') == symbol and row.get('exchange') in PRIMARY:
                redis_cache.set_json(key, row['exchange'], 7 * 86400)
                return row['exchange']
        for code in PRIMARY:
            data = self.kis.get(PATH + 'price', 'HHDFS00000300', {'AUTH': '', 'EXCD': code, 'SYMB': symbol}, 86400)
            try:
                if Decimal(str((data.get('output') or {}).get('last') or 0)) > 0:
                    redis_cache.set_json(key, code, 7 * 86400)
                    return code
            except InvalidOperation:
                continue
        raise NoSessionData('KIS에서 미국 종목 거래소를 확인할 수 없습니다.')

    def finnhub_quote(self, symbol):
        q = self.finnhub.quote(symbol)
        return q | {'source': 'Finnhub', 'origin': 'rest', 'venue': 'primary', 'valid_sessions': ['regular']}

    def kis_bars(self, symbol, overnight):
        primary = self.exchange(symbol)
        code = DAY_VENUE[primary] if overnight else primary
        data = self.kis.get(PATH + 'inquire-time-itemchartprice', 'HHDFS76950200',
                            {'AUTH': '', 'EXCD': code, 'SYMB': symbol, 'NMIN': '1', 'PINC': '1', 'NEXT': '',
                             'NREC': '1', 'FILL': '', 'KEYB': ''}, int(os.getenv('QUOTE_TTL', '15')))
        window = data.get('output1') or {}
        try:
            bar = max(data.get('output2') or [], key=lambda b: b['xymd'] + b['xhms'])
            stamp = datetime.strptime(bar['xymd'] + bar['xhms'], '%Y%m%d%H%M%S').replace(tzinfo=NEW_YORK).timestamp()
            price = Decimal(str(bar['last'])).quantize(Decimal('.0001'))
            if not price.is_finite() or price <= 0:
                raise ValueError()
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise NoSessionData(f'KIS {code} 체결 데이터가 없습니다.') from exc
        change = pct = None
        try:
            base = Decimal(str((self.kis.get(PATH + 'price', 'HHDFS00000300', {'AUTH': '', 'EXCD': code, 'SYMB': symbol}, 300)
                                .get('output') or {}).get('base')))
            if base.is_finite() and base > 0:
                change = price - base
                pct = (change / base * 100).quantize(Decimal('.01'))
        except (MarketError, InvalidOperation, TypeError):
            pass  # The change is display-only; the trade price stands without it.
        return {'symbol': symbol, 'price': price, 'timestamp': int(stamp), 'stale': False,
                'change': change, 'change_pct': pct, 'high': None, 'low': None, 'volume': None,
                'source': 'KIS', 'origin': 'rest', 'venue': 'overnight' if overnight else 'primary',
                'exchange': code, 'valid_sessions': ['overnight'] if overnight else PRIMARY_SESSIONS,
                'kis_window': f"{window.get('stim', '')}-{window.get('etim', '')} ET",
                'data_status': f"KIS {code} {'데이마켓' if overnight else '시간외 포함'} 1분봉 · 실시간 체결 스트림 아님"}

    def quote(self, symbol, session):
        kis = self.kis is not None and self.kis.configured
        if session == 'regular':
            try:
                q = self.finnhub_quote(symbol)
                record_health('finnhub', True)
                return q
            except MarketError as exc:
                record_health('finnhub', False, type(exc).__name__)
                if not kis:
                    raise
            return self._kis(symbol, False, 'kis_primary')
        if kis and session in ('overnight', 'pre_market', 'after_hours'):
            try:
                return self._kis(symbol, session == 'overnight', 'kis_overnight' if session == 'overnight' else 'kis_primary')
            except MarketError:
                pass
        # Display fallback. assess() never lets it fill an order off-session.
        return self.finnhub_quote(symbol)

    def _kis(self, symbol, overnight, name):
        try:
            q = self.kis_bars(symbol, overnight)
        except NoSessionData:
            record_health(name, True)
            raise
        except MarketError as exc:
            record_health(name, False, type(exc).__name__)
            raise
        record_health(name, True)
        return q


def diagnostics(market):
    """Admin-only view of the US price pipeline. Contains no credentials."""
    from .quote_policy import stream_healthy
    now = time.time()
    stream = redis_cache.stream_status() or {}
    provider = (getattr(market, 'providers', {}) or {}).get('US')
    status = provider.market_status() if provider and hasattr(provider, 'market_status') else {}
    last = stream.get('last_message')
    return {'session': status.get('session'), 'label': status.get('label'), 'price_mode': status.get('price_mode'),
            'stream': {'state': stream.get('state', 'no_worker'), 'healthy': stream_healthy(stream, now),
                       'last_message_age': round(now - last, 1) if last else None,
                       'subscribed': stream.get('subscribed', []), 'limit': stream.get('limit'),
                       'queued': stream.get('queued', []), 'reconnects': stream.get('reconnects', 0),
                       'last_error': stream.get('last_error')},
            'rest': {name: rest_health(name, now)[1] or None for name in ('finnhub', 'kis_primary', 'kis_overnight')}}
