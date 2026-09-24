from typing import Protocol
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
import os
import time
import httpx
from .market import MarketError
from .cache import TTLCache
from .instruments import valid_symbol

class MarketDataProvider(Protocol):
    def search(self, query: str) -> list: ...
    def quote(self, symbol: str) -> dict: ...
    def candles(self, symbol: str, period: str) -> dict: ...
    def movers(self, direction: str) -> dict: ...
    def volume_leaders(self) -> dict: ...
    def market_status(self) -> dict: ...

RANGES={'1D':(1,'5'),'1W':(7,'30'),'3M':(93,'D'),'1Y':(366,'D'),'5Y':(1830,'W'),'ALL':(365*40,'M')}

def validate(symbol, period):
    if not valid_symbol(symbol) or period not in RANGES: raise MarketError('잘못된 종목 또는 차트 기간입니다.')

def candle_result(symbol, period, resolution, rows, source):
    clean=[]
    for r in rows:
        if r['time']>time.time()+86400: continue
        values=[Decimal(str(r[k])) for k in ('open','high','low','close')]
        if not all(v.is_finite() and v>0 for v in values): continue
        if values[1]<max(values[0],values[2],values[3]) or values[2]>min(values[0],values[1],values[3]): continue
        clean.append(r)
    clean=sorted({r['time']:r for r in clean}.values(),key=lambda r:r['time'])
    return {'symbol':symbol,'range':period,'resolution':resolution,'candles':clean,'source':source,
            'data_status':'과거 시세 · 공급자 제공 범위','last_timestamp':clean[-1]['time'] if clean else None,
            'stale':not clean or time.time()-clean[-1]['time']>86400*4,'partial':period=='ALL'}

