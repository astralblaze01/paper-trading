import os
import time
import logging
from pathlib import Path

from .instruments import valid_symbol
from .multi_market import MultiMarket
from .redis_cache import redis_cache, trade_key
from .logging_config import configure_logging
from .kr_symbols import refresh_master
from .us_symbols import refresh_master as refresh_us_master

configure_logging()
log=logging.getLogger('market-worker')
HEARTBEAT = Path('/tmp/market-worker-heartbeat')  # compose healthcheck
MASTER_REFRESH = 86400  # seconds between symbol-master downloads
MASTER_RETRY = 3600     # a failed download is tried again after this instead


def main():
    os.environ['MARKET_WORKER_MODE'] = 'true'
    market = MultiMarket()
    interval = max(5, int(os.getenv('QUOTE_TTL', '15')))
    snapshot_ttl, retry_delay = max(120, interval * 8), max(30, interval * 2)
    refreshed = {}
    retry_after = {}
    attempted = {}
    master_refreshed=0
    try:
        while True:
            HEARTBEAT.touch()
            if time.monotonic()-master_refreshed>MASTER_REFRESH:
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
            now = time.monotonic()
            for symbol in sorted(dict.fromkeys(symbols), key=lambda s: (refreshed.get(s, 0), attempted.get(s, 0))):
                if not symbol or not valid_symbol(symbol):
                    continue
                if now < retry_after.get(symbol, 0):
                    continue
                if time.monotonic() - refreshed.get(symbol, 0) < interval:
                    continue
                attempted[symbol] = time.monotonic()
                try:
                    quote = market.quote_direct(symbol)
                    quote['_cached_at'] = time.time()
                    # Publication is monotonic in trade time. When the trade
                    # stream holds a newer print, republish that print so it
                    # stays available, instead of failing on the older bar.
                    trade = redis_cache.get_json(trade_key(symbol))
                    if trade and float(trade.get('timestamp', 0)) > float(quote['timestamp']):
                        quote = trade
                    # Tradeability is judged at read time (quote_policy), so
                    # the snapshot may outlive a slow provider cycle.
                    if not redis_cache.store_quote(symbol, quote, snapshot_ttl):
                        raise ValueError('quote store rejected')
                    refreshed[symbol] = time.monotonic()
                    retry_after.pop(symbol, None)
                    log.info('quote stored and published; quote_age_seconds=%s', round(time.time()-quote['timestamp'], 2), extra={'path': symbol})
                except Exception as exc:
                    # Do not leak provider credentials or response bodies.
                    retry_after[symbol] = time.monotonic() + retry_delay
                    log.warning('market refresh failed',extra={'path':symbol,'status_code':type(exc).__name__})
    finally:
        market.close()


if __name__ == '__main__':
    main()
