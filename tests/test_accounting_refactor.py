"""Characterization tests for accounting and admin behaviour: ranks, weekly
baselines, order replays, legacy cost basis, admin audits and grants."""
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select, func
from test_service import database, client, register, seed, order, FakeMarket
from app import main
from app.db import (Session, User, Wallet, Position, Transaction, LimitOrder, Watchlist, AdminAudit, SeasonArchive,
                    WeeklyState, WeeklyReport, PerformanceSnapshot, SiteNotice)
from app.market import MarketError
from app.trading import execute_order, preview_order
from app.portfolio import portfolio, initialize_equity
from app.performance_snapshots import snapshot_user
from app.accounts import delete_account_data
from app.weekly import standings, report_list
from app.routes import curated_market_rows

RETRY_MISMATCH = '재시도 요청 내용이 다릅니다.'
GRANT_INVALID = '지원금과 통화별 최소 단위를 확인하세요.'


def today():
    return datetime.now(timezone.utc).date().isoformat()


def admin(client):
    token = register(client, 'operator')
    with Session.begin() as db:
        me = db.scalar(select(User).where(User.username == 'operator')); me.is_admin = True
        return {'x-csrf-token': token}, me.id


def member(name='investor', **fields):
    with Session.begin() as db:
        u = User(username=name, password_hash='unused', **fields); db.add(u); db.flush()
        return u.id


def manage(client, headers, target, body):
    return client.post(f'/api/admin/users/{target}/manage', headers=headers, json=body)


def signature(body):
    """ManagementInput.model_dump(mode='json') for a request body."""
    return {'action': body['action'], 'currency': body.get('currency', 'USD'), 'amount': body.get('amount', '0'),
            'reason': body.get('reason', ''), 'confirmation': body.get('confirmation', ''), 'request_id': body['request_id']}


def audits():
    with Session() as db: return list(db.scalars(select(AdminAudit).order_by(AdminAudit.id)))


def balances(uid):
    with Session() as db:
        return {w.currency: w.balance for w in db.scalars(select(Wallet).where(Wallet.user_id == uid))}


def recent(stamp):
    return abs((datetime.now(timezone.utc) - stamp).total_seconds()) < 60


def is_uuid4(value):
    return UUID(value).version == 4


def weekly_report(rows, days_ago=0):
    end = datetime.now(timezone.utc) - timedelta(days=days_ago)
    with Session.begin() as db:
        report = WeeklyReport(scheduled_for=end, period_start=end - timedelta(days=7), period_end=end, rows=rows,
                              notes={'excluded_new_or_zero': 0, 'late': False, 'quotes': {}})
        db.add(report); db.flush()
        return report.id


def set_baseline(baseline):
    with Session.begin() as db:
        db.add(WeeklyState(id=1, baseline=baseline, next_due=datetime.now(timezone.utc) + timedelta(days=7)))


def weekly_baseline():
    with Session() as db: return db.get(WeeklyState, 1).baseline


# Competition ranks -------------------------------------------------------------

def test_standings_share_ranks_on_equal_weekly_return():
    baseline = {'1': {'equity': '100'}, '2': {'equity': '100'}, '3': {'equity': '100'}, '4': {'equity': '200'},
                '5': {'equity': '0'}}
    accounts = {'1': {'username': 'b', 'equity': '110'}, '2': {'username': 'a', 'equity': '110'},
                '3': {'username': 'c', 'equity': '105'}, '4': {'username': 'd', 'equity': '220'},
                '5': {'username': 'e', 'equity': '10'}, '6': {'username': 'new', 'equity': '10'}}
    rows, excluded = standings(baseline, accounts)
    assert excluded == 2
    assert [(r['username'], r['return_pct'], r['rank']) for r in rows] == [
        ('a', '10.000000', 1), ('b', '10.000000', 1), ('d', '10.000000', 1), ('c', '5.000000', 4)]
    assert list(rows[0]) == ['username', 'start_equity', 'equity', 'pnl', 'return_pct', 'total_return_pct', 'rank']


def test_report_list_reranks_visible_rows_by_return_string_and_keeps_stored_rows():
    member('operator', is_admin=True)
    stored = [{'username': 'a', 'return_pct': '5', 'rank': 1}, {'username': 'operator', 'return_pct': '4', 'rank': 2},
              {'username': 'b', 'return_pct': '4', 'rank': 2}, {'username': 'c', 'return_pct': '4', 'rank': 2},
              {'username': 'd', 'return_pct': '4.0', 'rank': 5}, {'username': 'e', 'return_pct': '1', 'rank': 6}]
    weekly_report(stored)
    rows = report_list()['reports'][0]['rows']
    # '4.0' and '4' are different strings, so they do not share a rank.
    assert [(r['username'], r['rank']) for r in rows] == [('a', 1), ('b', 2), ('c', 2), ('d', 4), ('e', 5)]
    with Session() as db: assert db.scalar(select(WeeklyReport)).rows == stored


