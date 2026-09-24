"""Read-only provider company facts; never fabricate a business description."""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from .instruments import instrument
from .market import MarketError
from .cache import TTLCache
cache=TTLCache()

def company_info(symbol,market):
    def load():
        info=instrument(symbol)
        result=info|{'industry':None,'exchange':None,'country':None,'website':None,'ipo':None,'market_cap':None,'source':None,'notice':None}
        try:
            if symbol.startswith('KR:'):
                data=market.kr.get('/uapi/domestic-stock/v1/quotations/search-stock-info','CTPF1002R',{'PRDT_TYPE_CD':'300','PDNO':symbol[3:]},86400)['output']
                if isinstance(data,list): data=data[0] if data else {}
                result.update(name=data.get('prdt_abrv_name') or data.get('prdt_name') or info['name'],country='한국',industry=data.get('std_idst_clsf_cd_name'),ipo=data.get('scts_mket_lstg_dt') or data.get('kosdaq_mket_lstg_dt'),source='한국투자증권 종목 기본정보')
            else:
                data=market.us.get('/stock/profile2',{'symbol':symbol},86400)
                result.update(name=data.get('name') or info['name'],industry=data.get('finnhubIndustry'),exchange=data.get('exchange'),country=data.get('country'),website=data.get('weburl'),ipo=data.get('ipo'),market_cap=data.get('marketCapitalization'),source='Finnhub 기업 기본정보')
            if not result['industry']: result['notice']='공급자가 업종·사업 소개를 제공하지 않는 종목입니다.'
        except (MarketError,KeyError,TypeError,AttributeError):
            result['notice']='기업 정보를 불러오지 못했습니다. 시세와 주문은 별도로 이용할 수 있습니다.'
        result['dividend']=dividend_info(symbol,market)
        return result
    return cache.get(symbol,3600,load)


def _number(value):
    try:
        number=Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation,TypeError,ValueError): return None


def dividend_info(symbol,market):
    """Annual dividend yield in percent.

    status is 'paid' (yield > 0), 'none' (the provider reports no dividend) or
    'unavailable' (the provider has no dividend data, e.g. many ETFs on
    Finnhub), so a missing record is never shown as "no dividend"."""
    try:
        if symbol.startswith('KR:'):
            # KIS lists each payout; sum the last 12 months of record dates.
            today=date.today();start=today-timedelta(days=365)
            rows=market.kr.get('/uapi/domestic-stock/v1/ksdinfo/dividend','HHKDB669102C0',{'CTS':'','GB1':'0','F_DT':start.strftime('%Y%m%d'),'T_DT':today.strftime('%Y%m%d'),'SHT_CD':symbol[3:],'HIGH_GB':''},86400).get('output1') or []
            total=sum((_number(r.get('per_sto_divi_amt')) or Decimal(0) for r in rows if str(r.get('record_date',''))>=start.strftime('%Y%m%d')),Decimal(0))
            if total<=0: return {'yield':None,'status':'none','basis':'최근 12개월 배당 기록 없음 · 한국투자증권'}
            quote=market.quote(symbol)
            price=_number(quote.get('native_price',quote.get('price')))
            if not price or price<=0: return {'yield':None,'status':'unavailable','basis':'현재가 확인 불가'}
            return {'yield':(total/price*100).quantize(Decimal('.01')),'status':'paid','basis':'최근 12개월 주당 배당금 ÷ 현재가 · 한국투자증권'}
        metric=(market.us.get('/stock/metric',{'symbol':symbol,'metric':'all'},86400) or {}).get('metric') or {}
        keys=('dividendYieldIndicatedAnnual','currentDividendYieldTTM','dividendIndicatedAnnual','dividendPerShareTTM')
        if not any(k in metric for k in keys): return {'yield':None,'status':'unavailable','basis':'공급자 배당 자료 없음'}
        value=_number(metric.get('dividendYieldIndicatedAnnual')) or _number(metric.get('currentDividendYieldTTM'))
        if value and value>0: return {'yield':value.quantize(Decimal('.01')),'status':'paid','basis':'연간 배당수익률 · Finnhub'}
        return {'yield':None,'status':'none','basis':'Finnhub 배당 정보 없음'}
    except (MarketError,KeyError,TypeError,AttributeError,ValueError):
        return {'yield':None,'status':'unavailable','basis':'배당 정보를 불러오지 못했습니다.'}
