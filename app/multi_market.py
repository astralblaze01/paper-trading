"""Price-only adapters. No account number, brokerage orders, or payment routes."""
import os
import re
import time
from datetime import datetime, timezone, date
from decimal import Decimal, InvalidOperation
from threading import RLock
from zoneinfo import ZoneInfo
import httpx
from .market import Finnhub, MarketError
from .instruments import discover, instrument, valid_symbol
from .redis_cache import price_key, redis_cache
from .quote_data import normalize_quote

SEOUL = ZoneInfo('Asia/Seoul')

class KoreaPrices:
    PATH = '/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice'

    def __init__(self):
        self.key = os.getenv('KOREA_MARKET_API_KEY') or os.getenv('KIS_APP_KEY', '')
        self.secret = os.getenv('KOREA_MARKET_API_SECRET') or os.getenv('KIS_APP_SECRET', '')
        self.configured = bool(self.key and self.secret)
        # Only token issuance and the fixed quotation endpoint are ever called.
        self.client = httpx.Client(base_url='https://openapi.koreainvestment.com:9443', timeout=8)
        self.lock = RLock()
        self.token = ''
        self.expires = 0
        self.cooldown = 0
        self.last_call = 0
        self.cache = {}
        from .cache import TTLCache
        self.responses = TTLCache()

    def _token_key(self):
        # KIS limits token issuance. Web and background workers therefore share
        # one read-only quotation token instead of issuing one per process.
        return redis_cache.key('market:kis:access-token',self.key+'\0'+self.secret)

    def _access_token(self, now):
        def issue():
            self.cooldown=now+60
            d=self._json(self.client.post('/oauth2/tokenP',json={'grant_type':'client_credentials','appkey':self.key,'appsecret':self.secret}))
            token=d['access_token']; lifetime=max(60,int(d.get('expires_in',86400))-120)
            if not token: raise ValueError()
            self.cooldown=0
            return {'access_token':token,'expires_at':time.time()+lifetime}
        shared=redis_cache.get_or_load(self._token_key(),21600,issue)
        token=str(shared['access_token']); remaining=float(shared.get('expires_at',time.time()+3600))-time.time()
        if not token or remaining<=0:
            self._drop_shared_token(token)
            raise ValueError('expired KIS token')
        self.token=token; self.expires=now+remaining

    def _drop_shared_token(self, token):
        # Another process may already have stored a replacement; only remove
        # the token this process actually saw rejected.
        redis_cache.delete_if(self._token_key(),lambda shared:shared.get('access_token')==token)

    def _json(self, response):
        if response.status_code == 429:
            self.cooldown = time.monotonic() + 60
            raise MarketError('한국 시세 요청 한도 초과. 1분 후 다시 시도하세요.')
        if response.status_code >= 400:
            try: code=response.json().get('msg_cd') or response.json().get('error_code')
            except (ValueError,TypeError,AttributeError): code=None
            if code in ('EGW00201','EGW00133'):
                self.cooldown=time.monotonic()+60
                raise MarketError('KIS 시세 요청 한도입니다. 1분 후 다시 시도하세요.')
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError('invalid response')
        return data

    def get(self, path, tr_id, params, ttl=15, tr_cont=''):
        if not path.startswith(('/uapi/domestic-stock/v1/quotations/','/uapi/overseas-price/v1/quotations/','/uapi/overseas-stock/v1/ranking/')) and path not in ('/uapi/domestic-stock/v1/ranking/fluctuation','/uapi/domestic-stock/v1/ksdinfo/dividend'):
            raise MarketError('허용되지 않은 시세 경로입니다.')
        def load():
            with self.lock:
                now=time.monotonic()
                if not self.configured: raise MarketError('국내 데이터 공급자 설정 필요')
                if now<self.cooldown: raise MarketError('국내 시세 요청 한도 대기 중입니다.')
                try:
                    if now>=self.expires: self._access_token(now)
                    # The KIS overseas historical endpoints rejected consecutive
                    # 0.5s requests in live verification; serialize at 1.1s.
                    delay=1.1-(time.monotonic()-self.last_call)
                    if delay>0: time.sleep(delay)
                    self.last_call=time.monotonic()
                    headers={'authorization':'Bearer '+self.token,'appkey':self.key,'appsecret':self.secret,'tr_id':tr_id,'custtype':'P'}
                    if tr_cont: headers['tr_cont']=tr_cont
                    r=self.client.get(path,headers=headers,params=params)
                    if r.status_code in (401,403):
                        self.expires=0; self.cooldown=time.monotonic()+60; self._drop_shared_token(self.token)
                    data=self._json(r)
                    if data.get('rt_cd')!='0':
                        if data.get('msg_cd') in ('EGW00201','EGW00133'): self.cooldown=time.monotonic()+60
                        raise MarketError('국내 시세 조회 권한/요청 한도를 확인하세요.')
                    return data | {'_tr_cont':r.headers.get('tr_cont','')}
                except (httpx.HTTPError,ValueError,KeyError,TypeError) as exc:
                    raise MarketError('국내 시세 공급자 응답 오류입니다.') from exc
        cache_key=(path,tr_cont,tuple(sorted(params.items())))
        return self.responses.get(cache_key,ttl,lambda:redis_cache.get_or_load(redis_cache.key('market:provider:kis',repr(cache_key)),ttl,load))

    def quote(self, symbol):
        if not re.fullmatch(r'KR:[0-9]{6}',symbol): raise MarketError('잘못된 국내 종목코드입니다.')
        from .kr_quotes import unified_quote
        return unified_quote(self, symbol)