def test_admin_clear_removes_and_reranks_stored_weekly_rows(client):
    headers, admin_id = admin(client)
    uid = member()
    rows = [{'username': 'a', 'return_pct': '5', 'rank': 1}, {'username': 'investor', 'return_pct': '5', 'rank': 1},
            {'username': 'b', 'return_pct': '3', 'rank': 3}, {'username': 'c', 'return_pct': '3', 'rank': 3},
            {'username': 'd', 'return_pct': '1', 'rank': 5}]
    other_rows = [{'username': 'x', 'return_pct': '2', 'rank': 7}]
    touched, untouched = weekly_report(rows), weekly_report(other_rows, days_ago=7)
    assert manage(client, headers, uid, {'action': 'clear', 'request_id': str(uuid4())}).json() == {'ok': True, 'replayed': False}
    with Session() as db:
        assert db.get(WeeklyReport, touched).rows == [
            {'username': 'a', 'return_pct': '5', 'rank': 1}, {'username': 'b', 'return_pct': '3', 'rank': 2},
            {'username': 'c', 'return_pct': '3', 'rank': 2}, {'username': 'd', 'return_pct': '1', 'rank': 4}]
        assert db.get(WeeklyReport, untouched).rows == other_rows
        archive = db.scalar(select(SeasonArchive))
        assert archive.data['weekly_rows'] == [{'report_id': touched, 'rows': [{'username': 'investor', 'return_pct': '5', 'rank': 1}]}]


# Weekly baseline removal -------------------------------------------------------

@pytest.mark.parametrize('action, kept', [('grant', True), ('rebase', False), ('clear', False), ('delete', False)])
def test_admin_actions_drop_the_weekly_baseline_except_grants(client, action, kept):
    headers, admin_id = admin(client)
    uid, other = member(), member('other')
    set_baseline({str(uid): {'equity': '1'}, str(other): {'equity': '2'}})
    body = {'action': action, 'request_id': str(uuid4())} | ({'amount': '10'} if action == 'grant' else {})
    assert manage(client, headers, uid, body).status_code == 200
    assert weekly_baseline() == {str(other): {'equity': '2'}} | ({str(uid): {'equity': '1'}} if kept else {})


def test_delete_account_data_drops_weekly_rows_and_baseline_without_reranking():
    uid, other = member('leaver'), member('stayer')
    rows = [{'username': 'leaver', 'return_pct': '3', 'rank': 1}, {'username': 'stayer', 'return_pct': '1', 'rank': 2}]
    report = weekly_report(rows)
    set_baseline({str(uid): {'equity': '1'}, str(other): {'equity': '2'}})
    with Session.begin() as db: assert delete_account_data(db, db.get(User, uid)) == 'leaver'
    with Session() as db: assert db.get(WeeklyReport, report).rows == [{'username': 'stayer', 'return_pct': '1', 'rank': 2}]
    assert weekly_baseline() == {str(other): {'equity': '2'}}


def test_season_reset_restores_funding_archives_audits_and_drops_baseline(client):
    headers, admin_id = admin(client)
    uid, other = member(), member('other')
    execute_order(uid, order(quantity=2), FakeMarket())
    now = datetime.now(timezone.utc)
    with Session.begin() as db:
        db.add(LimitOrder(user_id=uid, request_id=str(uuid4()), symbol='AAPL', side='buy', quantity=1, limit_price=D(90), status='pending', created_at=now))
        db.add(LimitOrder(user_id=uid, request_id=str(uuid4()), symbol='AAPL', side='buy', quantity=1, limit_price=D(90), status='filled', created_at=now))
        db.get(User, uid).net_contributions_krw = D(5000)
    set_baseline({str(uid): {'equity': '1'}, str(other): {'equity': '2'}})
    r = client.post(f'/api/admin/users/{uid}/reset', headers=headers, json={'label': '시즌1', 'confirmation': 'RESET'})
    assert r.status_code == 200 and r.json() == {'ok': True, 'archived': True}
    with Session() as db:
        u = db.get(User, uid)
        assert (u.cash, u.initial_usd, u.initial_krw, u.initial_fx_date, u.baseline_note, u.net_contributions_krw) == (
            100000, 100000, 100000000, today(), 'admin-reset', 0)
        assert recent(u.performance_since) and u.records_since is None
        assert not db.scalar(select(func.count()).select_from(Position).where(Position.user_id == uid))
        assert sorted((o.status, o.reason) for o in db.scalars(select(LimitOrder))) == [('cancelled', '관리자 초기화'), ('filled', None)]
        archive = db.scalar(select(SeasonArchive))
        assert (archive.user_id, archive.label) == (uid, '시즌1') and recent(archive.created_at)
        assert archive.data == {'wallets': {'USD': '99800.0000', 'KRW': '0.0000'}, 'initial_krw': 'None', 'actor': admin_id,
                                'positions': [{'symbol': 'AAPL', 'quantity': 2, 'average_cost': '100.0000000000',
                                               'native_average_cost': '100.0000000000'}]}
    assert balances(uid) == {'USD': 100000, 'KRW': 0}
    assert weekly_baseline() == {str(other): {'equity': '2'}}
    (a,) = audits()
    assert (a.actor_id, a.target_id, a.action, a.reason, a.data) == (admin_id, uid, 'season_reset', '시즌1', {'archived': True})
    assert is_uuid4(a.request_id) and recent(a.created_at)


