"""Korean REST quotes: unified KRX+NXT prints and per-symbol session capability.

Verified with live KIS responses (scripts/check_kr_sessions.py):
- FID_COND_MRKT_DIV_CODE 'J' = KRX, 'NX' = NXT, 'UN' = unified. For an
  NXT-listed symbol unified volume is KRX + NXT, so 'UN' is its source. For a
  symbol not on NXT, 'UN' misses KRX after-market prints (카카오 on 2026-09-23:
  KRX 18:30 bar 108 shares, unified none), so its source is 'J' (KRX).
- A symbol not listed on NXT answers 'NX' with 기준가 (stck_sdpr) 0.
- Minute bars include filler bars with volume 0 that repeat the last price;
  only a bar with volume is a trade, and its minute start is the trade time.
- ETF/ETN do not trade in either after-market (and are not on NXT).
"""
import os
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation

from .market import MarketError
from .redis_cache import redis_cache
from .kr_session import SEOUL

PATH = '/uapi/domestic-stock/v1/quotations/'
ETP_WORDS = ('ETF', 'ETN', 'ELW')


def capability(kis, symbol):
    """{'nxt': bool, 'etp': bool, 'known': bool}; unknown is treated as least capable."""
    key = f'market:kr-capability:{symbol}'
    cached = redis_cache.get_json(key)
    if cached:
        return cached
    from .instruments import instrument
    code = symbol[3:]
    try:
        krx = kis.get(PATH + 'inquire-price', 'FHKST01010100', {'FID_COND_MRKT_DIV_CODE': 'J', 'FID_INPUT_ISCD': code}, 3600).get('output') or {}
        nxt = kis.get(PATH + 'inquire-price', 'FHKST01010100', {'FID_COND_MRKT_DIV_CODE': 'NX', 'FID_INPUT_ISCD': code}, 3600).get('output') or {}
        names = f"{krx.get('rprs_mrkt_kor_name', '')} {krx.get('bstp_kor_isnm', '')}"
        result = {'nxt': Decimal(str(nxt.get('stck_sdpr') or 0)) > 0,
                  'etp': any(w in names for w in ETP_WORDS) or instrument(symbol)['category'] in ('kr_bond', 'gold'),
                  'known': True}
    except (MarketError, InvalidOperation, TypeError):
        return {'nxt': False, 'etp': True, 'known': False}
    redis_cache.set_json(key, result, 86400)
    return result


def valid_sessions(cap):
    """Sessions in which a unified print of this symbol may fill an order."""
    sessions = ['regular']
    if cap.get('nxt'):
        sessions.insert(0, 'pre_market')      # only NXT trades before the open
    if cap.get('nxt') or not cap.get('etp', True):
        sessions.append('after_hours')        # NXT, or KRX after-market for non-ETP
    return sessions


def _stamp(bar):
    return datetime.strptime(bar['stck_bsop_date'] + bar['stck_cntg_hour'], '%Y%m%d%H%M%S').replace(tzinfo=SEOUL).timestamp()


def source_for(cap):
    """(market code, venue): unified for NXT-listed symbols, KRX otherwise."""
    return ('UN', 'UNIFIED') if cap.get('nxt') else ('J', 'KRX')


def unified_quote(kis, symbol, now=None):
    now = now or datetime.now(SEOUL)
    cap = capability(kis, symbol)
    code, venue = source_for(cap)
    def load(hour, ttl):
        return kis.get(PATH + 'inquire-time-itemchartprice', 'FHKST03010200',
                       {'FID_COND_MRKT_DIV_CODE': code, 'FID_INPUT_ISCD': symbol[3:], 'FID_INPUT_HOUR_1': hour,
                        'FID_PW_DATA_INCU_YN': 'Y', 'FID_ETC_CLS_CODE': ''}, ttl)
    # Rounded minute makes requests from different browsers share one cache entry.
    d = load(now.astimezone(SEOUL).strftime('%H%M') + '00', int(os.getenv('QUOTE_TTL', '15')))
    today = now.astimezone(SEOUL).strftime('%Y%m%d')
    if all(b.get('stck_bsop_date') != today for b in d.get('output2') or []):
        # Before today's first print (holiday, pre-open) KIS answers with the
        # previous trading day's bars up to the same clock time, not that day's
        # last trade. Ask for the end of that day instead.
        d = load('200000', 300)
    try:
        bars = [b for b in d['output2'] if b.get('stck_bsop_date')]
        traded = [b for b in bars if Decimal(str(b.get('cntg_vol') or 0)) > 0]
        info = d.get('output1') or {}
        if traded:
            bar = max(traded, key=lambda b: b['stck_bsop_date'] + b['stck_cntg_hour'])
            stamp, sessions = _stamp(bar), valid_sessions(cap)
        else:
            # No trade in the returned window: the last trade is older than the
            # earliest bar. Show the price, never trade on it.
            bar = min(bars, key=lambda b: b['stck_bsop_date'] + b['stck_cntg_hour'])
            stamp, sessions = _stamp(bar), []
        price = Decimal(str(bar['stck_prpr'])).quantize(Decimal('.0001'))
        if not price.is_finite() or not 0 < price <= 1000000000:
            raise ValueError()
        change = pct = None
        current, diff = info.get('stck_prpr'), info.get('prdy_vrss')
        if current not in (None, '') and diff not in (None, ''):
            base = Decimal(str(current)) - Decimal(str(diff))
            if base > 0:
                change, pct = price - base, ((price - base) / base * 100).quantize(Decimal('.01'))
    except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
        raise MarketError('국내 시세를 확인할 수 없습니다.') from exc
    return {'symbol': symbol, 'price': price, 'timestamp': int(stamp),
            'stale': not traded or time.time() - stamp > int(os.getenv('MAX_QUOTE_AGE', '900')),
            'name': info.get('hts_kor_isnm', symbol), 'change': change, 'change_pct': pct,
            'high': info.get('stck_hgpr'), 'low': info.get('stck_lwpr'), 'volume': info.get('acml_vol'),
            'turnover': info.get('acml_tr_pbmn'), 'market': 'KR', 'venue': venue, 'source': 'KIS',
            'origin': 'rest', 'valid_sessions': sessions,
            'data_status': f"KIS {'통합(KRX+NXT)' if venue == 'UNIFIED' else 'KRX'} 1분봉 · 실시간 체결 스트림 아님"}
