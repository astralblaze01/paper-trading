"""Read-only provider company facts; never fabricate a business description."""
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
        return result
    return cache.get(symbol,3600,load)
