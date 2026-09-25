from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import os
import time
import httpx
from .market import MarketError
from .cache import TTLCache
from .instruments import valid_symbol
from .us_session import NEW_YORK
from .kr_session import SEOUL

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

KIS_OVERSEAS_QUOTES='/uapi/overseas-price/v1/quotations/'

def _us_minute_bar(b):
    stamp=datetime.strptime(b['xymd']+b['xhms'],'%Y%m%d%H%M%S').replace(tzinfo=NEW_YORK)
    return dict(time=int(stamp.timestamp()),open=b['open'],high=b['high'],low=b['low'],close=b['last'],volume=b.get('evol',0))

def _us_daily_bar(b,day):
    stamp=datetime.combine(day,datetime.min.time()).replace(tzinfo=NEW_YORK)
    return dict(time=int(stamp.timestamp()),open=b['open'],high=b['high'],low=b['low'],close=b['clos'],volume=b.get('tvol',0))

def _kr_minute_bar(b):
    stamp=datetime.strptime(b['stck_bsop_date']+b['stck_cntg_hour'],'%Y%m%d%H%M%S').replace(tzinfo=SEOUL)
    return dict(time=int(stamp.timestamp()),open=b['stck_oprc'],high=b['stck_hgpr'],low=b['stck_lwpr'],close=b['stck_prpr'],volume=b.get('cntg_vol',0))

