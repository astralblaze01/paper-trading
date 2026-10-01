"""Cash dividends: the KIS schedule, who is entitled at the cut-off, and crediting wallets once."""
from datetime import date, datetime, timezone
from decimal import Decimal as D
from uuid import uuid4

from sqlalchemy import select

from test_service import database, client, register, FakeMarket  # noqa: F401  (shared fixtures)
from app import dividends
from app.db import Session, User, Transaction, Wallet, DividendEvent, DividendPayment


def uid_of(name='alice'):
    with Session() as db: return db.scalar(select(User.id).where(User.username == name))


def trade(c, token, symbol='AAPL', quantity=1, side='buy'):
    r = c.post('/api/orders', headers={'x-csrf-token': token}, json={'symbol': symbol, 'side': side, 'quantity': quantity, 'request_id': str(uuid4())})
    assert r.status_code == 200, r.text


def backdate(uid, side, at):
    with Session.begin() as db:
        for t in db.scalars(select(Transaction).where(Transaction.user_id == uid, Transaction.side == side)): t.created_at = at


def usd(uid):
    with Session() as db: return db.get(Wallet, (uid, 'USD')).balance


def test_cutoff_is_the_last_day_a_purchase_settles_by_the_record_date():
    # KRX settles T+2: record Wed 2026-09-30 → buy by Mon 09-28, counted at the end of that day in Korea.
    assert dividends.cutoff('KR:005930', date(2026, 9, 30)) == datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc)
    # Over a weekend: record Mon 2026-10-05 → Thu 10-01.
    assert dividends.cutoff('KR:005930', date(2026, 10, 5)) == datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)
    # US settles T+1: record Mon 2026-11-09 → buy by Fri 11-06, end of day in New York (EST, UTC-5).
    assert dividends.cutoff('AAPL', date(2026, 11, 9)) == datetime(2026, 11, 7, 5, 0, tzinfo=timezone.utc)
    assert dividends.credit_day('AAPL', date(2026, 11, 12)) == date(2026, 11, 13)      # the next morning in Korea
    assert dividends.credit_day('KR:005930', date(2026, 11, 20)) == date(2026, 11, 20)


def test_schedule_reads_kis_korean_and_us_dividends():
    class KIS:
        def get(self, path, tr, params, ttl):
            if path.endswith('ksdinfo/dividend'):
                return {'output1': [
                    {'record_date': '20260630', 'sht_cd': '005930', 'per_sto_divi_amt': '374', 'divi_pay_dt': '2026/08/28'},
                    {'record_date': '20260930', 'sht_cd': '005930', 'per_sto_divi_amt': '0', 'divi_pay_dt': ''},          # not announced yet
                    {'record_date': '20261231', 'sht_cd': '005930', 'per_sto_divi_amt': '400', 'divi_pay_dt': ''}]}       # amount, no pay date yet
            if path.endswith('period-rights'):
                return {'output': [{'pdno': 'AAPL', 'acpl_bass_dt': '20261109', 'alct_frcr_unpr': '0.26000', 'crcy_cd': 'USD'},
                                   {'pdno': 'AAPL', 'acpl_bass_dt': '20260209', 'alct_frcr_unpr': '0.00000', 'crcy_cd': 'USD'}]}
            return {'output1': [{'ca_title': '현금배당', 'record_dt': '20261109', 'pay_dt': '20261112'},
                                {'ca_title': '주식분할', 'record_dt': '20261109', 'pay_dt': '20261201'}]}
    kr = dividends.schedule('KR:005930', KIS(), date(2026, 10, 1))
    assert [(e['record_date'], e['pay_date'], e['per_share'], e['currency']) for e in kr] == [
        (date(2026, 6, 30), date(2026, 8, 28), D(374), 'KRW'), (date(2026, 12, 31), None, D(400), 'KRW')]
    us = dividends.schedule('AAPL', KIS(), date(2026, 10, 1))
    assert [(e['record_date'], e['pay_date'], e['per_share'], e['currency']) for e in us] == [(date(2026, 11, 9), date(2026, 11, 12), D('0.26'), 'USD')]


