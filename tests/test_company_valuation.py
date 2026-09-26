"""Company valuation metrics: market cap, PER, PBR, ROE (percent) and PSR.

Provider figures are recorded shapes from live responses (2026-09-26):
Finnhub /stock/metric reports marketCapitalization in USD millions and roeTTM
already in percent (AAPL 137.18); KIS inquire-price reports hts_avls in 100M
KRW and PER/PBR 0.00 for an ETF; KIS financial-ratio reports roe_val in
percent and can list the current year to date before the last fiscal year.
"""
from decimal import Decimal as D

import pytest
from test_service import database, client, register
from app import main
from app.company import cache, company_info, valuation_info, _fiscal_year_row
from app.market import MarketError

AAPL = {'marketCapitalization': 4917946.5, 'peTTM': 38.1443, 'pbQuarterly': 38.486, 'pbAnnual': 50.978,
        'roeTTM': 137.18, 'roeRfy': 151.91, 'psTTM': 10.5349, 'dividendYieldIndicatedAnnual': 0.41}
SAMSUNG_PRICE = {'stck_prpr': '286500', 'hts_avls': '16749588', 'per': '43.65', 'pbr': '4.48'}
SAMSUNG_RATIO = [{'stac_yymm': '202606', 'roe_val': '31.39', 'sps': '72276'},
                 {'stac_yymm': '202512', 'roe_val': '10.85', 'sps': '49471'},
                 {'stac_yymm': '202412', 'roe_val': '9.03', 'sps': '44000'}]
ETF_PRICE = {'stck_prpr': '113145', 'hts_avls': '260516', 'per': '0.00', 'pbr': '0.00'}
EMPTY = {'market_cap': None, 'per': None, 'pbr': None, 'roe': None, 'psr': None}


class US:
    """Finnhub adapter answering from per-path dicts; counts every call."""
    def __init__(self, metric=None, profile=None, fail=()):
        self.metric, self.profile, self.fail, self.calls = metric, profile or {}, fail, []
    def get(self, path, params, ttl):
        self.calls.append(path)
        if path in self.fail: raise MarketError('offline')
        return {'metric': self.metric} if path == '/stock/metric' else self.profile


class KR:
    """KIS adapter: responses keyed by path; records (path, tr_id, params, ttl)."""
    def __init__(self, price=None, ratio=None, fail=()):
        self.price, self.ratio, self.fail, self.calls = price or {}, ratio or [], fail, []
    def get(self, path, tr_id, params, ttl=15, tr_cont=''):
        self.calls.append((path, tr_id, params, ttl))
        if any(path.endswith(f) for f in self.fail): raise MarketError('offline')
        if path.endswith('inquire-price'): return {'output': self.price}
        if path.endswith('financial-ratio'): return {'output': self.ratio}
        if path.endswith('dividend'): return {'output1': []}
        return {'output': {}}


def market(us=None, kr=None):
    return type('M', (), {'us': us, 'kr': kr, 'quote': lambda self, s: {'native_price': D(286500)}})()


def figures(v):
    return {k: v[k] for k in EMPTY}


def test_us_large_cap_maps_each_finnhub_metric_to_its_own_figure():
    v = valuation_info('AAPL', market(US(AAPL)))
    assert figures(v) == {'market_cap': D('4917946500000'), 'per': D('38.14'), 'pbr': D('38.49'), 'roe': D('137.18'), 'psr': D('10.53')}
    assert v['currency'] == 'USD' and v['notice'] is None
    assert v['basis'] == 'Finnhub · PER·ROE·PSR 최근 12개월, PBR 최근 분기 기준'


def test_roe_is_already_a_percentage_and_is_never_rescaled():
    # 31.5 means 31.5 %, not 0.315 % and not 3150 %.
    assert valuation_info('MSFT', market(US({'roeTTM': 31.5})))['roe'] == D('31.50')
    assert valuation_info('KR:005930', market(kr=KR(SAMSUNG_PRICE, [{'stac_yymm': '202512', 'roe_val': '0.32', 'sps': '1'}])))['roe'] == D('0.32')


def test_us_etf_has_no_figures_and_no_error():
    v = valuation_info('SPY', market(US({'52WeekHigh': 700})))
    assert figures(v) == EMPTY and v['notice'] and 'ETF' in v['notice']


def test_us_partial_metrics_keep_what_the_provider_reports():
    v = valuation_info('RKLB', market(US({'marketCapitalization': 125400, 'peTTM': -12.3, 'psTTM': 20.1})))
    assert figures(v) == {'market_cap': D('125400000000'), 'per': D('-12.30'), 'pbr': None, 'roe': None, 'psr': D('20.10')}
    assert v['notice']


def test_us_market_cap_falls_back_to_the_profile_figure():
    assert valuation_info('NEWCO', market(US({})), profile_cap=1234.5)['market_cap'] == D('1234500000')


def test_us_metric_outage_blanks_only_the_valuation():
    v = valuation_info('AAPL', market(US(fail=('/stock/metric',))))
    assert figures(v) == EMPTY and '불러오지 못했습니다' in v['basis']


