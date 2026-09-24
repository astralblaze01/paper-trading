import os
import time
import logging
from pathlib import Path

from .instruments import valid_symbol
from .multi_market import MultiMarket
from .redis_cache import redis_cache
from .logging_config import configure_logging

configure_logging()
log=logging.getLogger('market-worker')


def main():
    os.environ['MARKET_WORKER_MODE'] = 'true'
    market = MultiMarket()
    interval = max(5, int(os.getenv('QUOTE_TTL', '15')))
    refreshed = {}
    try:
        while True:
            Path('/tmp/market-worker-heartbeat').touch()
            urgent = redis_cache.next_refresh(timeout=1)
            symbols = ([urgent] if urgent else []) + redis_cache.requested_symbols()
            now = time.monotonic()
            for symbol in dict.fromkeys(symbols):
                if not symbol or not valid_symbol(symbol):
                    continue
                if symbol != urgent and now - refreshed.get(symbol, 0) < interval:
                    continue
                try:
                    quote = market.quote_direct(symbol)
                    quote['_cached_at'] = time.time()
                    redis_cache.set_json(f'market:price:{symbol}', quote, max(30, interval * 3))
                    refreshed[symbol] = time.monotonic()
                except Exception as exc:
                    # Do not leak provider credentials or response bodies.
                    log.warning('market refresh failed',extra={'path':symbol,'status_code':type(exc).__name__})
    finally:
        market.close()


if __name__ == '__main__':
    main()
