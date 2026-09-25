"""Curated discovery list; Korean securities outside it can be looked up by code."""
import re

SYMBOL_PATTERN = r'^(?:[A-Z][A-Z0-9-]{0,14}|KR:[0-9]{6})$'
CATEGORIES = {'all', 'us', 'kr', 'us_bond', 'kr_bond', 'gold'}
CATALOG = [
    ('AAPL', 'Apple', 'us', 'USD'),
    ('MSFT', 'Microsoft', 'us', 'USD'),
    ('NVDA', 'NVIDIA', 'us', 'USD'),
    ('KR:005930', '삼성전자', 'kr', 'KRW'),
    ('KR:000660', 'SK하이닉스', 'kr', 'KRW'),
    ('KR:005380', '현대차', 'kr', 'KRW'),
    ('KR:035420', 'NAVER', 'kr', 'KRW'),
    ('SHY', 'iShares 미국 국채 1–3년 ETF', 'us_bond', 'USD'),
    ('IEF', 'iShares 미국 국채 7–10년 ETF', 'us_bond', 'USD'),
    ('TLT', 'iShares 미국 국채 20년 이상 ETF', 'us_bond', 'USD'),
    ('SGOV', 'iShares 미국 단기 국채 ETF', 'us_bond', 'USD'),
    ('BIL', 'SPDR 미국 초단기 국채 ETF', 'us_bond', 'USD'),
    ('VGSH', 'Vanguard 미국 단기 국채 ETF', 'us_bond', 'USD'),
    ('VGIT', 'Vanguard 미국 중기 국채 ETF', 'us_bond', 'USD'),
    ('VGLT', 'Vanguard 미국 장기 국채 ETF', 'us_bond', 'USD'),
    ('GOVT', 'iShares 미국 국채 ETF', 'us_bond', 'USD'),
    ('KR:114260', 'Kodex 국고채3년 ETF', 'kr_bond', 'KRW'),
    ('KR:153130', 'Kodex 단기채권 ETF', 'kr_bond', 'KRW'),
    ('KR:152380', 'Kodex 국채선물10년 ETF', 'kr_bond', 'KRW'),
    ('KR:471230', 'Kodex 국고채10년액티브 ETF', 'kr_bond', 'KRW'),
    ('GLD', 'SPDR Gold Shares · 금 ETF', 'gold', 'USD'),
    ('IAU', 'iShares Gold Trust · 금 ETF', 'gold', 'USD'),
    ('GLDM', 'SPDR Gold MiniShares · 금 ETF', 'gold', 'USD'),
    ('KR:411060', 'ACE KRX금현물 ETF', 'gold', 'KRW'),
]

ALIASES = {
    'AAPL': ('애플',), 'MSFT': ('마이크로소프트',), 'NVDA': ('엔비디아',),
    'KR:005930': ('삼성 전자',), 'KR:000660': ('에스케이하이닉스','SK 하이닉스'),
    'KR:005380': ('현대 자동차',), 'KR:035420': ('네이버',),
    'GLD': ('금','골드'), 'IAU': ('금','골드'), 'GLDM': ('금','골드'),
}

def _search_text(value):
    return re.sub(r'[\s._-]+','',value.casefold())

def market_of(symbol):
    """'KR' for a Korean listing (KR:######), otherwise 'US'."""
    return 'KR' if symbol.startswith('KR:') else 'US'

def currency_of(symbol):
    """The settlement currency: KRW for Korean listings, otherwise USD."""
    return 'KRW' if symbol.startswith('KR:') else 'USD'

def instrument(symbol):
    for code, name, category, currency in CATALOG:
        if code == symbol:
            return dict(symbol=code, name=name, category=category, currency=currency)
    return dict(symbol=symbol, name=symbol, category='kr' if symbol.startswith('KR:') else 'us', currency=currency_of(symbol))

def discover(query='', category='all'):
    query = _search_text(query.strip())
    return [instrument(row[0]) for row in CATALOG if (category == 'all' or row[2] == category) and
            (not query or any(query in _search_text(value) for value in (row[0],row[1],*ALIASES.get(row[0],()))))]

def valid_symbol(symbol):
    return re.fullmatch(SYMBOL_PATTERN, symbol) is not None