# Order replays -----------------------------------------------------------------

class Offline:
    def quote(self, symbol): raise MarketError('offline')


def test_order_replay_answers_before_quoting_and_refuses_a_different_order():
    uid = seed()
    o = order(quantity=3)
    first = execute_order(uid, o, FakeMarket())
    assert list(first) == ['id', 'replayed', 'quantity', 'currency', 'net_amount'] and first['replayed'] is False
    # A replay needs no provider call.
    assert list(execute_order(uid, o, Offline()).items()) == [('id', first['id']), ('replayed', True), ('quantity', 3)]
    for changed in ({'quantity': 4}, {'side': 'sell'}, {'symbol': 'MSFT'}):
        with pytest.raises(HTTPException) as refused:
            execute_order(uid, order(**({'quantity': 3, 'request_id': o.request_id} | changed)), Offline())
        assert (refused.value.status_code, refused.value.detail) == (409, '동일 주문 ID에 다른 주문을 사용할 수 없습니다.')
    # use_max orders replay whatever quantity they ask for.
    assert execute_order(uid, order(quantity=99, use_max=True, request_id=o.request_id), Offline())['replayed']
    with Session() as db: assert db.scalar(select(func.count()).select_from(Transaction)) == 1


def test_order_replay_found_after_the_account_lock():
    uid = seed()
    o = order(quantity=2)
    class Racing(FakeMarket):
        def __init__(self, rival): self.rival = rival; self.calls = 0
        def quote(self, symbol):
            self.calls += 1
            if self.calls == 1: execute_order(uid, self.rival, FakeMarket())  # the same id fills meanwhile
            return super().quote(symbol)
    replay = execute_order(uid, o, Racing(o))
    with Session() as db:
        trade = db.scalar(select(Transaction))
        assert replay == {'id': trade.id, 'replayed': True, 'quantity': 2}
    rival = order(quantity=2)
    with pytest.raises(HTTPException) as refused:
        execute_order(uid, order(quantity=5, request_id=rival.request_id), Racing(rival))
    assert (refused.value.status_code, refused.value.detail) == (409, '동일 주문 ID에 다른 주문을 사용할 수 없습니다.')
    with Session() as db: assert db.scalar(select(func.count()).select_from(Transaction)) == 2
    assert balances(uid)['USD'] == 99600


def test_order_in_caller_transaction_checks_replay_only_under_the_lock():
    uid = seed()
    o = order(quantity=2)
    first = execute_order(uid, o, FakeMarket())
    with Session.begin() as db:
        assert execute_order(uid, o, FakeMarket(), db=db) == {'id': first['id'], 'replayed': True, 'quantity': 2}
    # With a caller's transaction there is no pre-lock check, so the quote is fetched first.
    with Session.begin() as db, pytest.raises(MarketError):
        execute_order(uid, o, Offline(), db=db)


def test_fill_accounting_weighted_average_fees_and_realized_pnl(monkeypatch):
    monkeypatch.setenv('US_BUY_FEE_BPS', '7'); monkeypatch.setenv('US_SELL_FEE_BPS', '3')
    uid = seed()
    m = FakeMarket()
    for side, quantity, price in (('buy', 3, '100.3'), ('buy', 4, '101.7'), ('sell', 5, '99.9')):
        m.quote = lambda s, price=price: {'symbol': s, 'price': D(price), 'timestamp': int(time.time()), 'stale': False}
        result = execute_order(uid, order(side=side, quantity=quantity), m)
    assert result['net_amount'] == D('499.3501') and result['currency'] == 'USD'
    with Session() as db:
        p = db.get(Position, (uid, 'AAPL'))
        assert (p.quantity, p.average_cost, p.native_average_cost) == (2, D('101.1'), D('101.1707857143'))
        assert db.get(Transaction, result['id']).realized_pnl == D('-6.5038')
        assert db.get(User, uid).cash == D('99791.1546')
    assert balances(uid) == {'USD': D('99791.1546'), 'KRW': 0}


