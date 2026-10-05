"""시장 › 배당주: the dividend list, highest trailing yield first."""
import threading
from decimal import Decimal as D
from test_service import database, client, register
from app import company, routes
from app.instruments import DIVIDEND_SYMBOLS

YIELDS = {'KO': D('3.10'), 'KR:033780': D('5.20'), 'SCHD': D('3.80')}


def fake_dividends(monkeypatch, calls=None, unavailable=(), gate=None):
    def info(symbol, market, metric=None):
        if gate: gate.wait(5)
        if calls is not None: calls.append(symbol)
        if symbol in unavailable: return {'yield': None, 'status': 'unavailable', 'basis': ''}
        return {'yield': YIELDS.get(symbol), 'status': 'paid' if symbol in YIELDS else 'none', 'basis': ''}
    monkeypatch.setattr(company, 'dividend_info', info)
    monkeypatch.setattr(routes, 'dividend_yields', {})
    monkeypatch.setattr(routes, 'dividend_refresh', None)


def test_rows_run_from_the_highest_yield_and_unknown_yields_go_last(client, monkeypatch):
    gate = threading.Event()   # the providers answer only once the first page is out
    fake_dividends(monkeypatch, gate=gate)
    register(client)
    # The first visit never waits on the providers: yields are looked up in the background.
    first = client.get('/api/explore?asset=dividend').json()
    assert all(r['dividend_pending'] for r in first['rows']) and '확인하는 중' in first['notice']
    gate.set(); routes.dividend_refresh.join(5)
    result = client.get('/api/explore?asset=dividend').json()
    rows = result['rows']
    assert not any(r['dividend_pending'] for r in rows) and '확인하는 중' not in result['notice']
    assert [r['symbol'] for r in rows[:3]] == ['KR:033780', 'SCHD', 'KO']
    assert [r['dividend_yield'] for r in rows[:3]] == [5.2, 3.8, 3.1]
    assert len(rows) == len(DIVIDEND_SYMBOLS) and all(r['dividend_yield'] is None for r in rows[3:])
    assert rows[0]['price'] is not None and rows[0]['market'] == 'KR' and '배당수익률 높은 순' in result['scope']


def test_yields_are_cached_but_a_failed_lookup_is_retried_soon(monkeypatch):
    calls = []
    fake_dividends(monkeypatch, calls, unavailable={'KO'})
    monkeypatch.setattr(routes, 'DIVIDEND_SYMBOLS', ['SCHD', 'KO'])
    clock = [1000.0]
    monkeypatch.setattr(routes.time, 'monotonic', lambda: clock[0])
    def settle():
        routes.known_dividends(None)
        if routes.dividend_refresh: routes.dividend_refresh.join(5)
    settle()
    clock[0] += routes.DIVIDEND_RETRY_TTL + 1
    settle()
    assert calls == ['SCHD', 'KO', 'KO']   # SCHD stays cached for hours; KO is asked again
    # An expired yield is still shown while its new one is looked up.
    clock[0] += routes.DIVIDEND_YIELD_TTL
    assert routes.known_dividends(None)['SCHD'] == (D('3.80'), True)
    routes.dividend_refresh.join(5)
    assert calls[-2:] == ['SCHD', 'KO']