class ReferenceFX:
    """Daily ECB reference rates for paper conversion, NOT a live FX quote."""
    def __init__(self):
        self.client = httpx.Client(base_url='https://api.frankfurter.dev', timeout=8)
        self.lock = RLock()
        self.cached = None
        self.expires = 0
        self.cooldown = 0

    def krw_to_usd(self):
        with self.lock:
            now = time.monotonic()
            if self.cached and now < self.expires: return self.cached
            if now < self.cooldown: raise MarketError('환율 조회를 잠시 후 다시 시도하세요.')
            cache_key = 'market:fx:USD:KRW:v2'
            def load():
                try:
                    # Request USD/KRW directly. The reverse endpoint is rounded
                    # to too few decimal places and produces a distorted rate
                    # when inverted for the user-facing USD/KRW quote.
                    r = self.client.get('/v2/providers/ecb/rate/USD/KRW')
                    r.raise_for_status()
                    return r.json()
                except (httpx.HTTPError, ValueError) as exc:
                    raise MarketError('유효한 기준환율이 없어 한국 종목을 평가하거나 거래할 수 없습니다.') from exc
            def parse(data):
                usd_to_krw = Decimal(str(data['rate']))
                day = date.fromisoformat(data['date'])
                age = (datetime.now(timezone.utc).date() - day).days
                if data['base'] != 'USD' or data['quote'] != 'KRW' or not usd_to_krw.is_finite() or usd_to_krw <= 0 or not 0 <= age <= 7:
                    raise ValueError('invalid FX rate')
                return (Decimal(1) / usd_to_krw).quantize(Decimal('.000000000001')), day
            try:
                data=redis_cache.get_json(cache_key)
                if data is not None:
                    try:
                        rate,day=parse(data)
                    except (KeyError,TypeError,ValueError,InvalidOperation):
                        redis_cache.delete(cache_key)
                        data=None
                if data is None:
                    data=redis_cache.get_or_load(cache_key,1800,load)
                    try:
                        rate,day=parse(data)
                    except (KeyError,TypeError,ValueError,InvalidOperation):
                        redis_cache.delete(cache_key)
                        raise
            except (KeyError, TypeError, ValueError, InvalidOperation, MarketError) as exc:
                self.cooldown = now + 60
                if isinstance(exc,MarketError): raise
                raise MarketError('유효한 기준환율이 없어 한국 종목을 평가하거나 거래할 수 없습니다.') from exc
            self.cached = (rate, day.isoformat())
            self.expires = now + 1800
            return self.cached