# Legacy cost basis (native_average_cost is None) ------------------------------

def test_legacy_position_falls_back_to_usd_average_cost():
    uid = seed()
    with Session.begin() as db:
        db.add(Position(user_id=uid, symbol='AAPL', quantity=10, average_cost=D(80), native_average_cost=None))
    p = preview_order(uid, 'AAPL', 'sell', 4, FakeMarket())
    # The preview shows no average cost but values the P&L at the fallback.
    assert p['average_cost'] is None and p['unrealized_pnl'] == 200
    assert (p['holding'], p['holding_after'], p['max_quantity'], p['can_submit']) == (10, 6, 10, True)
    row = portfolio(uid, FakeMarket(), main.fx)['positions'][0]
    assert (row['average_cost'], row['pnl'], row['return_pct']) == (80, 200, 25)
    now = datetime.now(timezone.utc)
    prices = {'AAPL': {'native_price': D(100), 'currency': 'USD', 'quote_time': now, 'meta': {'stale': False, 'fallback': False}}}
    assert snapshot_user(uid, now.date(), prices, {'rate': D(1000), 'date': today()}, now, now) == 'stored'
    with Session() as db: assert db.scalar(select(PerformanceSnapshot)).positions['AAPL']['average_cost'] == '80.0000000000'
    sold = execute_order(uid, order(side='sell', quantity=4), FakeMarket())
    with Session() as db:
        assert db.get(Transaction, sold['id']).realized_pnl == 80
        assert db.get(Position, (uid, 'AAPL')).native_average_cost is None
    execute_order(uid, order(quantity=6), FakeMarket())
    with Session() as db:
        position = db.get(Position, (uid, 'AAPL'))
        assert (position.quantity, position.average_cost, position.native_average_cost) == (12, 90, 90)


def test_preview_keys_and_indicative_only_by_market_quote_age(monkeypatch):
    uid = seed()
    old = FakeMarket()
    old.quote = lambda s: {'symbol': s, 'price': D(100), 'currency': 'KRW' if s.startswith('KR:') else 'USD',
                           'timestamp': int(time.time()) - 120, 'stale': False}
    monkeypatch.setenv('US_MAX_QUOTE_AGE', '60'); monkeypatch.setenv('MAX_QUOTE_AGE', '600')
    p = preview_order(uid, 'AAPL', 'buy', 1, old)
    assert list(p) == ['currency', 'gross_amount', 'fee', 'tax', 'net_amount', 'fee_bps', 'tax_bps', 'price', 'quantity',
                       'max_quantity', 'balance', 'balance_after', 'quote_timestamp', 'session', 'price_mode',
                       'session_tradeable', 'indicative_only', 'holding', 'holding_after', 'can_submit', 'average_cost',
                       'unrealized_pnl']
    assert p['indicative_only'] is True and p['unrealized_pnl'] == 0 and p['average_cost'] is None
    assert preview_order(uid, 'KR:005930', 'buy', 1, old)['indicative_only'] is False
    monkeypatch.setenv('US_MAX_QUOTE_AGE', '600'); monkeypatch.setenv('MAX_QUOTE_AGE', '60')
    assert preview_order(uid, 'AAPL', 'buy', 1, old)['indicative_only'] is False
    assert preview_order(uid, 'KR:005930', 'buy', 1, old)['indicative_only'] is True
    fresh = FakeMarket()
    for extra in ({'stale': True}, {'session_tradeable': False}):
        fresh.quote = lambda s, extra=extra: {'symbol': s, 'price': D(100), 'timestamp': int(time.time()), 'stale': False} | extra
        assert preview_order(uid, 'AAPL', 'buy', 1, fresh)['indicative_only'] is True


# Initial KRW equity ------------------------------------------------------------

def test_initial_krw_is_set_once_and_rounded_half_even():
    class TieFX:
        def __init__(self, rate): self.rate = rate
        def current_rate(self, source='USD', target='KRW'): return {'rate': self.rate, 'date': '2026-09-25'}
    # 100.0001 * 1234.5 = 123450.12345: half-even keeps ...1234 (PostgreSQL would give ...1235).
    first, second = member('first', initial_usd=D('100.0001')), member('second', initial_usd=D('100.0001'))
    initialize_equity(first, TieFX(D('1234.5')))
    initialize_equity(first, TieFX(D(2000)))
    now = datetime.now(timezone.utc)
    assert snapshot_user(second, now.date(), {}, {'rate': D('1234.5'), 'date': '2026-09-25'}, now, now) == 'stored'
    with Session() as db:
        for uid in (first, second):
            u = db.get(User, uid)
            assert (u.initial_krw, u.initial_fx_date) == (D('123450.1234'), '2026-09-25')
        assert db.scalar(select(PerformanceSnapshot)).initial_equity_krw == D('123450.1234')