def _kr_daily_bar(b):
    stamp=datetime.strptime(b['stck_bsop_date'],'%Y%m%d').replace(tzinfo=SEOUL)
    return dict(time=int(stamp.timestamp()),open=b['stck_oprc'],high=b['stck_hgpr'],low=b['stck_lwpr'],close=b['stck_clpr'],volume=b.get('acml_vol',0))

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
        return self.cache.get(('candles',s,period),30 if period=='1D' else 900,load)

    def _kis_candles(self,s,period):
        # KIS quotation APIs are read-only. The exchange is selected from actual
        # responses, never from a guessed price, and the result is marked as KIS.
        for exchange in ('NAS','NYS','AMS'):
            try:
                result=self._kis_minute_chart(s,exchange) if period=='1D' else self._kis_daily_chart(s,period,exchange)
                if result: return result
            except MarketError:
                if getattr(self.kis,'cooldown',0)>time.monotonic(): break
        raise MarketError('미국 과거 차트를 불러오지 못했습니다. Finnhub 과거 시세 권한과 KIS 해외 시세 권한을 확인하세요.')
    def _kis_minute_chart(self,s,exchange):
        """Today's 5-minute bars on one exchange, or None when it has none for the symbol."""
        data=self.kis.get(KIS_OVERSEAS_QUOTES+'inquire-time-itemchartprice','HHDFS76950200',
            {'AUTH':'','EXCD':exchange,'SYMB':s,'NMIN':'5','PINC':'1','NEXT':'','NREC':'120','FILL':'','KEYB':''},60)
        rows=[]
        for b in data.get('output2') or []:
            try: rows.append(_us_minute_bar(b))
            except (KeyError,ValueError,TypeError): continue
        result=candle_result(s,'1D','5m',rows,'KIS 미국 분봉')
        if not result['candles']: return None
        result['data_status']='KIS 제공 분봉 · 실시간 체결 스트림 아님'
        return result
    def _kis_daily_chart(self,s,period,exchange):
        """Daily/weekly/monthly bars on one exchange, paging back at most 6 times, or None when it has none."""
        days,_=RANGES[period]
        resolution={'1W':'D','3M':'D','1Y':'D','5Y':'W','ALL':'M'}[period]
        gubn={'D':'0','W':'1','M':'2'}[resolution]
        today=datetime.now(NEW_YORK).date()
        start=today-timedelta(days=days)
        rows=[]; continuation=''
        for _ in range(6):
            data=self.kis.get(KIS_OVERSEAS_QUOTES+'dailyprice','HHDFS76240000',
                {'AUTH':'','EXCD':exchange,'SYMB':s,'GUBN':gubn,'BYMD':'','MODP':'1'},900 if not continuation else 0,
                tr_cont=continuation)
            bars=data.get('output2') or []
            if not isinstance(bars,list) or not bars: break
            for b in bars:
                try:
                    day=date.fromisoformat(f"{b['xymd'][:4]}-{b['xymd'][4:6]}-{b['xymd'][6:8]}")
                    if start<=day<=today: rows.append(_us_daily_bar(b,day))
                except (KeyError,ValueError,TypeError): continue
            # Stop at the range start, or when KIS reports no further page.
            oldest=min((b.get('xymd','99999999') for b in bars),default='99999999')
            if oldest=='99999999' or oldest<=start.strftime('%Y%m%d') or data.get('_tr_cont') not in ('M','F'): break
            continuation='N'
        result=candle_result(s,period,resolution,rows,'KIS 미국 과거 시세')
        if not result['candles']: return None
        result['data_status']='KIS 과거 시세 · 공급자 제공 범위, 실시간 아님'
        requested_start=int(datetime.combine(start+timedelta(days=7),datetime.min.time()).replace(tzinfo=NEW_YORK).timestamp())
        result['partial']=period not in ('1D','1W') and min(r['time'] for r in rows)>requested_start
        return result
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
    def _alpha_movers(self,direction):
        data=self._leaders(); key={'up':'top_gainers','down':'top_losers','volume':'most_actively_traded','shares':'most_actively_traded'}[direction]
        rows=[]
        for row in data.get(key,[]):
            if valid_symbol(row['ticker']):
                try:
                    estimated=(Decimal(str(row['price']))*Decimal(str(row['volume']))).quantize(Decimal('.01'))
                except (ArithmeticError,ValueError): estimated=None
                rows.append({'symbol':row['ticker'],'name':row['ticker'],'price':row['price'],'change_pct':row['change_percentage'].rstrip('%'),'volume':row['volume'],'turnover':estimated,'turnover_estimated':True,'market':'US','currency':'USD','data_time':data.get('last_updated'),'data_status':'공급자 순위 스냅샷 · 실시간 아님'})
        if direction=='volume': rows.sort(key=lambda r:Decimal(str(r['turnover'] or 0)) if r['turnover'] is not None else Decimal(0),reverse=True)
        if direction=='shares': rows.sort(key=lambda r:Decimal(str(r['volume'] or 0)),reverse=True)
        return {'rows':rows,'source':'Alpha Vantage','data_time':data.get('last_updated'),'scope':'미국 거래량 상위 후보 · 거래대금 추정 정렬' if direction=='volume' else '미국 거래량 상위' if direction=='shares' else '미국 시장 · 공급자 순위','notice':'미국 거래대금은 스냅샷 가격×누적 거래량 추정치입니다. 전체 시장 거래대금 상위 순위는 아닙니다.' if direction=='volume' else '요금제별 갱신 주기/데이터 권한이 적용됩니다.'}
    def _kis_rank_rows(self,measure='turnover'):
        if not self.kis or not self.kis.configured: raise MarketError('KIS 미국 순위 공급자 설정이 필요합니다.')
        # Same response layout for both KIS rankings: 거래대금(trade-pbmn), 거래량(trade-vol).
        path,tr_id,label={'turnover':('trade-pbmn','HHDFS76320010','거래대금'),'volume':('trade-vol','HHDFS76310010','거래량')}[measure]
        def load():
            rows=[]; stamp=datetime.now(timezone.utc).isoformat()
            for exchange in ('NAS','NYS','AMS'):
                data=self.kis.get('/uapi/overseas-stock/v1/ranking/'+path,tr_id,
                    {'EXCD':exchange,'NDAY':'0','VOL_RANG':'0','AUTH':'','KEYB':'','PRC1':'','PRC2':''},30)
                for raw in data.get('output2') or []:
                    symbol=str(raw.get('symb','')).upper()
                    if not valid_symbol(symbol): continue
                    try:
                        price=Decimal(str(raw['last'])); volume=Decimal(str(raw['tvol'])); turnover=Decimal(str(raw['tamt']))
                        change=Decimal(str(raw.get('rate') or 0))
                        if not all(v.is_finite() for v in (price,volume,turnover,change)) or price<=0 or volume<0 or turnover<0: continue
                    except (KeyError,TypeError,ValueError,ArithmeticError): continue
                    rows.append({'symbol':symbol,'name':raw.get('name') or raw.get('ename') or symbol,'price':price,
                                 'change_pct':change,'volume':volume,'turnover':turnover,'market':'US','currency':'USD',
                                 'data_time':stamp,'data_status':f'KIS {exchange} 당일 {label} 순위 · 30초 확인'})
            # A security can occasionally appear in more than one exchange result.
            unique={}
            for row in rows:
                if row['symbol'] not in unique or row[measure]>unique[row['symbol']][measure]: unique[row['symbol']]=row
            if not unique: raise MarketError(f'KIS 미국 {label} 순위를 불러오지 못했습니다.')
            return list(unique.values()),stamp
        return self.cache.get('kis-us-rank' if measure=='turnover' else 'kis-us-rank-'+measure,30,load)
    def _kis_movers(self,direction):
        if direction=='shares':
            rows,stamp=self._kis_rank_rows('volume')
            return {'rows':sorted(rows,key=lambda r:r['volume'],reverse=True)[:100],'source':'KIS','data_time':stamp,'scope':'미국 거래량 순위',
                    'notice':'NASDAQ·NYSE·AMEX의 KIS 당일 누적 거래량 자료를 30초마다 다시 확인합니다.'}
        rows,stamp=self._kis_rank_rows()
        if direction=='volume': rows=sorted(rows,key=lambda r:r['turnover'],reverse=True)
        elif direction=='up': rows=sorted(rows,key=lambda r:r['change_pct'],reverse=True)
        else: rows=sorted(rows,key=lambda r:r['change_pct'])
        scope='미국 거래대금 순위' if direction=='volume' else f"미국 거래대금 상위 종목 중 {'상승률' if direction=='up' else '하락률'} 순위"
        return {'rows':rows[:100],'source':'KIS','data_time':stamp,'scope':scope,
                'notice':'NASDAQ·NYSE·AMEX의 KIS 당일 누적 거래대금 자료를 30초마다 다시 확인합니다.'}
    def movers(self,direction):
        if self.kis and self.kis.configured:
            try:return self._kis_movers(direction)
            except MarketError:pass
        return self._alpha_movers(direction)
    def volume_leaders(self): return self.movers('volume')
    def raw_status(self):
        """Finnhub /stock/market-status (holidays, early closes), or None.

        Failures are cached too: every quote assessment asks for the session."""
        def load():
            try: return self.adapter.get('/stock/market-status',{'exchange':'US'},60)
            except MarketError: return None
        return self.cache.get('raw-status',60,load)
    def session(self): return self.session_from(self.raw_status())
    def market_status(self):
        """Session plus the price sources that can actually serve it right now.

        Capability (`*_supported`) comes from the configured provider; runtime
        status (`*_status`) from the stream heartbeat and recent REST results.
        Nothing here is true merely because of the clock."""
        from .us_session import LABELS, EXTENDED
        from .quote_policy import stream_healthy, session_price_mode
        from .us_quotes import rest_health
        from .redis_cache import redis_cache
        raw=self.raw_status(); session=self.session_from(raw)
        kis=bool(self.kis and getattr(self.kis,'configured',False))
        stream=redis_cache.stream_status() if kis else None
        streaming=stream_healthy(stream)
        def rest(name):
            ok,record=rest_health(name)
            return ok or not record  # no recent attempt is not a failure
        rest_ok={'overnight':kis and rest('kis_overnight'),'pre_market':kis and rest('kis_primary'),
                 'after_hours':kis and rest('kis_primary'),
                 'regular':rest('finnhub') or (kis and rest('kis_primary'))}.get(session,False)
        def runtime(sessions,idle):
            if not kis: return 'unsupported'
            if session not in sessions: return idle
            return 'realtime' if streaming else 'rest' if rest_ok else 'provider_unavailable'
        extended=runtime(EXTENDED,'not_current_session')
        day=runtime({'overnight'},'available')
        label='휴장' if raw and raw.get('holiday') and session=='closed' else LABELS[session]
        mode=session_price_mode(session,stream if kis else None,rest_ok)
        is_open=session not in ('closed','unknown')
        return {'market':'US','label':label,'session':session,'timezone':'America/New_York',
                'open':is_open,'tradable':is_open and mode!='unavailable','venue':'US' if is_open else 'NONE',
                'source':'KIS' if kis and session!='regular' else 'Finnhub',
                'verified':raw is not None,'stream_connected':streaming,
                'price_mode':mode,
                'extended_prices':session in EXTENDED and extended in ('realtime','rest'),
                'extended_price_status':extended,
                'day_market_supported':kis,'day_market_status':day}
    @staticmethod
    def session_from(raw):
        from .us_session import resolve_session
        return resolve_session(raw)

