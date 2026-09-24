"""Shared quote validation/freshness; never performs provider or cache I/O."""
import os
import time
from decimal import Decimal, InvalidOperation
from .instruments import valid_symbol

DECIMALS = ('price', 'native_price', 'fx_rate', 'change', 'change_pct', 'high', 'low', 'turnover')
PUBLIC = ('symbol', 'price', 'native_price', 'currency', 'timestamp', 'stale', 'change',
          'change_pct', 'high', 'low', 'volume', 'turnover', 'source', 'data_status',
          'fx_rate', 'fx_date', 'name', 'category', 'cached_at')


def max_age(symbol):
    return int(os.getenv('MAX_QUOTE_AGE', '900') if symbol.startswith('KR:') else os.getenv('US_MAX_QUOTE_AGE', '1800'))


def normalize_quote(symbol, value, now=None):
    now = time.time() if now is None else now
    if not valid_symbol(symbol) or not isinstance(value, dict):
        raise ValueError('invalid quote')
    q = dict(value)
    if q.get('symbol', symbol) != symbol:
        raise ValueError('quote symbol mismatch')
    q['symbol'] = symbol
    stamp = float(q['timestamp'])
    if not 0 < stamp <= now + 60:
        raise ValueError('invalid quote timestamp')
    expected = 'KRW' if symbol.startswith('KR:') else 'USD'
    if q.get('currency') != expected:
        raise ValueError('invalid quote currency')
    try:
        for field in DECIMALS:
            if q.get(field) is not None:
                q[field] = Decimal(str(q[field]))
                if not q[field].is_finite():
                    raise ValueError('invalid quote number')
        if q['price'] <= 0 or q['native_price'] <= 0:
            raise ValueError('invalid quote price')
    except (InvalidOperation, KeyError, TypeError) as exc:
        raise ValueError('invalid quote number') from exc
    q['stale'] = bool(q.get('stale')) or now - stamp > max_age(symbol)
    q['cached_at'] = q.get('_cached_at', q.get('cached_at'))
    return q


def public_quote(symbol, value):
    q = normalize_quote(symbol, value)
    return {key: str(q[key]) if isinstance(q[key], Decimal) else q[key]
            for key in PUBLIC if key in q}