# Admin manage actions ----------------------------------------------------------

def test_manage_grant_response_audit_replay_and_validation(client):
    headers, admin_id = admin(client)
    uid = member()
    body = {'action': 'grant', 'currency': 'USD', 'amount': '1000', 'request_id': str(uuid4())}
    r = manage(client, headers, uid, body)
    assert r.status_code == 200 and r.json() == {'ok': True, 'replayed': False}
    (a,) = audits()
    assert (a.actor_id, a.target_id, a.request_id, a.action, a.reason) == (admin_id, uid, body['request_id'], 'grant', '지원금 지급')
    assert a.data == {'request': signature(body), 'before': {'USD': '100000.0000', 'KRW': '0'},
                      'after': {'USD': '101000.0000', 'KRW': '0'}, 'fx_rate': '1000', 'fx_date': today()}
    assert recent(a.created_at)
    with Session() as db:
        u = db.get(User, uid)
        assert (u.cash, u.net_contributions_krw, u.initial_krw, u.initial_fx_date) == (101000, 1000000, 100000000, today())
    assert manage(client, headers, uid, body).json() == {'ok': True, 'replayed': True}
    for retry, target in ((body | {'amount': '2000'}, uid), (body, admin_id)):
        r = manage(client, headers, target, retry)
        assert (r.status_code, r.json()['detail']) == (409, RETRY_MISMATCH)
    assert len(audits()) == 1 and balances(uid)['USD'] == 101000
    for bad in ({'amount': '0'}, {'currency': 'KRW', 'amount': '10.5'}):
        r = manage(client, headers, uid, {'action': 'grant', 'request_id': str(uuid4())} | bad)
        assert (r.status_code, r.json()['detail']) == (422, GRANT_INVALID)
    r = manage(client, headers, 999999, {'action': 'grant', 'amount': '0', 'request_id': str(uuid4())})
    assert (r.status_code, r.json()['detail']) == (404, '사용자가 없습니다.')
    with Session.begin() as db: db.get(Wallet, (uid, 'USD')).balance = D('999999999999000')
    r = manage(client, headers, uid, {'action': 'grant', 'amount': '1000.0001', 'request_id': str(uuid4())})
    assert (r.status_code, r.json()['detail']) == (409, '지갑 한도를 초과합니다.')
    assert manage(client, headers, uid, {'action': 'grant', 'amount': '1000', 'request_id': str(uuid4())}).status_code == 200
    assert balances(uid)['USD'] == D('1000000000000000')


def test_grant_amount_is_checked_after_the_rate_in_manage_but_before_it_in_manage_all(client, monkeypatch):
    headers, admin_id = admin(client)
    uid = member()
    class Down:
        def current_rate(self, source='USD', target='KRW'): raise MarketError('기준환율 없음')
    monkeypatch.setattr(main, 'fx', Down())
    r = manage(client, headers, uid, {'action': 'grant', 'amount': '0', 'request_id': str(uuid4())})
    assert (r.status_code, r.json()['detail']) == (503, '기준환율 없음')
    r = manage(client, headers, 999999, {'action': 'grant', 'amount': '0', 'request_id': str(uuid4())})
    assert r.status_code == 404
    r = client.post('/api/admin/users/manage-all', headers=headers, json={'action': 'grant', 'amount': '0', 'request_id': str(uuid4())})
    assert (r.status_code, r.json()['detail']) == (422, GRANT_INVALID)
    r = client.post('/api/admin/users/manage-all', headers=headers, json={'action': 'grant', 'amount': '10', 'request_id': str(uuid4())})
    assert r.status_code == 503