class USProvider:
    def __init__(self, adapter, kis=None): self.adapter=adapter; self.kis=kis; self.cache=TTLCache()
    def search(self,q): return self.adapter.search(q)
    def quote(self,s): return self.adapter.quote(s)
    def candles(self,s,period):
        validate(s,period)
        def load():
            try:
                days,res=RANGES[period]; now=int(time.time())
                data=self.adapter.get('/stock/candle',{'symbol':s,'resolution':res,'from':max(0,now-days*86400),'to':now},60 if period=='1D' else 900)
                if data.get('s')=='no_data': raise MarketError('Finnhub 과거 차트 데이터가 없습니다.')
                rows=[dict(time=t,open=data['o'][i],high=data['h'][i],low=data['l'][i],close=data['c'][i],volume=data['v'][i]) for i,t in enumerate(data['t'])]
                return candle_result(s,period,res,rows,'Finnhub')
            except (KeyError,IndexError,TypeError,MarketError) as exc:
                if self.kis and self.kis.configured:
                    return self._kis_candles(s,period)
                if isinstance(exc,MarketError): raise
                raise MarketError('차트 응답 형식 오류 또는 이용 권한 부족입니다.') from exc
        return self.cache.get(('candles',s,period),60 if period=='1D' else 900,load)

    def _kis_candles(self,s,period):
        # KIS quotation APIs are read-only. The exchange is selected from actual
        # responses, never from a guessed price, and the result is marked as KIS.
        exchanges=('NAS','NYS','AMS')
        path='/uapi/overseas-price/v1/quotations/'
        for exchange in exchanges:
            try:
                if period=='1D':
                    data=self.kis.get(path+'inquire-time-itemchartprice','HHDFS76950200',
                        {'AUTH':'','EXCD':exchange,'SYMB':s,'NMIN':'5','PINC':'1','NEXT':'','NREC':'120','FILL':'','KEYB':''},60)
                    bars=data.get('output2') or []
                    rows=[]
                    for b in bars:
                        try:
                            stamp=datetime.strptime(b['xymd']+b['xhms'],'%Y%m%d%H%M%S').replace(tzinfo=ZoneInfo('America/New_York'))
                            rows.append(dict(time=int(stamp.timestamp()),open=b['open'],high=b['high'],low=b['low'],close=b['last'],volume=b.get('evol',0)))
                        except (KeyError,ValueError,TypeError): continue
                    result=candle_result(s,period,'5m',rows,'KIS 미국 분봉')
                    if result['candles']:
                        result['data_status']='KIS 제공 분봉 · 실시간 체결 스트림 아님'
                        return result
                    continue
                days,_=RANGES[period]
                resolution={'1W':'D','3M':'D','1Y':'D','5Y':'W','ALL':'M'}[period]
                gubn={'D':'0','W':'1','M':'2'}[resolution]
                today=datetime.now(ZoneInfo('America/New_York')).date()
                start=today-timedelta(days=days)
                rows=[]; continuation=''
                for _ in range(6):
                    data=self.kis.get(path+'dailyprice','HHDFS76240000',
                        {'AUTH':'','EXCD':exchange,'SYMB':s,'GUBN':gubn,'BYMD':'','MODP':'1'},900 if not continuation else 0,
                        tr_cont=continuation)
                    bars=data.get('output2') or []
                    if not isinstance(bars,list) or not bars: break
                    valid=[]
                    for b in bars:
                        try:
                            day=date.fromisoformat(f"{b['xymd'][:4]}-{b['xymd'][4:6]}-{b['xymd'][6:8]}")
                            if day<start or day>today: continue
                            stamp=datetime.combine(day,datetime.min.time()).replace(tzinfo=ZoneInfo('America/New_York'))
                            valid.append(dict(time=int(stamp.timestamp()),open=b['open'],high=b['high'],low=b['low'],close=b['clos'],volume=b.get('tvol',0)))
                        except (KeyError,ValueError,TypeError): continue
                    rows.extend(valid)
                    oldest=min((b.get('xymd','99999999') for b in bars),default='99999999')
                    if oldest=='99999999' or oldest<=start.strftime('%Y%m%d') or data.get('_tr_cont') not in ('M','F'): break
                    continuation='N'
                result=candle_result(s,period,resolution,rows,'KIS 미국 과거 시세')
                if result['candles']:
                    result['data_status']='KIS 과거 시세 · 공급자 제공 범위, 실시간 아님'
                    requested_start=int(datetime.combine(start+timedelta(days=7),datetime.min.time()).replace(tzinfo=ZoneInfo('America/New_York')).timestamp())
                    result['partial']=period not in ('1D','1W') and min(r['time'] for r in rows)>requested_start
                    return result
            except MarketError:
                if getattr(self.kis,'cooldown',0)>time.monotonic(): break
        raise MarketError('미국 과거 차트를 불러오지 못했습니다. Finnhub 과거 시세 권한과 KIS 해외 시세 권한을 확인하세요.')
    def _leaders(self):
        key=os.getenv('ALPHAVANTAGE_API_KEY','')
        if not key: raise MarketError('미국 전체 시장 순위는 별도 공급자 설정이 필요합니다. ALPHAVANTAGE_API_KEY를 설정하세요.')
        def load():
            try:
                r=httpx.get('https://www.alphavantage.co/query',params={'function':'TOP_GAINERS_LOSERS','apikey':key},timeout=8)
                r.raise_for_status(); data=r.json()
                if 'most_actively_traded' not in data: raise ValueError()
                return data
            except (httpx.HTTPError,ValueError): raise MarketError('미국 순위 공급자 한도/응답 오류입니다.')
        return self.cache.get('leaders',3600,load)
    def movers(self,direction):
        data=self._leaders(); key={'up':'top_gainers','down':'top_losers','volume':'most_actively_traded'}[direction]
        rows=[]
        for row in data.get(key,[]):
            if valid_symbol(row['ticker']):
                try:
                    estimated=(Decimal(str(row['price']))*Decimal(str(row['volume']))).quantize(Decimal('.01'))
                except (ArithmeticError,ValueError): estimated=None
                rows.append({'symbol':row['ticker'],'name':row['ticker'],'price':row['price'],'change_pct':row['change_percentage'].rstrip('%'),'volume':row['volume'],'turnover':estimated,'turnover_estimated':True,'market':'US','currency':'USD','data_time':data.get('last_updated'),'data_status':'공급자 순위 스냅샷 · 실시간 아님'})
        if direction=='volume': rows.sort(key=lambda r:Decimal(str(r['turnover'] or 0)) if r['turnover'] is not None else Decimal(0),reverse=True)
        return {'rows':rows,'source':'Alpha Vantage','data_time':data.get('last_updated'),'scope':'미국 거래량 상위 후보 · 거래대금 추정 정렬' if direction=='volume' else '미국 시장 · 공급자 순위','notice':'미국 거래대금은 스냅샷 가격×누적 거래량 추정치입니다. 전체 시장 거래대금 상위 순위는 아닙니다.' if direction=='volume' else '요금제별 갱신 주기/데이터 권한이 적용됩니다.'}
    def volume_leaders(self): return self.movers('volume')
    def market_status(self):
        def load():
            d=self.adapter.get('/stock/market-status',{'exchange':'US'},60)
            session=d.get('session')
            label='휴장' if d.get('holiday') else {'pre-market':'프리장','post-market':'애프터장','regular':'정규장'}.get(session,'정규장' if d.get('isOpen') else '장마감')
            return {'label':label,'timezone':'America/New_York','source':'Finnhub','verified':True,'extended_prices':False,'day_market_supported':False}
        try: return self.cache.get('status',60,load)
        except MarketError: return {'label':'장 상태 확인 불가','timezone':'America/New_York','verified':False,'extended_prices':False}