def test_korean_large_cap_uses_kis_price_ratios_and_the_last_full_fiscal_year():
    kis = KR(SAMSUNG_PRICE, SAMSUNG_RATIO)
    v = valuation_info('KR:005930', market(kr=kis))
    # 16,749,588 억원; PSR = 286,500 / 49,471 (FY2025 SPS, the same year as KIS PER/PBR); ROE FY2025.
    assert figures(v) == {'market_cap': D('1674958800000000'), 'per': D('43.65'), 'pbr': D('4.48'), 'roe': D('10.85'), 'psr': D('5.79')}
    assert v['currency'] == 'KRW' and v['notice'] is None and '2025.12 결산' in v['basis']


def test_korean_price_request_is_the_cached_capability_request():
    """No second inquire-price call: the valuation asks with the exact key capability() uses."""
    from app import kr_quotes
    from app.redis_cache import redis_cache
    kis = KR(SAMSUNG_PRICE, SAMSUNG_RATIO)
    original = redis_cache.get_json
    redis_cache.get_json = lambda key: None
    try: kr_quotes.capability(kis, 'KR:005930')
    finally: redis_cache.get_json = original
    capability_call = kis.calls[0]
    kis.calls.clear()
    valuation_info('KR:005930', market(kr=kis))
    assert kis.calls[0] == capability_call
    assert [c[0].rsplit('/', 1)[1] for c in kis.calls] == ['inquire-price', 'financial-ratio'] and kis.calls[1][3] == 86400


def test_korean_etf_keeps_market_cap_and_reports_zero_ratios_as_missing():
    v = valuation_info('KR:069500', market(kr=KR(ETF_PRICE, [])))
    assert figures(v) == {'market_cap': D('26051600000000'), 'per': None, 'pbr': None, 'roe': None, 'psr': None}
    assert v['notice']


def test_korean_ratio_outage_keeps_the_price_based_figures():
    v = valuation_info('KR:005930', market(kr=KR(SAMSUNG_PRICE, fail=('financial-ratio',))))
    assert figures(v) == {'market_cap': D('1674958800000000'), 'per': D('43.65'), 'pbr': D('4.48'), 'roe': None, 'psr': None}


def test_korean_price_outage_blanks_the_valuation():
    v = valuation_info('KR:005930', market(kr=KR(fail=('inquire-price',))))
    assert figures(v) == EMPTY and '불러오지 못했습니다' in v['basis']


@pytest.mark.parametrize('rows,period', [
    (SAMSUNG_RATIO, '202512'),                                                            # current year to date first
    ([{'stac_yymm': '202512'}, {'stac_yymm': '202503'}, {'stac_yymm': '202403'}], '202503'),  # March fiscal year
    ([{'stac_yymm': '202512'}], '202512'),                                                # a single (new listing) year
])
def test_fiscal_year_row(rows, period):
    assert _fiscal_year_row(rows)['stac_yymm'] == period


def test_fiscal_year_row_without_data():
    assert _fiscal_year_row([]) is None and _fiscal_year_row([{'roe_val': '1'}]) is None


def test_company_info_reads_one_metric_response_for_dividend_and_valuation():
    cache.values.clear()
    us = US(AAPL, {'name': 'Apple Inc', 'finnhubIndustry': 'Technology', 'marketCapitalization': 4902476.68})
    info = company_info('AAPL', market(us))
    assert us.calls == ['/stock/profile2', '/stock/metric']
    assert info['dividend']['status'] == 'paid' and info['valuation']['per'] == D('38.14')
    assert info['market_cap'] == 4902476.68 and info['industry'] == 'Technology'   # existing fields unchanged


def test_company_info_survives_every_provider_failing():
    cache.values.clear()
    info = company_info('KR:005930', market(kr=KR(fail=('search-stock-info', 'inquire-price', 'financial-ratio', 'dividend'))))
    assert info['notice'] and figures(info['valuation']) == EMPTY and info['name'] == '삼성전자'


def test_company_endpoint_sends_percent_and_multiples_as_numbers(client, monkeypatch):
    cache.values.clear()
    register(client)
    monkeypatch.setattr(main.market, 'us', US({'marketCapitalization': 3200000, 'peTTM': 28.4, 'pbQuarterly': 7.2, 'roeTTM': 31.5, 'psTTM': 8.6}), raising=False)
    body = client.get('/api/company/MSFT').json()
    assert {k: float(body['valuation'][k]) for k in EMPTY} == {'market_cap': 3.2e12, 'per': 28.4, 'pbr': 7.2, 'roe': 31.5, 'psr': 8.6}
    assert body['valuation']['currency'] == 'USD' and body['name'] == 'Microsoft'


def test_the_kis_client_allows_only_the_financial_ratio_finance_path():
    from app.multi_market import KoreaPrices
    kis = KoreaPrices(); kis.configured = False
    with pytest.raises(MarketError, match='설정 필요'):
        kis.get('/uapi/domestic-stock/v1/finance/financial-ratio', 'FHKST66430300', {'fid_input_iscd': '005930'}, 86400)
    with pytest.raises(MarketError, match='허용되지 않은'):
        kis.get('/uapi/domestic-stock/v1/finance/income-statement', 'FHKST66430200', {}, 86400)