class KRProvider:
    def __init__(self,adapter): self.adapter=adapter; self.cache=TTLCache()
    def search(self,q):
        from .instruments import discover
        return discover(q,'kr')
    def quote(self,s): return self.adapter.quote(s)
    def candles(self,s,period):
        validate(s,period)
        def load():
            now=datetime.now(SEOUL); days,res=RANGES[period]
            if period=='1D': return candle_result(s,period,'1m',self._minute_rows(s,now),'KIS KRX 당일 분봉')
            res='D' if period=='1W' else res
            return candle_result(s,period,res,self._daily_rows(s,res,now.date(),days),'KIS KRX 수정주가 · 1W는 일봉으로 제공')
        try: return self.cache.get(('candles',s,period),30 if period=='1D' else 900,load)
        except (KeyError,TypeError,ValueError): raise MarketError('국내 차트 데이터 형식 오류입니다.')
    def _minute_rows(self,s,now):
        """Today's 1-minute bars, paging back from now (at most 14 pages) until 09:00 or no older bar."""
        rows=[]; cursor=now.strftime('%H%M%S')
        for _ in range(14):
            d=self.adapter.get(self.adapter.PATH,'FHKST03010200',{'FID_COND_MRKT_DIV_CODE':'J','FID_INPUT_ISCD':s[3:],'FID_INPUT_HOUR_1':cursor,'FID_PW_DATA_INCU_YN':'Y','FID_ETC_CLS_CODE':''},60)
            bars=d.get('output2',[])
            if not bars: break
            rows.extend(_kr_minute_bar(b) for b in bars)
            earliest=min(r['time'] for r in rows)
            before=datetime.fromtimestamp(earliest,SEOUL)-timedelta(minutes=1)
            if before.date()!=now.date() or before.hour<9 or before.strftime('%H%M%S')>=cursor: break
            cursor=before.strftime('%H%M%S')
        return rows
    def _daily_rows(self,s,res,end,days):
        """Bars of resolution res covering `days` up to `end`, paging back by end date (at most 10 pages)."""
        rows=[]; start=end-timedelta(days=days)
        for _ in range(10):
            d=self.adapter.get('/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice','FHKST03010100',{'FID_COND_MRKT_DIV_CODE':'J','FID_INPUT_ISCD':s[3:],'FID_INPUT_DATE_1':start.strftime('%Y%m%d'),'FID_INPUT_DATE_2':end.strftime('%Y%m%d'),'FID_PERIOD_DIV_CODE':res,'FID_ORG_ADJ_PRC':'0'},900)
            bars=[b for b in d.get('output2',[]) if b.get('stck_bsop_date')]
            if not bars: break
            rows.extend(_kr_daily_bar(b) for b in bars)
            earliest=datetime.strptime(min(b['stck_bsop_date'] for b in bars),'%Y%m%d').date()
            if earliest<=start or earliest>end: break
            end=earliest-timedelta(days=1)
        return rows
    def movers(self,direction):
        def load():
            common={'FID_COND_MRKT_DIV_CODE':'J','FID_INPUT_ISCD':'0000','FID_DIV_CLS_CODE':'0','FID_TRGT_CLS_CODE':'0','FID_TRGT_EXLS_CLS_CODE':'0','FID_INPUT_PRICE_1':'','FID_INPUT_PRICE_2':'','FID_VOL_CNT':''}
            if direction in ('volume','shares'):
                path='/uapi/domestic-stock/v1/quotations/volume-rank'; tr='FHPST01710000'
                # KIS 20171: 3 requests 거래금액순, 0 requests 거래량순.
                params=common|{'FID_COND_SCR_DIV_CODE':'20171','FID_BLNG_CLS_CODE':'3' if direction=='volume' else '0','FID_INPUT_DATE_1':''}
            else:
                path='/uapi/domestic-stock/v1/ranking/fluctuation'; tr='FHPST01700000'
                params=common|{'FID_COND_SCR_DIV_CODE':'20170','FID_RANK_SORT_CLS_CODE':'0' if direction=='up' else '1','FID_INPUT_CNT_1':'0','FID_PRC_CLS_CODE':'0','FID_RSFL_RATE1':'','FID_RSFL_RATE2':''}
            data=self.adapter.get(path,tr,params,120)
            rows=[]; stamp=datetime.now(timezone.utc).isoformat()
            for r in data.get('output',[]):
                symbol='KR:'+r.get('mksc_shrn_iscd',r.get('stck_shrn_iscd',''))
                if valid_symbol(symbol): rows.append({'symbol':symbol,'name':r.get('hts_kor_isnm',symbol),'price':r['stck_prpr'],'change_pct':r['prdy_ctrt'],'volume':r['acml_vol'],'turnover':r.get('acml_tr_pbmn'),'market':'KR','currency':'KRW','data_time':stamp,'data_status':'KIS 조회 스냅샷 · 조회 시각'})
            sort_key={'volume':'turnover','shares':'volume'}.get(direction,'change_pct')
            def rank_value(row):
                try:
                    value=Decimal(str(row.get(sort_key) or 0))
                    return value if value.is_finite() else Decimal(0)
                except (ArithmeticError,ValueError): return Decimal(0)
            rows.sort(key=rank_value,reverse=direction!='down')
            scope={'volume':'KRX · KIS 거래금액순','shares':'KRX · KIS 거래량순'}.get(direction,'KRX · 공급자 반환 순위')
            notice={'volume':'KIS 거래금액순(20171) 공급자가 반환한 종목만 표시합니다. 시각은 API 조회 시각입니다.','shares':'KIS 거래량순(20171) 공급자가 반환한 종목만 표시합니다. 시각은 API 조회 시각입니다.'}.get(direction,'시각은 API 조회 시각입니다. 목록 가격으로 주문을 체결하지 않습니다.')
            return {'rows':rows,'scope':scope,'source':'KIS','data_time':stamp,'notice':notice}
        return self.cache.get(('leaders',direction),120,load)
    def volume_leaders(self): return self.movers('volume')
    def trading_day(self,local=None):
        """True/False from the KIS holiday API, None when it cannot be checked."""
        local=local or datetime.now(SEOUL); day=local.strftime('%Y%m%d')
        try:
            data=self.adapter.get('/uapi/domestic-stock/v1/quotations/chk-holiday','CTCA0903R',{'BASS_DT':day,'CTX_AREA_FK':'','CTX_AREA_NK':''},86400)
            return next(r for r in data['output'] if r['bass_dt']==day)['opnd_yn']=='Y'
        except (MarketError,KeyError,StopIteration,TypeError): return None
    def session(self):
        from .kr_session import clock_session
        day=self.cache.get('trading-day',300,self.trading_day)
        return 'unknown' if day is None else clock_session(trading_day=day)
    def market_status(self):
        """KRX+NXT session with the price sources that can serve it right now."""
        from .kr_session import LABELS, OPEN, venues
        from .quote_policy import stream_healthy, session_price_mode
        from .us_quotes import rest_health
        from .redis_cache import redis_cache
        day=self.cache.get('trading-day',300,self.trading_day)
        session=self.session()
        configured=bool(getattr(self.adapter,'configured',False))
        stream=redis_cache.stream_status() if configured else None
        streaming=stream_healthy(stream,market='KR')
        ok,record=rest_health('kis_kr')
        rest_ok=configured and (ok or not record)
        is_open=session in OPEN
        mode=session_price_mode(session,stream if configured else None,rest_ok,'KR')
        label='휴장' if day is False else LABELS[session]
        return {'market':'KR','label':label,'session':session,'timezone':'Asia/Seoul',
                'open':is_open,'tradable':is_open and mode!='unavailable',
                'venue':'UNIFIED' if is_open else 'NONE','venues_open':venues(session),
                'source':'KIS','verified':day is not None,'schedule_verified':False,
                'schedule_source':'KIS 휴장일 + 표준 시간표 (특별 개장시간 미반영)',
                'stream_connected':streaming,'price_mode':mode,
                'extended_prices':session in ('pre_market','after_hours') and is_open and mode!='unavailable',
                'nxt_supported':configured,'nxt_status':'open' if 'NXT' in venues(session) else 'closed'}