class KRProvider:
    def __init__(self,adapter): self.adapter=adapter; self.cache=TTLCache()
    def search(self,q):
        from .instruments import discover
        return discover(q,'kr')
    def quote(self,s): return self.adapter.quote(s)
    def candles(self,s,period):
        validate(s,period)
        def load():
            rows=[]
            now=datetime.now(ZoneInfo('Asia/Seoul')); days,res=RANGES[period]
            if period=='1D':
                cursor=now.strftime('%H%M%S')
                for _ in range(14):
                    d=self.adapter.get(self.adapter.PATH,'FHKST03010200',{'FID_COND_MRKT_DIV_CODE':'J','FID_INPUT_ISCD':s[3:],'FID_INPUT_HOUR_1':cursor,'FID_PW_DATA_INCU_YN':'Y','FID_ETC_CLS_CODE':''},60)
                    bars=d.get('output2',[])
                    if not bars: break
                    for b in bars:
                        stamp=datetime.strptime(b['stck_bsop_date']+b['stck_cntg_hour'],'%Y%m%d%H%M%S').replace(tzinfo=ZoneInfo('Asia/Seoul'))
                        rows.append(dict(time=int(stamp.timestamp()),open=b['stck_oprc'],high=b['stck_hgpr'],low=b['stck_lwpr'],close=b['stck_prpr'],volume=b.get('cntg_vol',0)))
                    earliest=min(r['time'] for r in rows)
                    nxt=datetime.fromtimestamp(earliest,ZoneInfo('Asia/Seoul'))-timedelta(minutes=1)
                    if nxt.date()!=now.date() or nxt.hour<9 or nxt.strftime('%H%M%S')>=cursor: break
                    cursor=nxt.strftime('%H%M%S')
                return candle_result(s,period,'1m',rows,'KIS 당일 분봉')
            res='D' if period=='1W' else res
            end=now.date(); start=end-timedelta(days=days)
            for _ in range(10):
                d=self.adapter.get('/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice','FHKST03010100',{'FID_COND_MRKT_DIV_CODE':'J','FID_INPUT_ISCD':s[3:],'FID_INPUT_DATE_1':start.strftime('%Y%m%d'),'FID_INPUT_DATE_2':end.strftime('%Y%m%d'),'FID_PERIOD_DIV_CODE':res,'FID_ORG_ADJ_PRC':'0'},900)
                bars=[b for b in d.get('output2',[]) if b.get('stck_bsop_date')]
                if not bars: break
                for b in bars:
                    stamp=datetime.strptime(b['stck_bsop_date'],'%Y%m%d').replace(tzinfo=ZoneInfo('Asia/Seoul'))
                    rows.append(dict(time=int(stamp.timestamp()),open=b['stck_oprc'],high=b['stck_hgpr'],low=b['stck_lwpr'],close=b['stck_clpr'],volume=b.get('acml_vol',0)))
                earliest=datetime.strptime(min(b['stck_bsop_date'] for b in bars),'%Y%m%d').date()
                if earliest<=start or earliest>end: break
                end=earliest-timedelta(days=1)
            return candle_result(s,period,res,rows,'KIS 수정주가 · 1W는 일봉으로 제공')
        try: return self.cache.get(('candles',s,period),60 if period=='1D' else 900,load)
        except (KeyError,TypeError,ValueError): raise MarketError('국내 차트 데이터 형식 오류입니다.')
    def movers(self,direction):
        def load():
            common={'FID_COND_MRKT_DIV_CODE':'J','FID_INPUT_ISCD':'0000','FID_DIV_CLS_CODE':'0','FID_TRGT_CLS_CODE':'0','FID_TRGT_EXLS_CLS_CODE':'0','FID_INPUT_PRICE_1':'','FID_INPUT_PRICE_2':'','FID_VOL_CNT':''}
            if direction=='volume':
                path='/uapi/domestic-stock/v1/quotations/volume-rank'; tr='FHPST01710000'
                # KIS 20171: 3 requests 거래금액순 rather than 거래량순 (0).
                params=common|{'FID_COND_SCR_DIV_CODE':'20171','FID_BLNG_CLS_CODE':'3','FID_INPUT_DATE_1':''}
            else:
                path='/uapi/domestic-stock/v1/ranking/fluctuation'; tr='FHPST01700000'
                params=common|{'FID_COND_SCR_DIV_CODE':'20170','FID_RANK_SORT_CLS_CODE':'0' if direction=='up' else '1','FID_INPUT_CNT_1':'0','FID_PRC_CLS_CODE':'0','FID_RSFL_RATE1':'','FID_RSFL_RATE2':''}
            data=self.adapter.get(path,tr,params,120)
            rows=[]; stamp=datetime.now(timezone.utc).isoformat()
            for r in data.get('output',[]):
                symbol='KR:'+r.get('mksc_shrn_iscd',r.get('stck_shrn_iscd',''))
                if valid_symbol(symbol): rows.append({'symbol':symbol,'name':r.get('hts_kor_isnm',symbol),'price':r['stck_prpr'],'change_pct':r['prdy_ctrt'],'volume':r['acml_vol'],'turnover':r.get('acml_tr_pbmn'),'market':'KR','currency':'KRW','data_time':stamp,'data_status':'KIS 조회 스냅샷 · 조회 시각'})
            sort_key='turnover' if direction=='volume' else 'change_pct'
            def rank_value(row):
                try:
                    value=Decimal(str(row.get(sort_key) or 0))
                    return value if value.is_finite() else Decimal(0)
                except (ArithmeticError,ValueError): return Decimal(0)
            rows.sort(key=rank_value,reverse=direction!='down')
            return {'rows':rows,'scope':'KRX · KIS 거래금액순' if direction=='volume' else 'KRX · 공급자 반환 순위','source':'KIS','data_time':stamp,'notice':'KIS 거래금액순(20171) 공급자가 반환한 종목만 표시합니다. 시각은 API 조회 시각입니다.' if direction=='volume' else '시각은 API 조회 시각입니다. 목록 가격으로 주문을 체결하지 않습니다.'}
        return self.cache.get(('leaders',direction),120,load)
    def volume_leaders(self): return self.movers('volume')
    def market_status(self):
        local=datetime.now(ZoneInfo('Asia/Seoul')); day=local.strftime('%Y%m%d')
        try:
            data=self.adapter.get('/uapi/domestic-stock/v1/quotations/chk-holiday','CTCA0903R',{'BASS_DT':day,'CTX_AREA_FK':'','CTX_AREA_NK':''},86400)
            record=next(r for r in data['output'] if r['bass_dt']==day)
            if record['opnd_yn']!='Y': label='휴장'
            else:
                minutes=local.hour*60+local.minute
                label='정규장' if 540<=minutes<930 else '장전' if 480<=minutes<540 else '장후' if 930<=minutes<1080 else '장마감'
            return {'label':label,'timezone':'Asia/Seoul','source':'KIS 휴장일 + 표준 시간표 (특별 개장시간 미반영)','verified':False,'extended_prices':False}
        except (MarketError,KeyError,StopIteration,TypeError): return {'label':'장 상태 확인 불가','timezone':'Asia/Seoul','verified':False,'extended_prices':False}