def test_holders_at_the_cutoff_are_paid_once_after_withholding(client):
    token = register(client)
    trade(client, token, quantity=10)
    uid = uid_of()
    backdate(uid, 'buy', datetime(2026, 11, 2, tzinfo=timezone.utc))
    # Sold 4 on the ex-date (after the cut-off): those 4 still earn the dividend.
    trade(client, token, quantity=4, side='sell')
    backdate(uid, 'sell', datetime(2026, 11, 9, 15, tzinfo=timezone.utc))
    with Session.begin() as db:
        db.add(DividendEvent(symbol='AAPL', record_date=date(2026, 11, 9), pay_date=date(2026, 11, 12), per_share=D('0.26'), currency='USD', source='test'))
    before = usd(uid)
    # Not yet: US dividends land the day after the pay date, Korea time.
    assert dividends.pay_due(datetime(2026, 11, 12, 12, tzinfo=timezone.utc)) == 0
    assert dividends.pay_due(datetime(2026, 11, 12, 16, tzinfo=timezone.utc)) == 1    # 11-13 01:00 in Korea
    # 10 × $0.26 = $2.60; 15% withheld = $0.39.
    assert usd(uid) - before == D('2.21')
    with Session() as db:
        p = db.scalar(select(DividendPayment))
        assert (p.quantity, p.gross, p.tax, p.net, p.tax_bps) == (10, D('2.6'), D('.39'), D('2.21'), 1500)
        assert db.get(User, uid).cash == usd(uid)                                     # legacy mirror kept
    assert dividends.pay_due(datetime(2026, 11, 14, tzinfo=timezone.utc)) == 0        # never twice
    assert usd(uid) - before == D('2.21')
    body = client.get('/api/dividends').json()
    assert [r['symbol'] for r in body['payments']] == ['AAPL'] and D(str(body['totals']['USD']['net'])) == D('2.21')


def test_buying_after_the_cutoff_earns_nothing_and_old_dividends_are_not_backdated(client):
    token = register(client)
    trade(client, token, quantity=5)
    uid = uid_of()
    backdate(uid, 'buy', datetime(2026, 11, 7, 6, tzinfo=timezone.utc))            # after the 11-06 New York close day
    with Session.begin() as db:
        db.add(DividendEvent(symbol='AAPL', record_date=date(2026, 11, 9), pay_date=date(2026, 11, 12), per_share=D('0.26'), currency='USD', source='test'))
        # Paid before the feature started: never credited retroactively.
        db.add(DividendEvent(symbol='AAPL', record_date=date(2026, 8, 11), pay_date=date(2026, 8, 14), per_share=D('0.26'), currency='USD', source='test'))
    before = usd(uid)
    assert dividends.pay_due(datetime(2026, 11, 20, tzinfo=timezone.utc)) == 0
    assert usd(uid) == before


def test_upcoming_dividends_show_the_expected_amount(client):
    token = register(client)
    trade(client, token, symbol='AAPL', quantity=3)
    with Session.begin() as db:
        db.add(DividendEvent(symbol='AAPL', record_date=date(2099, 11, 9), pay_date=None, per_share=D('0.50'), currency='USD', source='test'))
    body = client.get('/api/dividends').json()
    row = body['upcoming'][0]
    # 3 × $0.50 = $1.50; 15% is $0.225, withheld to the cent below: $0.22.
    assert (row['symbol'], row['quantity'], D(str(row['expected_net'])), row['status']) == ('AAPL', 3, D('1.28'), '보유 시 지급')
    assert body['rates'] == {'KR_DIVIDEND_TAX_BPS': 1540, 'US_DIVIDEND_TAX_BPS': 1500}


def test_schedule_is_read_a_few_listings_per_pass_and_waits_out_request_limits(client, monkeypatch):
    from app.market import MarketError
    symbols = ['AAPL', 'KR:005930', 'MSFT', 'NVDA', 'VOO']
    monkeypatch.setattr(dividends, 'tracked_symbols', lambda db, now: symbols)
    asked, refuse = [], set()
    def schedule(symbol, kis, today, lookback=0):
        if symbol in refuse: raise MarketError('국내 시세 요청 한도 대기 중입니다.')
        asked.append(symbol); return []
    monkeypatch.setattr(dividends, 'schedule', schedule)
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert dividends.run(None, start)['synced'] == 3 and asked == ['AAPL', 'KR:005930', 'MSFT']
    refuse.add('NVDA')                     # the shared KIS budget is spent: stop, retry next pass
    assert dividends.run(None, start)['synced'] == 0 and asked == ['AAPL', 'KR:005930', 'MSFT']
    refuse.clear()
    assert dividends.run(None, start)['synced'] == 2 and asked[3:] == ['NVDA', 'VOO']
    assert dividends.run(None, start)['synced'] == 0 and len(asked) == 5        # round done
    later = datetime(2026, 10, 1, 6, 1, tzinfo=timezone.utc)                 # SYNC_HOURS later: a new round
    assert dividends.run(None, later)['synced'] == 3 and asked[5:] == ['AAPL', 'KR:005930', 'MSFT']