def test_manage_rebase_sets_the_baseline_and_audits(client):
    headers, admin_id = admin(client)
    uid = member()
    execute_order(uid, order(quantity=3), FakeMarket())
    with Session.begin() as db: db.get(User, uid).net_contributions_krw = D(5000)
    body = {'action': 'rebase', 'reason': ' 기준 변경 ', 'request_id': str(uuid4())}
    assert manage(client, headers, uid, body).json() == {'ok': True, 'replayed': False}
    (a,) = audits()
    with Session() as db:
        u = db.get(User, uid)
        assert (u.initial_krw, u.initial_usd, u.net_contributions_krw, u.initial_fx_date, u.baseline_note, u.records_since) == (
            100000000, 100000, 0, today(), 'admin-rebase', None)
        assert u.performance_since == a.created_at and recent(a.created_at)
    wallets = {'USD': '99700.0000', 'KRW': '0.0000'}
    assert (a.actor_id, a.target_id, a.request_id, a.action, a.reason) == (admin_id, uid, body['request_id'], 'rebase', '기준 변경')
    assert a.data == {'request': signature(body), 'before': wallets, 'after': wallets, 'fx_rate': '1000', 'fx_date': today()}
    assert manage(client, headers, uid, body).json() == {'ok': True, 'replayed': True}
    empty = member('empty', cash=D(0))
    r = manage(client, headers, empty, {'action': 'rebase', 'request_id': str(uuid4())})
    assert (r.status_code, r.json()['detail']) == (409, '총자산이 0 이하인 계좌는 기준 재설정이 불가능합니다.')


def test_manage_clear_archives_restores_registration_state_and_audits(client):
    headers, admin_id = admin(client)
    uid = member()
    execute_order(uid, order(quantity=2), FakeMarket())
    now = datetime.now(timezone.utc)
    with Session.begin() as db:
        db.add(LimitOrder(user_id=uid, request_id=str(uuid4()), symbol='AAPL', side='buy', quantity=1, limit_price=D(90), status='pending', created_at=now))
        db.add(Watchlist(user_id=uid, symbol='AAPL', created_at=now))
        u = db.get(User, uid); u.net_contributions_krw = D(7000); u.performance_since = now - timedelta(days=1)
        db.get(Wallet, (uid, 'KRW')).balance = D(5000)
    with Session() as db: since = db.get(User, uid).performance_since
    body = {'action': 'clear', 'request_id': str(uuid4())}
    assert manage(client, headers, uid, body).json() == {'ok': True, 'replayed': False}
    (a,) = audits()
    with Session() as db:
        u = db.get(User, uid)
        assert (u.cash, u.initial_usd, u.initial_krw, u.net_contributions_krw, u.initial_fx_date, u.performance_since, u.baseline_note) == (
            100000, 100000, 100000000, 0, today(), None, 'registration')
        for model in (Position, Transaction, LimitOrder, Watchlist):
            assert not db.scalar(select(func.count()).select_from(model).where(model.user_id == uid))
        archive = db.scalar(select(SeasonArchive))
        assert (archive.user_id, archive.label) == (uid, '전체 초기화')
        assert archive.created_at == u.records_since == a.created_at and recent(a.created_at)
        data = archive.data
        assert set(data) == {'positions', 'transactions', 'fx_transactions', 'limit_orders', 'watchlists', 'popularity_events',
                             'wallet_transfers', 'wallets', 'actor', 'reason', 'performance', 'weekly_rows'}
        assert (data['wallets'], data['actor'], data['reason'], data['weekly_rows']) == (
            {'USD': '99800.0000', 'KRW': '5000.0000'}, admin_id, '회원가입 직후 상태로 초기화', [])
        assert data['performance'] == {'initial_krw': '100000000.0000', 'initial_usd': '100000.0000', 'net_contributions_krw': '7000.0000',
                                       'initial_fx_date': today(), 'performance_since': str(since)}
        assert data['transactions'][0]['quantity'] == 2 and data['positions'][0]['symbol'] == 'AAPL'
        assert data['limit_orders'][0]['status'] == 'pending' and data['watchlists'][0]['symbol'] == 'AAPL'
    assert balances(uid) == {'USD': 100000, 'KRW': 0}
    assert (a.actor_id, a.target_id, a.request_id, a.action, a.reason) == (admin_id, uid, body['request_id'], 'clear', '회원가입 직후 상태로 초기화')
    assert a.data == {'request': signature(body), 'before': {'USD': '99800.0000', 'KRW': '5000.0000'},
                      'after': {'USD': '100000', 'KRW': '0'}, 'fx_rate': '1000', 'fx_date': today()}
    assert manage(client, headers, uid, body).json() == {'ok': True, 'replayed': True}
    with Session() as db: assert db.scalar(select(func.count()).select_from(SeasonArchive)) == 1


