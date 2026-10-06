"""The ranking's light valuation must agree with the full portfolio figures."""
import time
from decimal import Decimal as D
from sqlalchemy import select
from test_service import database, seed
from app import main
from app.db import Session, User, Wallet, Position
from app.market import MarketError
from app.portfolio import portfolio, ranking_values

KEYS = ('username', 'equity', 'equity_usd', 'return_pct', 'return_pct_usd', 'stale')


class Market:
    def __init__(self, failing=()):
        self.failing, self.calls = set(failing), []
    def quote(self, symbol):
        self.calls.append(symbol)
        if symbol in self.failing: raise MarketError('no price')
        if symbol.startswith('KR:'):
            return {'symbol': symbol, 'price': D('70'), 'native_price': D('70000'), 'timestamp': int(time.time()), 'stale': False}
        return {'symbol': symbol, 'price': D('123.45'), 'timestamp': int(time.time()), 'stale': symbol == 'MSFT'}


def account(name, usd=None, krw=None, positions=(), contributions=D(0)):
    with Session.begin() as db:
        u = User(username=name, password_hash='x', net_contributions_krw=contributions, net_contributions_usd=contributions / 1000)
        db.add(u); db.flush()
        if usd is not None: db.add(Wallet(user_id=u.id, currency='USD', balance=usd))
        if krw is not None: db.add(Wallet(user_id=u.id, currency='KRW', balance=krw))
        for symbol, qty, cost in positions:
            db.add(Position(user_id=u.id, symbol=symbol, quantity=qty, average_cost=cost, native_average_cost=cost))
        return u.id


def test_ranking_values_match_portfolio():
    ids = [account('cash_only', usd=D('100000'), krw=D('0')),
           account('mixed', usd=D('5000'), krw=D('2500000'), positions=[('AAPL', 10, D('100')), ('KR:005930', 3, D('65000'))], contributions=D('50000')),
           account('stale', usd=D('1'), krw=D('0'), positions=[('MSFT', 2, D('300'))]),
           account('no_wallets', positions=[('AAPL', 1, D('90'))])]
    market = Market()
    light = ranking_values(ids, market, main.fx)
    for uid in ids:
        full = portfolio(uid, market, main.fx)
        assert {k: light[uid][k] for k in KEYS} == {k: full[k] for k in KEYS}, full['username']
        assert light[uid]['fx'] == full['fx']


def test_ranking_values_quote_each_symbol_once_and_skip_missing():
    a = account('a', usd=D('10'), krw=D('0'), positions=[('AAPL', 1, D('1')), ('KR:005930', 1, D('1'))])
    b = account('b', usd=D('10'), krw=D('0'), positions=[('AAPL', 2, D('1'))])
    market = Market()
    values = ranking_values([a, b, 999999], market, main.fx)
    assert sorted(market.calls) == ['AAPL', 'KR:005930']
    assert set(values) == {a, b}  # a withdrawn account is left out


def test_ranking_values_unpriced_holding_is_incomplete():
    uid = account('gap', usd=D('10'), krw=D('0'), positions=[('NVDA', 1, D('1'))])
    v = ranking_values([uid], Market(failing={'NVDA'}), main.fx)[uid]
    assert v['equity'] is None and v['equity_usd'] is None and v['return_pct'] is None


def test_ranking_values_fix_the_starting_krw_value_once():
    uid = account('fresh', usd=D('100000'), krw=D('0'))
    ranking_values([uid], Market(), main.fx)
    with Session() as db:
        assert db.get(User, uid).initial_krw == D('100000000')


class Waiting(Market):
    """Each price request waits for the collector (3 s); a batch shares one wait, like MultiMarket.quotes."""
    def __init__(self, failing=()):
        super().__init__(failing); self.waits = 0
    def quote(self, symbol):
        self.waits += 3; return super().quote(symbol)
    def quotes(self, symbols):
        self.waits += 3; out = {}
        for s in symbols:
            try: out[s] = Market.quote(self, s)
            except MarketError as exc: out[s] = exc
        return out


def test_ranking_and_portfolio_read_their_prices_in_one_batch():
    """Read one by one, an idle site's first ranking waited 3 s per held stock under the ranking lock."""
    ids = [account('a', usd=D('5000'), krw=D('2500000'), positions=[('AAPL', 10, D('100')), ('KR:005930', 3, D('65000'))]),
           account('b', usd=D('1'), krw=D('0'), positions=[('MSFT', 2, D('300')), ('NVDA', 1, D('90'))])]
    plain, waiting = ranking_values(ids, Market(), main.fx), Waiting()
    assert ranking_values(ids, waiting, main.fx) == plain and waiting.waits == 3
    waiting = Waiting(failing={'NVDA'})
    view = portfolio(ids[1], waiting, main.fx)
    assert waiting.waits == 3 and view['equity'] is None and view['errors'] == ['NVDA: no price']
    assert [p['value'] for p in view['positions'] if p['symbol'] == 'MSFT'] == [D('246.90')]
