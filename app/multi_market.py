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
        if not path.startswith(('/uapi/domestic-stock/v1/quotations/','/uapi/overseas-price/v1/quotations/')) and path != '/uapi/domestic-stock/v1/ranking/fluctuation':
            raise MarketError('허용되지 않은 시세 경로입니다.')
        def load():
            with self.lock:
                now=time.monotonic()
                if not self.configured: raise MarketError('국내 데이터 공급자 설정 필요')
                if now<self.cooldown: raise MarketError('국내 시세 요청 한도 대기 중입니다.')
                try:
                    if now>=self.expires:
                        self.cooldown=now+60
                        d=self._json(self.client.post('/oauth2/tokenP',json={'grant_type':'client_credentials','appkey':self.key,'appsecret':self.secret}))
                        self.token=d['access_token']; self.expires=now+int(d.get('expires_in',86400))-120
                        if not self.token: raise ValueError()
                        self.cooldown=0
                    # The KIS overseas historical endpoints rejected consecutive
                    # 0.5s requests in live verification; serialize at 1.1s.
                    delay=1.1-(time.monotonic()-self.last_call)
                    if delay>0: time.sleep(delay)
                    self.last_call=time.monotonic()
                    headers={'authorization':'Bearer '+self.token,'appkey':self.key,'appsecret':self.secret,'tr_id':tr_id,'custtype':'P'}
                    if tr_cont: headers['tr_cont']=tr_cont
                    r=self.client.get(path,headers=headers,params=params)
                    if r.status_code in (401,403): self.expires=0; self.cooldown=time.monotonic()+60
                    data=self._json(r)
                    if data.get('rt_cd')!='0':
                        if data.get('msg_cd') in ('EGW00201','EGW00133'): self.cooldown=time.monotonic()+60
                        raise MarketError('국내 시세 조회 권한/요청 한도를 확인하세요.')
                    return data | {'_tr_cont':r.headers.get('tr_cont','')}
                except (httpx.HTTPError,ValueError,KeyError,TypeError) as exc:
                    raise MarketError('국내 시세 공급자 응답 오류입니다.') from exc
        return self.responses.get((path,tr_cont,tuple(sorted(params.items()))),ttl,load)

    def quote(self, symbol):
        if not re.fullmatch(r'KR:[0-9]{6}',symbol): raise MarketError('잘못된 국내 종목코드입니다.')
        # Rounded minute makes requests from different browsers share one cache entry.
        d=self.get(self.PATH,'FHKST03010200',{'FID_COND_MRKT_DIV_CODE':'J','FID_INPUT_ISCD':symbol[3:],'FID_INPUT_HOUR_1':datetime.now(SEOUL).strftime('%H%M')+'00','FID_PW_DATA_INCU_YN':'Y','FID_ETC_CLS_CODE':''},int(os.getenv('QUOTE_TTL','15')))
        try:
            bar=max(d['output2'],key=lambda b:b['stck_bsop_date']+b['stck_cntg_hour'])
            stamp=int(datetime.strptime(bar['stck_bsop_date']+bar['stck_cntg_hour'],'%Y%m%d%H%M%S').replace(tzinfo=SEOUL).timestamp())
            price=Decimal(str(bar['stck_prpr'])).quantize(Decimal('.0001'))
            if not price.is_finite() or not 0<price<=1000000000 or not 0<stamp<=time.time()+60: raise ValueError()
            info=d.get('output1',{})
            return {'symbol':symbol,'price':price,'timestamp':stamp,'stale':time.time()-stamp>int(os.getenv('MAX_QUOTE_AGE','900')),
                    'name':info.get('hts_kor_isnm',symbol),'change':info.get('prdy_vrss'),'change_pct':info.get('prdy_ctrt'),
                    'high':info.get('stck_hgpr'),'low':info.get('stck_lwpr'),'volume':info.get('acml_vol'),
                    'turnover':info.get('acml_tr_pbmn') or bar.get('acml_tr_pbmn'),
                    'data_status':'KIS 분봉 · 실시간 체결 스트림 아님'}
        except (ValueError,KeyError,TypeError,InvalidOperation) as exc: raise MarketError('국내 시세를 확인할 수 없습니다.') from exc

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
            try:
                r = self.client.get('/v2/providers/ecb/rate/KRW/USD')
                r.raise_for_status()
                data = r.json()
                rate = Decimal(str(data['rate'])).quantize(Decimal('.000000000001'))
                day = date.fromisoformat(data['date'])
                age = (datetime.now(timezone.utc).date() - day).days
                if data['base'] != 'KRW' or data['quote'] != 'USD' or not rate.is_finite() or rate <= 0 or not 0 <= age <= 7:
                    raise ValueError('invalid FX rate')
            except (httpx.HTTPError, KeyError, TypeError, ValueError, InvalidOperation) as exc:
                self.cooldown = now + 60
                raise MarketError('유효한 기준환율이 없어 한국 종목을 평가하거나 거래할 수 없습니다.') from exc
            self.cached = (rate, day.isoformat())
            self.expires = now + 3600
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
        if self.us.key:
            for row in self.us.search(query):
                if valid_symbol(row['symbol']) and row['symbol'] not in {r['symbol'] for r in rows}:
                    rows.append(instrument(row['symbol']) | {'name': row['name']})
        return rows[:30]

    def quote(self, symbol):
        if not valid_symbol(symbol): raise MarketError('잘못된 종목 코드입니다.')
        info = instrument(symbol)
        if info['currency'] == 'USD':
            q = self.us.quote(symbol)
            return q | info | {'native_price': q['price'], 'fx_rate': Decimal(1), 'fx_date': None, 'source': 'Finnhub'}
        q = self.kr.quote(symbol)
        rate, fx_date = self.fx.krw_to_usd()
        price = (q['price'] * rate).quantize(Decimal('.0001'))
        if price <= 0: raise MarketError('환산 가격이 최소 거래 단위보다 작습니다.')
        return q | info | {'price': price, 'native_price': q['price'], 'fx_rate': rate,
                           'fx_date': fx_date, 'source': 'KIS 분봉 / ECB 일별 기준환율'}