def test_manage_delete_response_audit_and_replay(client):
    headers, admin_id = admin(client)
    uid = member()
    body = {'action': 'delete', 'request_id': str(uuid4())}
    assert manage(client, headers, uid, body).json() == {'ok': True, 'replayed': False, 'deleted': 'investor'}
    (a,) = audits()
    assert (a.actor_id, a.target_id, a.request_id, a.action, a.reason) == (admin_id, admin_id, body['request_id'], 'account_delete', '계정 영구 삭제')
    assert a.data == {'request': signature(body), 'deleted_username': 'investor', 'deleted_user_id': uid} and recent(a.created_at)
    assert manage(client, headers, uid, body).json() == {'ok': True, 'replayed': True}
    r = manage(client, headers, admin_id, {'action': 'delete', 'request_id': str(uuid4())})
    assert (r.status_code, r.json()['detail']) == (409, '현재 로그인한 관리자 계정은 삭제할 수 없습니다.')
    r = manage(client, headers, uid, {'action': 'delete', 'request_id': str(uuid4())})
    assert (r.status_code, r.json()['detail']) == (404, '사용자가 없습니다.')
    assert len(audits()) == 1


def test_manage_all_bulk_grant_audit_replay_and_wallet_cap(client):
    headers, admin_id = admin(client)
    first, second, inactive = member('first'), member('second'), member('inactive', active=False)
    post = lambda body: client.post('/api/admin/users/manage-all', headers=headers, json=body)
    r = post({'action': 'rebase', 'request_id': str(uuid4())})
    assert (r.status_code, r.json()['detail']) == (422, '모든 사용자 대상 작업은 지원금 지급만 허용합니다.')
    body = {'action': 'grant', 'currency': 'KRW', 'amount': '5000', 'request_id': str(uuid4())}
    assert post(body).json() == {'ok': True, 'replayed': False, 'count': 2}
    for uid in (first, second):
        assert balances(uid) == {'USD': 100000, 'KRW': 5000}
        with Session() as db: assert (db.get(User, uid).net_contributions_krw, db.get(User, uid).cash) == (5000, 100000)
    assert balances(inactive) == {}
    (a,) = audits()
    assert (a.actor_id, a.target_id, a.request_id, a.action, a.reason) == (admin_id, admin_id, body['request_id'], 'bulk_grant', '전체 지원금 지급')
    assert a.data == {'request': signature(body), 'count': 2, 'currency': 'KRW', 'amount': '5000'} and recent(a.created_at)
    assert post(body).json() == {'ok': True, 'replayed': True, 'count': 2}
    for retry in (body | {'amount': '6000'}, body | {'amount': '0'}):
        r = post(retry)
        assert (r.status_code, r.json()['detail']) == (409, RETRY_MISMATCH)
    r = post({'action': 'grant', 'amount': '0', 'request_id': str(uuid4())})
    assert (r.status_code, r.json()['detail']) == (422, GRANT_INVALID)
    with Session.begin() as db: db.get(Wallet, (second, 'KRW')).balance = D('999999999999999')
    r = post({'action': 'grant', 'currency': 'KRW', 'amount': '5000', 'reason': '추가', 'request_id': str(uuid4())})
    assert (r.status_code, r.json()['detail']) == (409, 'second 지갑 한도를 초과합니다.')
    assert balances(first)['KRW'] == 5000 and len(audits()) == 1


# Other admin audits -------------------------------------------------------------

def test_initial_amount_and_account_status_audits(client):
    headers, admin_id = admin(client)
    uid = member()
    r = client.post('/api/admin/initial', headers=headers, json={'amount': '50000'})
    assert r.json() == {'ok': True, 'applies_to': 'new accounts and explicitly reset accounts'}
    r = client.post(f'/api/admin/users/{admin_id}/active', headers=headers, json={'active': False})
    assert (r.status_code, r.json()['detail']) == (409, '자기 계정을 정지할 수 없습니다.')
    r = client.post('/api/admin/users/999999/active', headers=headers, json={'active': False})
    assert (r.status_code, r.json()['detail']) == (404, '사용자가 없습니다.')
    assert client.post(f'/api/admin/users/{uid}/active', headers=headers, json={'active': False}).json() == {'ok': True}
    with Session() as db: assert db.get(User, uid).active is False
    initial, status = audits()
    assert (initial.actor_id, initial.target_id, initial.action, initial.reason, initial.data) == (
        admin_id, admin_id, 'initial_amount', '초기 지급액 변경', {'before': '100000', 'after': '50000'})
    assert (status.actor_id, status.target_id, status.action, status.reason, status.data) == (
        admin_id, uid, 'account_status', '계정 상태 변경', {'before': True, 'after': False})
    assert all(is_uuid4(a.request_id) and recent(a.created_at) for a in (initial, status))
    assert initial.request_id != status.request_id