class MultiMarket:
    def __init__(self):
        self.us = Finnhub()
        if os.getenv('KOREA_MARKET_PROVIDER','kis') not in ('kis','disabled'): raise ValueError('Unsupported KOREA_MARKET_PROVIDER')
        self.kr = KoreaPrices()
        if os.getenv('KOREA_MARKET_PROVIDER','kis')=='disabled': self.kr.configured=False
        self.fx = ReferenceFX()
        from .providers import USProvider, KRProvider
        self.providers={'US':USProvider(self.us,self.kr),'KR':KRProvider(self.kr)}
        from .us_quotes import USQuotes
        self.us_quotes = USQuotes(self.us, self.kr)
        self._stream = (0.0, None)
        self.key = self.us.key or self.kr.key
        self.client = self  # lifespan close interface

    def close(self):
        self.us.client.close()
        self.kr.client.close()
        self.fx.client.close()

    def status(self):
        return {'us': bool(self.us.key), 'kr': self.kr.configured}

    def search(self, query='', category='all'):
        query = query.strip()
        rows = discover(query, category)
        if not query or category in ('us_bond', 'kr_bond', 'gold'): return rows
        if category in ('all','kr'):
            from .kr_symbols import search_master
            for row in search_master(query):
                if row['symbol'] not in {r['symbol'] for r in rows}: rows.append(row)
        code = query.removeprefix('KR:')
        if re.fullmatch(r'[0-9]{6}', code) and category in ('all', 'kr'):
            symbol = 'KR:' + code
            if not rows:
                q = self.kr.quote(symbol)
                row = instrument(symbol)
                row['name'] = q['name']
                rows.append(row)
            return rows
        if category == 'kr': return rows
        from .us_symbols import has_hangul, search_master as search_us_master
        if has_hangul(query):
            # Finnhub only matches English names; Korean names of US listings
            # come from the KIS overseas master.
            for row in search_us_master(query):
                if row['symbol'] not in {r['symbol'] for r in rows}:
                    rows.append(instrument(row['symbol']) | {'name': row['name']})
            return rows[:30]
        if self.us.key:
            for row in self.us.search(query):
                if valid_symbol(row['symbol']) and row['symbol'] not in {r['symbol'] for r in rows}:
                    rows.append(instrument(row['symbol']) | {'name': row['name']})
        return rows[:30]

    def quote(self, symbol):
        if os.getenv('MARKET_CACHE_MODE','direct').lower() == 'worker' and os.getenv('MARKET_WORKER_MODE','false').lower() != 'true':
            cached = redis_cache.get_json(price_key(symbol))
            redis_cache.request_quote(symbol, force=cached is None)
            deadline = time.monotonic() + (3 if cached is None else 0)
            while cached is None and time.monotonic() < deadline:
                time.sleep(.1)
                cached = redis_cache.get_json(price_key(symbol))
            if cached is None:
                raise MarketError('시세 수집기가 가격을 준비 중입니다. 잠시 후 다시 시도하세요.')
            try:
                return self.assess(symbol, normalize_quote(symbol, cached))
            except (ValueError, TypeError, KeyError) as exc:
                raise MarketError('유효한 서버 시세가 없습니다.') from exc
        return self.assess(symbol, self.quote_direct(symbol))

    def stream_status(self):
        # One Redis read per second per process, however many quotes are assessed.
        at, value = self._stream
        if time.monotonic() - at > 1:
            value = redis_cache.stream_status()
            self._stream = (time.monotonic(), value)
        return value

    def assess(self, symbol, q):
        """Attach current-session tradeability to a quote (see quote_policy)."""
        from .quote_policy import assess
        if 'valid_sessions' not in q:
            # Pre-upgrade snapshots were Finnhub (US) or KRX (KR) prints of the
            # regular session only.
            q = q | {'origin': 'rest', 'valid_sessions': ['regular']}
        code = 'KR' if symbol.startswith('KR:') else 'US'
        return assess(q, self.providers[code].session(), self.stream_status())

    def quote_direct(self, symbol):
        if not valid_symbol(symbol): raise MarketError('잘못된 종목 코드입니다.')
        info = instrument(symbol)
        if info['currency'] == 'USD':
            q = self.us_quotes.quote(symbol, self.providers['US'].session())
            return q | info | {'native_price': q['price'], 'fx_rate': Decimal(1), 'fx_date': None}
        from .us_quotes import record_health
        try:
            q = self.kr.quote(symbol)
        except MarketError as exc:
            record_health('kis_kr', False, type(exc).__name__)
            raise
        record_health('kis_kr', True)
        rate, fx_date = self.fx.krw_to_usd()
        price = (q['price'] * rate).quantize(Decimal('.0001'))
        if price <= 0: raise MarketError('환산 가격이 최소 거래 단위보다 작습니다.')
        return q | info | {'price': price, 'native_price': q['price'], 'fx_rate': rate, 'fx_date': fx_date}
