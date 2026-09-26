"""Trade history filters, fees paid, Toss fee defaults and per-holding returns in KRW and USD."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from test_service import database, client, register, FakeMarket  # noqa: F401  (shared fixtures)
from app import main
from app.db import Session, User, Transaction, FxTransaction, engine
from app.money import costs, bps
from app.portfolio import portfolio, dual_costs
from app.migrations import backfill_trade_rates


def uid_of(name='alice'):
    with Session() as db: return db.scalar(select(User.id).where(User.username == name))


def buy(c, token, symbol='AAPL', quantity=1, side='buy'):
    r = c.post('/api/orders', headers={'x-csrf-token': token}, json={'symbol': symbol, 'side': side, 'quantity': quantity, 'request_id': str(uuid4())})
    assert r.status_code == 200, r.text


def test_history_filters_by_side_and_korea_time_month(client):
    token = register(client)
    buy(client, token, quantity=3); buy(client, token, quantity=1, side='sell')
    uid = uid_of()
    # Move the sale to 2026-08-31 23:30 in Korea (14:30 UTC): it belongs to August there.
    with Session.begin() as db:
        sale = db.scalar(select(Transaction).where(Transaction.user_id == uid, Transaction.side == 'sell'))
        sale.created_at = datetime(2026, 8, 31, 14, 30, tzinfo=timezone.utc)
    assert [t['side'] for t in client.get('/api/transactions?side=buy').json()] == ['buy']
    assert [t['side'] for t in client.get('/api/transactions?side=sell').json()] == ['sell']
    assert [t['side'] for t in client.get('/api/transactions?month=2026-08').json()] == ['sell']
    assert client.get('/api/transactions?month=2026-08&side=buy').json() == []
    months = client.get('/api/transactions/months').json()
    assert months[-1] == {'month': '2026-08', 'count': 1} and len(months) == 2
    assert client.get('/api/transactions?month=2026-13').status_code == 422
    assert client.get('/api/transactions?side=hold').status_code == 422


def test_fee_summary_keeps_each_currency_and_totals_in_krw(client, monkeypatch):
    token = register(client)
    monkeypatch.setenv('US_BUY_FEE_BPS', '10'); monkeypatch.setenv('US_SELL_FEE_BPS', '10')
    buy(client, token, quantity=10)                 # $1,000 gross -> $1 fee
    buy(client, token, quantity=5, side='sell')     # $500 gross -> $0.50 fee
    uid = uid_of()
    with Session.begin() as db:
        db.add(FxTransaction(user_id=uid, request_id=str(uuid4()), source='KRW', target='USD', amount=D(100000), received=D(70),
                             fee=D(100), rate=D('.0007'), fee_bps=D(10), spread_bps=D(5), rate_date='2026-09-25',
                             created_at=datetime.now(timezone.utc)))
    r = client.get('/api/fees').json()
    usd, krw = r['paid']['USD'], r['paid']['KRW']
    assert (D(usd['buy_fee']), D(usd['sell_fee']), D(usd['tax']), D(usd['total']), usd['trades']) == (1, D('.5'), 0, D('1.5'), 2)
    assert (D(krw['fx_fee']), D(krw['total']), krw['exchanges']) == (100, 100, 1)
    assert D(r['total_krw']) == 100 + D('1.5') * 1000        # the test FX is 1,000 KRW per USD
    assert D(r['rates']['US_BUY_FEE_BPS']) == 10


def test_toss_defaults_and_korean_etfs_skip_the_sell_tax(monkeypatch):
    for name in ('US_BUY_FEE_BPS', 'US_SELL_FEE_BPS', 'KR_BUY_FEE_BPS', 'KR_SELL_FEE_BPS', 'KR_SELL_TAX_BPS'):
        monkeypatch.delenv(name, raising=False)
    assert [bps(n) for n in ('US_BUY_FEE_BPS', 'US_SELL_FEE_BPS', 'KR_BUY_FEE_BPS', 'KR_SELL_FEE_BPS', 'KR_SELL_TAX_BPS')] == [10, 10, D('1.5'), D('1.5'), 20]
    us = costs('AAPL', 'sell', D('200'), 10)                  # $2,000
    assert (us['fee'], us['tax'], us['net_amount']) == (D(2), 0, D(1998))
    stock = costs('KR:005930', 'sell', D(80000), 10)           # 800,000원: fee 120원, tax 1,600원
    assert (stock['fee'], stock['tax'], stock['net_amount']) == (120, 1600, 798280)
    etf = costs('KR:114260', 'sell', D(100000), 10)            # catalog bond ETF: no transaction tax
    assert (etf['fee'], etf['tax']) == (150, 0)
    assert costs('KR:005930', 'buy', D(80000), 10)['tax'] == 0


class RatedMarket(FakeMarket):
    """FakeMarket whose reference rate is 1,300 KRW per USD at the fill."""
    fx = SimpleNamespace(krw_to_usd=lambda: (D(1) / D(1300), '2026-09-24'))


def test_holding_return_in_krw_includes_the_rate_move(client, monkeypatch):
    monkeypatch.setattr(main, 'market', RatedMarket())
    token = register(client)
    buy(client, token, quantity=2)
    uid = uid_of()
    with Session() as db: assert db.scalar(select(Transaction.usd_krw).where(Transaction.user_id == uid)) == 1300
    # Valued at the test FX of 1,000 KRW per USD: the price did not move, the won strengthened.
    row = portfolio(uid, FakeMarket(), main.fx)['positions'][0]
    usd, krw = row['basis']['USD'], row['basis']['KRW']
    assert (usd['average_cost'], usd['pnl'], usd['return_pct']) == (100, 0, 0) and row['return_pct'] == 0
    assert (krw['average_cost'], krw['pnl']) == (130000, -60000)
    assert float(krw['return_pct']) == pytest.approx((1000 / 1300 - 1) * 100)


def test_dual_costs_average_out_sales_and_drop_unknown_rates():
    t = lambda side, qty, net, rate, symbol='AAPL', currency='USD': SimpleNamespace(symbol=symbol, side=side, quantity=qty, net_amount=D(net), usd_krw=None if rate is None else D(rate), currency=currency)
    book = dual_costs([t('buy', 10, 1000, 1300), t('buy', 10, 1000, 1400), t('sell', 5, 600, 1350),
                       t('buy', 1, 70000, 1400, 'KR:005930', 'KRW'), t('buy', 1, 100, None, 'MSFT')])
    assert book['AAPL']['quantity'] == 15 and book['AAPL']['USD'] == 1500 and book['AAPL']['KRW'] == D(2700000) * 15 / 20
    assert book['KR:005930']['USD'] == D(50) and book['KR:005930']['KRW'] == 70000
    assert 'MSFT' not in book


def test_backfill_uses_the_rate_published_before_each_fill():
    with Session.begin() as db:
        u = User(username='old', password_hash='x', initial_usd=D(100000), initial_krw=D(137000000), initial_fx_date='2026-09-23')
        db.add_all([u, User(username='newer', password_hash='x', initial_usd=D(100000), initial_krw=D(136860000), initial_fx_date='2026-09-24')])
        db.flush(); uid = u.id
        for at in (datetime(2026, 9, 24, 13, 0, tzinfo=timezone.utc),     # before the 9-24 rate came out
                   datetime(2026, 9, 25, 3, 0, tzinfo=timezone.utc)):     # after it
            db.add(Transaction(user_id=uid, request_id=str(uuid4()), symbol='AAPL', side='buy', quantity=1, price=D(1),
                               native_price=D(1), currency='USD', quote_time=at, created_at=at, net_amount=D(1)))
    with engine.begin() as db:
        backfill_trade_rates(db)
    with Session() as db:
        rates = list(db.scalars(select(Transaction.usd_krw).where(Transaction.user_id == uid).order_by(Transaction.id)))
    assert rates == [D(1370), D('1368.6')]