def test_notice_audits_use_the_notice_timestamps(client):
    headers, admin_id = admin(client)
    assert client.post('/api/admin/notice', headers=headers, json={'kind': 'update', 'title': '새 기능', 'body': '내용'}).status_code == 200
    assert client.post('/api/admin/notice/clear', headers=headers).json() == {'cleared': True, 'notice': None}
    posted, cleared = audits()
    with Session() as db: notice = db.scalar(select(SiteNotice))
    assert (posted.actor_id, posted.target_id, posted.action, posted.reason, posted.data) == (
        admin_id, admin_id, 'notice_post', '공지 등록 · 업데이트 안내 · 새 기능', {'notice_id': notice.id, 'kind': 'update'})
    assert (cleared.actor_id, cleared.target_id, cleared.action, cleared.reason, cleared.data) == (
        admin_id, admin_id, 'notice_clear', '공지 해제 · 새 기능', {'notice_id': notice.id, 'kind': 'update'})
    assert posted.created_at == notice.posted_at and cleared.created_at == notice.cleared_at
    assert is_uuid4(posted.request_id) and is_uuid4(cleared.request_id) and posted.request_id != cleared.request_id


# Catalog rows ------------------------------------------------------------------

class CatalogMarket:
    """us_bond catalog order: SHY IEF TLT SGOV BIL VGSH VGIT VGLT GOVT."""
    values = {
        'SHY': {'change_pct': '1.5', 'volume': 300, 'turnover': 'NaN'},
        'IEF': {'volume': 100},
        'TLT': {'change_pct': 'NaN', 'volume': '0', 'turnover': 50},
        'SGOV': {'change_pct': -2, 'volume': 'x', 'turnover': 70},
        'BIL': {'change_pct': 0, 'volume': 300, 'turnover': None},
        'VGSH': {'change_pct': D('1.50'), 'turnover': 70},
        'VGIT': None,
        'VGLT': {'change_pct': 3, 'volume': -5, 'turnover': 'Infinity'},
        'GOVT': {'change_pct': 'abc', 'volume': 1000, 'turnover': 90},
    }
    def quote(self, symbol):
        if self.values[symbol] is None: raise MarketError('공급자 설정 필요')
        return {'symbol': symbol, 'price': D(10), 'timestamp': int(time.time()), 'stale': False} | self.values[symbol]


@pytest.mark.parametrize('kind, expected', [
    ('up', ['VGLT', 'SHY', 'VGSH', 'BIL', 'SGOV', 'IEF', 'TLT', 'VGIT', 'GOVT']),
    ('down', ['SGOV', 'BIL', 'SHY', 'VGSH', 'VGLT', 'IEF', 'TLT', 'VGIT', 'GOVT']),
    ('shares', ['GOVT', 'SHY', 'BIL', 'IEF', 'TLT', 'VGLT', 'SGOV', 'VGSH', 'VGIT']),
    ('volume', ['GOVT', 'SGOV', 'VGSH', 'TLT', 'SHY', 'IEF', 'BIL', 'VGIT', 'VGLT']),
])
def test_curated_rows_sort_priced_rows_first(kind, expected):
    result = curated_market_rows(CatalogMarket(), 'us_bond', kind)
    assert [r['symbol'] for r in result['rows']] == expected
    measure = {'volume': '거래대금', 'shares': '거래량'}.get(kind)
    notes = ['등록 종목의 실제 공급자 시세입니다. 전체 시장 순위가 아닙니다.']
    if measure: notes.append(f'{measure}이 제공된 종목만 그 값으로 정렬하고 나머지는 뒤에 표시합니다.')
    notes.append('1개 종목 시세 조회 실패/공급자 설정 필요')
    assert {k: v for k, v in result.items() if k != 'rows'} == {'scope': '등록 종목 둘러보기', 'notice': ' '.join(notes),
                                                               'unavailable': False, 'source': '실제 시세'}
    failed = next(r for r in result['rows'] if r['symbol'] == 'VGIT')
    assert failed['price'] is None and failed['data_status'] == '공급자 설정 필요'


def test_curated_rows_keep_catalog_order_when_nothing_is_priced():
    market = FakeMarket()
    result = curated_market_rows(market, 'gold', 'volume', unavailable=MarketError('순위 키 필요'))
    assert [r['symbol'] for r in result['rows']] == ['GLD', 'IAU', 'GLDM', 'KR:411060']
    assert [r['market'] for r in result['rows']] == ['US', 'US', 'US', 'KR']
    assert result['notice'] == ('등록 종목의 실제 공급자 시세입니다. 전체 시장 순위가 아닙니다. '
                                '이 시세 공급자는 거래대금을 제공하지 않아 거래대금순으로 정렬할 수 없습니다. 순위 키 필요')
    assert (result['unavailable'], result['source']) == (True, '시세 확인 대기')
