"""Read-only Finnhub adapter; contains no brokerage or real-order API."""
import os
import time
from collections import deque
from decimal import Decimal, InvalidOperation
from threading import RLock
from typing import Protocol
import httpx
from .redis_cache import redis_cache
from . import quote_policy

class MarketError(Exception): pass
class MarketData(Protocol):
    def quote(self, symbol: str) -> dict: ...
    def search(self, query: str) -> list: ...

class Finnhub:
    def __init__(self):
        self.key = os.getenv('FINNHUB_API_KEY', '')
        self.cache = {}
        self.calls = deque()
        self.lock = RLock()
        self.cooldown = 0
        self.client = httpx.Client(base_url='https://finnhub.io/api/v1', timeout=8)

    def get(self, path, params, ttl):
        key = (path, tuple(sorted(params.items())))
        redis_key = redis_cache.key('market:provider:finnhub', repr(key))
        with self.lock:
            now = time.monotonic()
            if key in self.cache and now - self.cache[key][0] < ttl:
                return self.cache[key][1]
            if not self.key:
                raise MarketError('FINNHUB_API_KEY를 설정해야 시세를 조회할 수 있습니다.')
            def load():
                call_now=time.monotonic();limit=int(os.getenv('MARKET_CALLS_PER_MINUTE','50'))
                while self.calls and call_now-self.calls[0]>60:self.calls.popleft()
                if call_now<self.cooldown or len(self.calls)>=limit or not redis_cache.allow_rate('finnhub',limit):
                    raise MarketError('시세 요청 한도에 도달했습니다. 1분 후 다시 시도하세요.')
                self.calls.append(call_now)
                try:
                    response = self.client.get(path, params=params, headers={'X-Finnhub-Token': self.key})
                    if response.status_code == 429:
                        self.cooldown = time.monotonic() + 60
                        raise MarketError('시세 공급자 요청 한도 초과. 잠시 후 다시 시도하세요.')
                    if response.status_code in (401,403):
                        raise MarketError('Finnhub API 키 또는 해당 데이터 이용 권한이 필요합니다. 무료 요금제는 과거 차트를 제공하지 않을 수 있습니다.')
                    response.raise_for_status()
                    result = response.json()
                    if isinstance(result, dict) and result.get('error'):
                        raise MarketError('시세 공급자가 요청을 거부했습니다.')
                    return result
                except (httpx.HTTPError, ValueError) as exc:
                    raise MarketError('시세 공급자 연결 또는 응답 오류입니다.') from exc
            data = redis_cache.get_or_load(redis_key, ttl, load)
            if len(self.cache) >= 2048:
                self.cache.clear()
            self.cache[key] = (time.monotonic(), data)
            return data

    def search(self, query):
        data = self.get('/search', {'q': query, 'exchange': 'US'}, 300)
        if not isinstance(data, dict) or not isinstance(data.get('result'), list):
            raise MarketError('종목 검색 응답 형식 오류입니다.')
        return [{'symbol': x['symbol'], 'name': x.get('description', '')} for x in data['result'] if x.get('type') in ('Common Stock', 'ETP', 'ADR') and '.' not in x.get('symbol', '')][:20]

    def quote(self, symbol):
        data = self.get('/quote', {'symbol': symbol}, int(os.getenv('QUOTE_TTL', '15')))
        try:
            price = Decimal(str(data['c'])).quantize(Decimal('.0001'))
            timestamp = int(data['t'])
            if not price.is_finite() or price <= 0 or price > Decimal('100000000') or timestamp <= 0 or timestamp > time.time() + 60:
                raise ValueError()
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise MarketError('유효한 가격이 없는 종목입니다.') from exc
        return {'symbol': symbol, 'price': price, 'timestamp': timestamp, 'stale': time.time() - timestamp > quote_policy.max_age('US'), 'change': data.get('d'), 'change_pct': data.get('dp'), 'high': data.get('h'), 'low': data.get('l'), 'volume': None, 'data_status': 'Finnhub 최근 시세 · 요금제별 지연 가능'}
