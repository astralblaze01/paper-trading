import os
import time
import logging
from pathlib import Path

from .instruments import valid_symbol
from .multi_market import MultiMarket
from .redis_cache import redis_cache, trade_key, price_key
from .quote_data import normalize_quote
from .finnhub_stream import day_fields
from .logging_config import configure_logging
from .kr_symbols import refresh_master
from .us_symbols import refresh_master as refresh_us_master

configure_logging()
log=logging.getLogger('market-worker')
HEARTBEAT = Path('/tmp/market-worker-heartbeat')  # compose healthcheck
MASTER_REFRESH = 86400  # seconds between symbol-master downloads
MASTER_RETRY = 3600     # a failed download is tried again after this instead


CLOSED_RETRY = 300  # seconds before a symbol that failed while its market was closed is tried again
# A symbol the Finnhub stream delivers live needs REST only for its day change, high
# and low; Finnhub REST allows 50 calls a minute for every symbol together.
STREAMED_REFRESH = 300


def finnhub_streamed():
    """Symbols the Finnhub stream holds right now, or none while it is unhealthy."""
    from .quote_policy import stream_healthy
    status = redis_cache.finnhub_stream_status()
    return set(status.get('subscribed') or ()) if stream_healthy(status) else set()


def collect_quote(market, symbol, snapshot_ttl):
    quote = market.quote_direct(symbol)
    quote['_cached_at'] = time.time()
    trade = redis_cache.get_json(trade_key(symbol))
    if trade and float(trade.get('timestamp', 0)) > float(quote['timestamp']):
        # A Finnhub trade carries only its price; the REST answer has the day's change.
        quote = day_fields(trade, quote) if trade.get('stream') == 'finnhub' else trade
    if redis_cache.store_quote(symbol, quote, snapshot_ttl):
        state = 'stored'
    else:
        old = redis_cache.get_json(price_key(symbol))
        # An older provider response is expected after changing sessions. It
        # must not overwrite the last price, nor count as a collection outage.
        if not old or float(old['timestamp']) <= float(quote['timestamp']):
            raise ValueError('quote store rejected')
        normalize_quote(symbol, old)
        state = 'retained_newer'
    redis_cache.set_json(f'market:collection:{symbol}',
                         {'state': state, 'checked_at': time.time()}, 7*86400)
    return quote, state


def main():
    os.environ['MARKET_WORKER_MODE'] = 'true'
    market = MultiMarket()
    interval = max(5, int(os.getenv('QUOTE_TTL', '15')))
    snapshot_ttl, retry_delay = max(120, interval * 8), max(30, interval * 2)
    refreshed = {}
    retry_after = {}
    attempted = {}
    # None until the first download: monotonic() counts from host boot, so a 0
    # sentinel would skip the startup refresh for a host up less than a day.
    master_refreshed=None
    try:
        while True:
            HEARTBEAT.touch()
            if master_refreshed is None or time.monotonic()-master_refreshed>MASTER_REFRESH:
                try:
                    count=refresh_master();master_refreshed=time.monotonic();log.info(f'Korean symbol master refreshed ({count} symbols)')
                except Exception as exc:
                    master_refreshed=time.monotonic()-(MASTER_REFRESH-MASTER_RETRY)
                    log.warning('Korean symbol master refresh failed',extra={'status_code':type(exc).__name__})
                try:
                    count=refresh_us_master();log.info(f'US symbol master refreshed ({count} symbols)')
                except Exception as exc:
                    master_refreshed=time.monotonic()-(MASTER_REFRESH-MASTER_RETRY)
                    log.warning('US symbol master refresh failed',extra={'status_code':type(exc).__name__})
            urgent = redis_cache.next_refresh(timeout=1)
            symbols = ([urgent] if urgent else []) + redis_cache.requested_symbols()
            streamed = finnhub_streamed()
            now = time.monotonic()
            pending = sorted(dict.fromkeys(symbols), key=lambda s: (refreshed.get(s, 0), attempted.get(s, 0)))
            while True:
                # A pass can take tens of seconds (KIS overseas allows one call per 1.1 s), so a
                # symbol someone just opened is taken before each kept-fresh one, not after the pass.
                symbol = redis_cache.pop_refresh() or (pending.pop(0) if pending else None)
                if symbol is None:
                    break
                if not symbol or not valid_symbol(symbol):
                    continue
                if now < retry_after.get(symbol, 0):
                    continue
                if time.monotonic() - refreshed.get(symbol, 0) < (STREAMED_REFRESH if symbol in streamed else interval):
                    continue
                attempted[symbol] = time.monotonic()
                try:
                    if (refreshed.get(symbol) and time.monotonic()-refreshed[symbol] < 600
                            and market.providers['KR' if symbol.startswith('KR:') else 'US'].session() == 'closed'):
                        continue
                    quote, state = collect_quote(market, symbol, snapshot_ttl)
                    refreshed[symbol] = time.monotonic()
                    retry_after.pop(symbol, None)
                    log.info('quote %s; quote_age_seconds=%s', state, round(time.time()-quote['timestamp'], 2), extra={'path': symbol})
                except Exception as exc:
                    # Do not leak provider credentials or response bodies.
                    # A closed market keeps failing until it opens (KIS answers errors before the
                    # 08:00 pre-market): retrying every 30 s only spends the shared KIS budget.
                    try: closed = market.providers['KR' if symbol.startswith('KR:') else 'US'].session() == 'closed'
                    except Exception: closed = False
                    retry_after[symbol] = time.monotonic() + (CLOSED_RETRY if closed else retry_delay)
                    redis_cache.set_json(f'market:collection:{symbol}',
                                         {'state': 'failed', 'checked_at': time.time(), 'error': type(exc).__name__}, 7*86400)
                    log.warning('market refresh failed',extra={'path':symbol,'status_code':type(exc).__name__})
    finally:
        market.close()


if __name__ == '__main__':
    main()
