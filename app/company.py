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
        # Dividend and valuation read the same Finnhub metric response.
        metric=_once(lambda:_us_metric(symbol,market))
        result['dividend']=dividend_info(symbol,market,metric)
        result['valuation']=valuation_info(symbol,market,result['market_cap'],metric)
        return result
    return cache.get(symbol,3600,load)


def _number(value):
    try:
        number=Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation,TypeError,ValueError): return None


def _us_metric(symbol,market):
    return (market.us.get('/stock/metric',{'symbol':symbol,'metric':'all'},86400) or {}).get('metric') or {}


def _once(load):
    """load() at most once: later calls return, or re-raise, the first outcome."""
    outcome=[]
    def get():
        if not outcome:
            try: outcome.append((True,load()))
            except Exception as exc: outcome.append((False,exc))
        ok,value=outcome[0]
        if ok: return value
        raise value
    return get


def dividend_info(symbol,market,metric=None):
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
        metric=metric() if metric else _us_metric(symbol,market)
        keys=('dividendYieldIndicatedAnnual','currentDividendYieldTTM','dividendIndicatedAnnual','dividendPerShareTTM')
        if not any(k in metric for k in keys): return {'yield':None,'status':'unavailable','basis':'공급자 배당 자료 없음'}
        value=_number(metric.get('dividendYieldIndicatedAnnual')) or _number(metric.get('currentDividendYieldTTM'))
        if value and value>0: return {'yield':value.quantize(Decimal('.01')),'status':'paid','basis':'연간 배당수익률 · Finnhub'}
        return {'yield':None,'status':'none','basis':'Finnhub 배당 정보 없음'}
    except (MarketError,KeyError,TypeError,AttributeError,ValueError):
        return {'yield':None,'status':'unavailable','basis':'배당 정보를 불러오지 못했습니다.'}


KR_PRICE='/uapi/domestic-stock/v1/quotations/inquire-price'
KR_RATIO='/uapi/domestic-stock/v1/finance/financial-ratio'
VALUATION_NOTICE='공급자가 제공하지 않는 지표는 정보 없음으로 표시합니다. ETF 등은 재무 지표가 없을 수 있습니다.'


def _multiple(value):
    """A provider multiple; KIS reports a missing PER/PBR as 0, so 0 is missing too."""
    number=_number(value)
    return number.quantize(Decimal('.01')) if number else None


def _fiscal_year_row(rows):
    """The newest full fiscal year among KIS annual ratio rows.

    The first annual row can be the current year to date (202606 before the
    202512 rows of a December company); the fiscal year-end month is the month
    most rows share."""
    rows=[r for r in rows if isinstance(r,dict) and len(str(r.get('stac_yymm','')))==6]
    if not rows: return None
    months=[str(r['stac_yymm'])[4:] for r in rows]
    month=max(dict.fromkeys(months),key=months.count)
    return next(r for r in rows if str(r['stac_yymm']).endswith(month))


def valuation_info(symbol,market,profile_cap=None,metric=None):
    """Market cap in native currency units and PER/PBR/ROE/PSR; ROE is in percent.

    Each figure is None when the provider does not report it (ETF, new
    listing, provider outage), and a failed call only blanks its own figures."""
    kr=symbol.startswith('KR:')
    result={'currency':'KRW' if kr else 'USD','market_cap':None,'per':None,'pbr':None,'roe':None,'psr':None,'basis':None,'notice':None}
    try:
        if kr:
            code=symbol[3:]
            # The same request (and cache entry) as the quote capability check.
            price=market.kr.get(KR_PRICE,'FHKST01010100',{'FID_COND_MRKT_DIV_CODE':'J','FID_INPUT_ISCD':code},3600).get('output') or {}
            cap=_number(price.get('hts_avls'))  # 억원
            result.update(market_cap=(cap*100000000).quantize(Decimal(1)) if cap and cap>0 else None,per=_multiple(price.get('per')),pbr=_multiple(price.get('pbr')),basis='한국투자증권 · 현재가와 최근 결산 재무 기준')
            try:
                row=_fiscal_year_row(market.kr.get(KR_RATIO,'FHKST66430300',{'FID_DIV_CLS_CODE':'0','fid_cond_mrkt_div_code':'J','fid_input_iscd':code},86400).get('output') or [])
            except MarketError: row=None
            if row:
                roe,sps,now=_number(row.get('roe_val')),_number(row.get('sps')),_number(price.get('stck_prpr'))
                result['roe']=roe.quantize(Decimal('.01')) if roe is not None else None
                result['psr']=(now/sps).quantize(Decimal('.01')) if now and now>0 and sps and sps>0 else None
                period=str(row['stac_yymm'])
                result['basis']=f'한국투자증권 · 현재가와 {period[:4]}.{period[4:]} 결산 재무 기준'
        else:
            metric=metric() if metric else _us_metric(symbol,market)
            cap=_number(metric.get('marketCapitalization')) or _number(profile_cap)  # USD millions
            roe=_number(metric.get('roeTTM'))
            result.update(market_cap=(cap*1000000).quantize(Decimal(1)) if cap and cap>0 else None,per=_multiple(metric.get('peTTM')),pbr=_multiple(metric.get('pbQuarterly')),roe=roe.quantize(Decimal('.01')) if roe is not None else None,psr=_multiple(metric.get('psTTM')),basis='Finnhub · PER·ROE·PSR 최근 12개월, PBR 최근 분기 기준')
    except (MarketError,KeyError,TypeError,AttributeError,ValueError):
        result['basis']='투자 지표를 불러오지 못했습니다. 시세와 주문은 별도로 이용할 수 있습니다.'
        return result
    if any(result[k] is None for k in ('market_cap','per','pbr','roe','psr')): result['notice']=VALUATION_NOTICE
    return result
