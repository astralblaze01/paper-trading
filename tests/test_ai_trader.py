"""scripts/ai_trader.py carries an AI's research requests and orders to the site's API, and nothing else."""
import importlib.util
import json
from pathlib import Path
from datetime import datetime, timezone
import pytest

spec = importlib.util.spec_from_file_location('ai_trader', Path(__file__).resolve().parents[1] / 'scripts' / 'ai_trader.py')
ai_trader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ai_trader)

WorldResearch = ai_trader.WorldResearch
FEED = b'<rss><channel><item><title>Fixture market report</title><link>https://example.org/news</link><pubDate>Mon, 05 Oct 2026 07:00:00 GMT</pubDate></item></channel></rss>'
def fixture_world():
    return WorldResearch(loader=lambda _: FEED, article_loader=lambda _: {'url':'https://example.org/news','body':'Fixture article body with enough context. '*20}, now=datetime(2026, 10, 5, 8, tzinfo=timezone.utc))
SOURCE_ID = fixture_world().news('market')['items'][0]['id']

@pytest.fixture(autouse=True)
def offline_world(monkeypatch):
    monkeypatch.setattr(ai_trader, 'WorldResearch', fixture_world)

PORTFOLIO = {'wallets': {'USD': 100000.0, 'KRW': 0.0}, 'equity_usd': 100000.0, 'return_pct': 0, 'realized_pnl': {'USD': 0, 'KRW': 0},
             'positions': [], 'fx': {'rate': 1350.0}}


class FakeClient:
    def __init__(self, sessions={'KR': 'regular', 'US': 'closed'}):
        self.sessions, self.posts, self.gets = sessions, [], []

    def get(self, path, **params):
        self.gets.append(path)
        if path == '/api/market-overview':
            return {'markets': [{'market': m, 'session': s, 'open': s != 'closed', 'tradable': s != 'closed'} for m, s in self.sessions.items()]}
        if path == '/api/portfolio': return PORTFOLIO
        if path == '/api/explore': return {'rows': [{'symbol': 'KR:005930', 'name': '삼성전자', 'price': 70000, 'change_pct': 1.2}]}
        if path.startswith('/api/candles/'):
            return {'range': params['range'], 'candles': [{'close': str(100 + i)} for i in range(30)]}
        raise AssertionError(path)

    def post(self, path, body=None, **kw):
        self.posts.append((path, body))
        if path == '/api/orders' and body['quantity'] > 100: raise ai_trader.ApiError(409, '잔액이 부족합니다.')
        return {'quantity': body.get('quantity'), 'currency': 'KRW', 'net_amount': '1', 'received': '1'}


def scripted(*answers):
    prompts = []
    def ask(prompt):
        prompts.append(prompt)
        answer = json.loads(answers[len(prompts) - 1])
        answer.setdefault('sources', [SOURCE_ID])
        return json.dumps(answer)
    return ask, prompts


def test_answers_are_read_through_code_fences_and_chatter():
    assert ai_trader.extract_json('네.\n```json\n{"step": "decide", "actions": []}\n```') == {'step': 'decide', 'actions': []}
    assert ai_trader.extract_json('{"a": {"b": 1}}') == {'a': {'b': 1}}


def test_only_well_formed_actions_for_open_markets_are_kept():
    kept, dropped = ai_trader.checked_actions([
        {'type': 'buy', 'symbol': 'KR:005930', 'quantity': 3},
        {'type': 'buy', 'symbol': 'AAPL', 'quantity': 1},          # US is closed
        {'type': 'sell', 'symbol': 'KR:005930', 'quantity': 1.5},  # not whole shares
        {'type': 'buy', 'symbol': 'rm -rf', 'quantity': 1},
        {'type': 'exchange', 'source': 'USD', 'amount': 1000},
        {'type': 'withdraw', 'amount': 5},
    ], ['KR'])
    assert [(a['type'], a.get('symbol')) for a in kept] == [('buy', 'KR:005930'), ('exchange', None)]
    assert [why for _, why in dropped] == ['US market is not open', 'quantity must be a positive whole number', 'bad symbol', 'unknown action']


def test_nothing_is_asked_while_every_market_is_closed():
    client = FakeClient({'KR': 'closed', 'US': 'closed'})
    ask, prompts = scripted()
    assert ai_trader.run('claude', client=client, ask=ask) is None
    assert prompts == [] and client.posts == []


def test_extended_sessions_count_as_open(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_trader, 'STATE', tmp_path)
    client = FakeClient({'KR': 'closed', 'US': 'overnight'})
    ask, prompts = scripted(json.dumps({'step': 'decide', 'actions': [{'type': 'buy', 'symbol': 'AAPL', 'quantity': 1}]}))
    entry = ai_trader.run('gpt', client=client, ask=ask)
    assert entry['markets'] == ['US'] and 'US(overnight)' in prompts[0]
    assert [p for p, _ in client.posts] == ['/api/orders', '/api/ai/decisions'] and client.posts[0][1]['symbol'] == 'AAPL'


def test_research_then_decide_places_the_orders_in_order(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_trader, 'STATE', tmp_path)
    client = FakeClient()
    ask, prompts = scripted(
        json.dumps({'step': 'research', 'requests': [{'type': 'chart', 'symbol': 'KR:005930', 'range': '3M'}]}),
        json.dumps({'step': 'decide', 'summary': '삼성전자 매수', 'actions': [
            {'type': 'exchange', 'source': 'USD', 'amount': 1000, 'reason': '원화 확보'},
            {'type': 'buy', 'symbol': 'KR:005930', 'quantity': 500, 'reason': '너무 많이'},
            {'type': 'buy', 'symbol': 'KR:005930', 'quantity': 10, 'reason': '추세'}]}))
    entry = ai_trader.run('claude', client=client, ask=ask)
    # The second prompt carries the chart the AI asked for.
    assert '"change_20_pct"' in prompts[1] and '"KR:005930"' in prompts[1]
    # The trades, then the run's record for the admin page.
    assert [p for p, _ in client.posts] == ['/api/fx/exchange', '/api/orders', '/api/orders', '/api/ai/decisions']
    # A rejected order is logged and the run goes on.
    assert entry['results'][1]['error'] == '잔액이 부족합니다.' and entry['results'][2]['result']['quantity'] == 10
    # The next run remembers what was done.
    assert ai_trader.memory('claude')[0]['summary'] == '삼성전자 매수'


def test_the_last_round_must_decide(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_trader, 'STATE', tmp_path)
    research = json.dumps({'step': 'research', 'requests': [{'type': 'quote', 'symbol': 'KR:005930'}]})
    ask, prompts = scripted(research, research, research)
    client = FakeClient()
    client.get = (lambda original: lambda path, **p: {'native_price': 1} if path.startswith('/api/quote/') else original(path, **p))(client.get)
    entry = ai_trader.run('gpt', client=client, ask=ask)
    assert len(prompts) == ai_trader.RESEARCH_ROUNDS + 1 and '남은 조사 횟수: 0' in prompts[-1]
    assert entry['results'] == [] and [p for p, _ in client.posts] == ['/api/ai/decisions']


def test_dry_run_places_and_logs_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_trader, 'STATE', tmp_path)
    client = FakeClient()
    ask, _ = scripted(json.dumps({'step': 'decide', 'actions': [{'type': 'buy', 'symbol': 'KR:005930', 'quantity': 1}]}))
    entry = ai_trader.run('claude', client=client, ask=ask, dry_run=True)
    assert entry['results'][0]['dry_run'] and client.posts == [] and not list(tmp_path.iterdir())


def test_gemini_runs_through_agy_pinned_to_a_gemini_model(monkeypatch):
    seen = {}
    class Done:
        returncode, stderr = 0, ''
        stdout = json.dumps({'status': 'SUCCESS', 'response': '{"step": "decide", "actions": []}'})
    def run(cmd, **kw):
        seen['cmd'] = cmd
        return Done()
    monkeypatch.setattr(ai_trader.subprocess, 'run', run)
    assert ai_trader.extract_json(ai_trader.ASK['gemini']('prompt')) == {'step': 'decide', 'actions': []}
    cmd = seen['cmd']
    # Antigravity also serves other vendors' models, so ai_gemini always names a Gemini one.
    assert cmd[0] == 'agy' and cmd[cmd.index('--model') + 1].startswith('gemini-') and cmd[-1].endswith('prompt')
    assert ai_trader.AGENTS['gemini']['username'] == 'ai_gemini'


def test_claude_and_gpt_models_are_pinned_not_left_to_cli_defaults(monkeypatch, tmp_path):
    seen = []
    class Done:
        returncode, stderr = 0, ''
        stdout = json.dumps({'result': '{}', 'is_error': False})
    def run(cmd, **kw):
        seen.append(cmd)
        if cmd[0] == 'codex': Path(cmd[cmd.index('-o') + 1]).write_text('{}')
        return Done()
    monkeypatch.setattr(ai_trader.subprocess, 'run', run)
    ai_trader.ASK['claude']('p'); ai_trader.ASK['gpt']('p')
    claude, codex = seen
    assert claude[claude.index('--model') + 1] == 'claude-opus-5-5' and claude[claude.index('--effort') + 1] == 'medium'
    assert codex[codex.index('-m') + 1] == 'gpt-6.1-sol' and 'model_reasoning_effort="medium"' in codex


def test_the_record_carries_the_reasoning_and_the_data_looked_at(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_trader, 'STATE', tmp_path)
    client = FakeClient()
    ask, _ = scripted(
        json.dumps({'step': 'research', 'thinking': '반도체 추세 확인', 'requests': [{'type': 'chart', 'symbol': 'KR:005930', 'range': '3M'}]}),
        json.dumps({'step': 'decide', 'analysis': '20일 상승 추세라 분할 매수', 'summary': '삼성전자 소량 매수',
                    'actions': [{'type': 'buy', 'symbol': 'KR:005930', 'quantity': 1, 'reason': '추세'}]}))
    ai_trader.run('claude', client=client, ask=ask)
    path, body = client.posts[-1]
    record = body['data']
    assert path == '/api/ai/decisions' and body['status'] == 'ok' and body['summary'] == '삼성전자 소량 매수'
    assert record['analysis'] == '20일 상승 추세라 분할 매수' and record['thinking'] == ['반도체 추세 확인']
    assert record['research_data'][0][0]['data']['change_20_pct'] is not None
    assert record['results'][0]['action']['reason'] == '추세' and record['account']['cash']['USD'] == 100000.0
    assert record['model'] == 'claude-opus-5-5 · medium'
    # The local memory file keeps the reasoning but not the bulky data.
    local = json.loads((tmp_path / 'claude.jsonl').read_text())
    assert 'research_data' not in local and local['analysis'] == '20일 상승 추세라 분할 매수'


def test_a_failed_ai_call_is_recorded_as_an_error_and_trades_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_trader, 'STATE', tmp_path)
    client = FakeClient()
    def broken(prompt): raise RuntimeError('codex failed: quota')
    try:
        ai_trader.run('gpt', client=client, ask=broken)
        raise AssertionError('the run should fail')
    except RuntimeError as exc:
        assert 'quota' in str(exc)
    (path, body), = client.posts
    assert path == '/api/ai/decisions' and body['status'] == 'error' and 'quota' in body['data']['error']


@pytest.mark.parametrize('sources', [[], ['invented-id'], 'bad-shape'])
def test_uncited_or_invented_world_evidence_cannot_place_orders(tmp_path, monkeypatch, sources):
    monkeypatch.setattr(ai_trader, 'STATE', tmp_path)
    client = FakeClient()
    ask, _ = scripted(json.dumps({'step': 'decide', 'sources': sources, 'actions': [
        {'type': 'exchange', 'source': 'USD', 'amount': 1000},
        {'type': 'buy', 'symbol': 'KR:005930', 'quantity': 1}]}))
    entry = ai_trader.run('claude', client=client, ask=ask)
    assert entry['results'] == [] and len(entry['dropped']) == 2
    assert [path for path, _ in client.posts] == ['/api/ai/decisions']


def test_live_world_context_and_requested_news_are_auditable(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_trader, 'STATE', tmp_path)
    client = FakeClient()
    ask, prompts = scripted(
        json.dumps({'step': 'research', 'requests': [{'type': 'news', 'query': '삼성전자 실적', 'market': 'KR'}]}),
        json.dumps({'step': 'decide', 'sources': [SOURCE_ID], 'analysis': '보도 제목을 확인했으나 본문은 미확인, 관망', 'actions': []}))
    entry = ai_trader.run('claude', client=client, ask=ask)
    assert 'Fixture market report' in prompts[0] and 'headline_only' in prompts[0]
    assert 'https://example.org/news' not in prompts[0]  # bounded prompt, full URL stays auditable
    assert '삼성전자 실적' in prompts[1] and 'published_at' in prompts[1]
    assert entry['sources'][0]['id'] == SOURCE_ID
    assert entry['sources'][0]['url'] == 'https://example.org/news'
    assert entry['research_data'][0][0]['request']['type'] == 'news'
    assert entry['research_data'][-1][0]['request']['type'] == 'world_overview'


def test_no_world_feeds_means_no_orders_even_if_ai_reuses_old_citation(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_trader, 'STATE', tmp_path)
    def failed(_): raise OSError('offline')
    world = WorldResearch(loader=failed)
    client = FakeClient()
    ask, _ = scripted(json.dumps({'step': 'decide', 'sources': [SOURCE_ID], 'actions': [{'type': 'sell', 'symbol': 'KR:005930', 'quantity': 1}]}))
    result = ai_trader.run('gpt', client=client, ask=ask, world=world)
    assert result['sources'] == [] and result['results'] == [] and len(result['dropped']) == 1


def test_headline_only_source_cannot_authorize_an_order(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_trader, 'STATE', tmp_path)
    class Headlines:
        def overview(self, markets): return {'feeds': {}}
        def news(self, *args): return {'status':'ok','items':[]}
        def citations(self, decision): return [{'id':'headline','coverage':'headline_only','title':'title','url':'https://example.org','publisher':'x','published_at':'2026-10-05T00:00:00+00:00'}]
    client=FakeClient(); ask,_=scripted(json.dumps({'step':'decide','sources':['headline'],'actions':[{'type':'buy','symbol':'KR:005930','quantity':1}]}))
    result=ai_trader.run('claude',client=client,ask=ask,world=Headlines())
    assert result['results']==[] and '본문·공식 발췌' in result['dropped'][0]['why']


def test_world_evidence_in_the_prompt_is_trimmed_to_fit_every_cli():
    body = 'x' * 5000
    context = {'feeds': {'US': {'feed': 'https://f', 'items': [{'id': 'a', 'url': 'https://u', 'body': body}]}},
               'remembered': [{'id': 'b', 'body': body}]}
    trimmed = ai_trader.prompt_evidence(context)
    assert len(trimmed['feeds']['US']['items'][0]['body']) == ai_trader.PROMPT_BODY_CHARS and 'url' not in trimmed['feeds']['US']['items'][0]
    assert len(trimmed['remembered'][0]['body']) == ai_trader.PROMPT_REMEMBERED_CHARS


def test_memory_covers_the_last_twelve_hours_of_runs(tmp_path, monkeypatch):
    """At one run every 30 minutes the AI sees its last 24 decisions, oldest first."""
    monkeypatch.setattr(ai_trader, 'STATE', tmp_path)
    ai_trader.log_path('claude').write_text(''.join(
        json.dumps({'time': f't{i}', 'summary': f'결정 {i}', 'results': []}) + '\n' for i in range(30)))
    seen = ai_trader.memory('claude')
    assert len(seen) == 24 and seen[0]['summary'] == '결정 6' and seen[-1]['summary'] == '결정 29'


# The AI's own notebook ---------------------------------------------------------

NOON = datetime(2026, 10, 6, 3, 30, tzinfo=timezone.utc)   # 12:30 in Korea


def test_a_run_writes_the_day_journal_and_dated_stock_notes(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_trader, 'STATE', tmp_path)
    decision = {'summary': 'KODEX 200 분할 매수', 'journal': '- 가설: 반도체 실적 기대가 지수를 끈다\n- 다음: 삼성 잠정실적 확인',
                'stock_notes': {'KR:069500': '논리: 지수 분산. 손절 6% 아래.', 'rm -rf': 'x', 'AAPL': ''}}
    results = [{'action': {'type': 'buy', 'symbol': 'KR:069500', 'quantity': 118}, 'result': {}},
               {'action': {'type': 'exchange', 'source': 'USD', 'amount': 10000}, 'error': '잔액 부족'}]
    ai_trader.write_notebook('claude', decision, results, {'KR': '정규장'}, NOON)
    ai_trader.write_notebook('claude', {'summary': '관망', 'stock_notes': {'KR:069500': '실적 확인 전까지 보유'}}, [], {'KR': '정규장'},
                             NOON.replace(minute=59))
    journal = (tmp_path / 'claude-notes' / 'journal' / '2026-10-06.md').read_text()
    assert journal.startswith('# Claude 투자 일지 · 2026-10-06\n')
    assert '## 12:30 · KR 정규장\n**판단**: KODEX 200 분할 매수\n- buy KR:069500 118주: 체결\n- 환전 USD 10000: 거절 (잔액 부족)\n\n- 가설:' in journal
    assert '## 12:59 · KR 정규장\n**판단**: 관망' in journal
    notes = ai_trader.stock_entries('claude', 'KR:069500')
    assert notes == ['## 2026-10-06 12:30 KST\n논리: 지수 분산. 손절 6% 아래.', '## 2026-10-06 12:59 KST\n실적 확인 전까지 보유']
    assert ai_trader.noted_symbols('claude') == ['KR:069500']   # bad and empty notes are not stored


def test_a_stock_keeps_its_latest_thirty_notes(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_trader, 'STATE', tmp_path)
    for i in range(35):
        ai_trader.write_notebook('gpt', {'stock_notes': {'NVDA': f'메모 {i}'}}, [], {'US': '정규장'}, NOON)
    notes = ai_trader.stock_entries('gpt', 'NVDA')
    assert len(notes) == 30 and notes[0].endswith('메모 5') and notes[-1].endswith('메모 34')


def test_the_next_run_reads_yesterday_today_and_its_held_stocks(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_trader, 'STATE', tmp_path)
    yesterday = NOON.replace(day=5)
    ai_trader.write_notebook('claude', {'summary': '어제 판단', 'journal': '어제 메모', 'stock_notes': {'KR:069500': f'노트 {i}' for i in range(1)}}, [], {'KR': '정규장'}, yesterday)
    for i in range(1, 5):
        ai_trader.write_notebook('claude', {'stock_notes': {'KR:069500': f'노트 {i}', 'NVDA': '관심만'}}, [], {'KR': '정규장'}, NOON)
    ai_trader.write_notebook('claude', {'summary': '사흘 전', 'journal': '오래된 메모'}, [], {'KR': '정규장'}, NOON.replace(day=3))
    seen = ai_trader.notebook('claude', {'holdings': [{'symbol': 'KR:069500'}]}, NOON)
    assert '어제 메모' in seen['journal'] and '오래된 메모' not in seen['journal']
    assert seen['journal'].index('2026-10-05') < seen['journal'].index('2026-10-06')        # oldest first
    assert [line for line in seen['held_stock_notes']['KR:069500'].splitlines() if line and not line.startswith('##')] == ['노트 2', '노트 3', '노트 4']
    assert seen['other_noted_symbols'] == ['NVDA']
    assert ai_trader.fetch(None, {'type': 'notes', 'symbol': 'nvda'}, agent='claude')['notes'].endswith('관심만')
    assert ai_trader.fetch(None, {'type': 'notes', 'symbol': 'TSLA'}, agent='claude')['notes'] == '이 종목에 남긴 노트가 없다.'
    assert 'error' in ai_trader.fetch(None, {'type': 'notes', 'symbol': '../x'}, agent='claude')
    assert ai_trader.notebook('gemini', {'holdings': []}, NOON) is None


def test_a_long_journal_is_cut_at_a_run_heading(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_trader, 'STATE', tmp_path)
    for i in range(40):
        ai_trader.write_notebook('claude', {'summary': f'판단 {i}', 'journal': '관찰 ' * 60}, [], {'KR': '정규장'}, NOON)
    journal = ai_trader.notebook('claude', {'holdings': []}, NOON)['journal']
    assert len(journal) <= ai_trader.PROMPT_JOURNAL_CHARS and journal.startswith('## 12:30') and '판단 39' in journal


def test_a_run_carries_its_notes_into_the_next_run(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_trader, 'STATE', tmp_path)
    ask, prompts = scripted(
        json.dumps({'step': 'decide', 'summary': '삼성 관찰', 'journal': '반도체 업황 반등 신호 확인 필요', 'stock_notes': {'KR:005930': '목표 8만 원'}, 'actions': []}),
        json.dumps({'step': 'research', 'requests': [{'type': 'notes', 'symbol': 'KR:005930'}]}),
        json.dumps({'step': 'decide', 'summary': '노트대로 관망', 'actions': []}))
    first = ai_trader.run('claude', client=FakeClient(), ask=ask)
    assert first['journal'] == '반도체 업황 반등 신호 확인 필요' and first['stock_notes'] == {'KR:005930': '목표 8만 원'}
    ai_trader.run('claude', client=FakeClient(), ask=ask)
    assert '내 투자 노트' in prompts[1] and '반도체 업황 반등 신호 확인 필요' in prompts[1] and '"KR:005930"' in prompts[1]
    assert '목표 8만 원' in prompts[2]   # the notes it asked for came back
    assert '내 투자 노트' not in prompts[0]


def test_a_dry_run_or_failed_run_writes_no_notes(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_trader, 'STATE', tmp_path)
    ask, _ = scripted(json.dumps({'step': 'decide', 'journal': '시험', 'stock_notes': {'NVDA': '시험'}, 'actions': []}))
    ai_trader.run('claude', dry_run=True, client=FakeClient(), ask=ask)
    def broken(prompt): raise RuntimeError('CLI down')
    with pytest.raises(RuntimeError): ai_trader.run('claude', client=FakeClient(), ask=broken)
    assert not (tmp_path / 'claude-notes').exists()


# Long memory: strategy, daily summaries, lessons, the 30 % pace ------------------

from datetime import timedelta


def test_a_new_strategy_replaces_the_file_and_keeps_the_old_one(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_trader, 'STATE', tmp_path)
    write = lambda d, at: ai_trader.write_notebook('claude', d, [], {'US': '정규장'}, at)
    write({'strategy': 'US 상승 · 롱 위주'}, NOON)
    write({'summary': '방침 그대로'}, NOON.replace(minute=40))                    # no strategy: unchanged
    assert (tmp_path / 'claude-notes' / 'strategy.md').read_text() == 'US 상승 · 롱 위주\n'
    write({'strategy': 'US 하락 전환 · 인버스 ' + '가' * 3000, 'lesson': 123}, NOON.replace(hour=4))   # a bad lesson is ignored
    assert len(ai_trader.read(ai_trader.strategy_path('claude'))) == ai_trader.STRATEGY_CHARS
    history = sorted((tmp_path / 'claude-notes' / 'strategy_history').iterdir())
    assert [p.name for p in history] == ['2026-10-06T1330.md']   # replaced at 13:30 KST and history[0].read_text() == 'US 상승 · 롱 위주\n'
    assert not ai_trader.lessons('claude')
    for i in range(25): write({'strategy': f'방침 {i}'}, NOON + timedelta(hours=i + 1))
    assert len(list((tmp_path / 'claude-notes' / 'strategy_history').iterdir())) == 20


def test_lessons_are_one_dated_line_each_and_the_latest_fifty_are_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_trader, 'STATE', tmp_path)
    for i in range(55):
        ai_trader.write_notebook('gpt', {'lesson': f'QQQ/손절:\n추격 매수 {i}'}, [], {'US': '정규장'}, NOON)
    lines = (tmp_path / 'gpt-notes' / 'lessons.md').read_text().splitlines()
    assert len(lines) == 50 and lines[0] == '- 2026-10-06 QQQ/손절: 추격 매수 5' and lines[-1].endswith('추격 매수 54')
    assert len(ai_trader.notebook('gpt', {'holdings': []}, NOON)['lessons']) == 30


def test_four_weeks_of_daily_summaries_drop_the_oldest_past_8000_characters(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_trader, 'STATE', tmp_path)
    today = NOON.astimezone(ai_trader.SEOUL).date()
    for n in range(1, 31):
        path = ai_trader.daily_path('claude', today - timedelta(days=n)); path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'{n}일 전 요약 ' + '가' * 590)
    days = ai_trader.notebook('claude', {'holdings': []}, NOON)['recent_days']
    assert len(days) <= ai_trader.PROMPT_RECENT_DAYS_CHARS
    assert days.endswith('1일 전 요약 ' + '가' * 590) and '29일 전' not in days   # four weeks at most, newest kept
    shown = [int(line.split('일 전')[0]) for line in days.splitlines() if '일 전 요약' in line]
    assert shown == sorted(shown, reverse=True) and len(shown) < 28              # oldest first, the oldest cut


def test_yesterdays_journal_is_summarized_once_on_the_first_run_of_a_day(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_trader, 'STATE', tmp_path)
    now = datetime.now(timezone.utc)
    ai_trader.write_notebook('claude', {'summary': '어제 SPY 매수', 'journal': '가설: 금리 하락'}, [], {'US': '정규장'}, now - timedelta(days=1))
    def failing(prompt):
        if prompt.startswith(ai_trader.SUMMARY_PROMPT[:20]): raise RuntimeError('CLI busy')
        return json.dumps({'step': 'decide', 'sources': [SOURCE_ID], 'actions': []})
    assert ai_trader.run('claude', client=FakeClient(), ask=failing)['status'] == 'ok'   # trading goes on
    yesterday = ai_trader.daily_path('claude', ai_trader.korea_day(now) - timedelta(days=1))
    assert not yesterday.exists()
    ask, prompts = scripted(json.dumps({'summary': '금리 하락 가설로 SPY 매수'}), json.dumps({'step': 'decide', 'actions': []}),
                            json.dumps({'step': 'decide', 'actions': []}))
    ai_trader.run('claude', client=FakeClient(), ask=ask)                                  # the next run tries again
    assert yesterday.read_text() == '금리 하락 가설로 SPY 매수\n'
    assert '어제 SPY 매수' in prompts[0] and '금리 하락 가설로 SPY 매수' in prompts[1]      # summarized, then remembered
    ai_trader.run('claude', client=FakeClient(), ask=ask)
    assert len(prompts) == 3                                                               # not summarized twice


def test_the_30_percent_pace(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_trader, 'STATE', tmp_path)
    monkeypatch.setenv('AI_TARGET_START', '2025-10-06')
    assert ai_trader.target_pace('claude', 5.0, NOON) == {'days_elapsed': 365, 'target_return_pct_to_date': 30.0, 'pace_gap_pct': -25.0}
    monkeypatch.setenv('AI_TARGET_START', '2026-10-06')
    assert ai_trader.target_pace('claude', None, NOON) == {'days_elapsed': 0, 'target_return_pct_to_date': 0.0, 'pace_gap_pct': None}
    monkeypatch.delenv('AI_TARGET_START')   # else the first logged run, saved once
    (tmp_path / 'claude.jsonl').write_text(json.dumps({'time': '2026-10-04T23:30:00+00:00'}) + '\n')   # 10-05 in Korea
    assert ai_trader.target_pace('claude', 1.0, NOON)['days_elapsed'] == 1
    ai_trader.save_start_date('claude')
    assert (tmp_path / 'claude-notes' / 'start_date').read_text() == '2026-10-05\n'


def test_a_dry_run_writes_no_file_at_all(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_trader, 'STATE', tmp_path)
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    ai_trader.write_notebook('claude', {'summary': '어제'}, [], {'US': '정규장'}, yesterday)
    before = sorted(p for p in tmp_path.rglob('*'))
    ask, prompts = scripted(json.dumps({'step': 'decide', 'strategy': '새 방침', 'lesson': 'X: 교훈', 'actions': []}))
    entry = ai_trader.run('claude', dry_run=True, client=FakeClient(), ask=ask)
    assert sorted(p for p in tmp_path.rglob('*')) == before and len(prompts) == 1            # no summary call either
    assert 'target_return_pct_to_date' in prompts[0] and entry['strategy'] == '새 방침' and entry['lesson'] == 'X: 교훈'


def test_the_notebook_gives_way_to_keep_the_prompt_under_the_cli_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_trader, 'STATE', tmp_path)
    notes = {'strategy': '방침 ' * 500, 'lessons': [f'- 교훈 {i} ' + '가' * 190 for i in range(30)],
             'recent_days': '\n\n'.join(f'### 2026-09-{d:02d}\n' + '나' * 280 for d in range(1, 29)),
             'journal': '\n\n'.join(f'## {h:02d}:00 · US\n' + '다' * 400 for h in range(20)), 'held_stock_notes': {}, 'other_noted_symbols': []}
    big_world = {'items': [{'id': str(i), 'body': '라' * 700} for i in range(40)]}   # about 84 KB, like a busy news day
    prompt = ai_trader.build_prompt('Claude', {'US': '정규장'}, {}, {}, [], [], 2, big_world, notes)
    assert len(prompt.encode()) <= ai_trader.PROMPT_LIMIT_BYTES
    assert notes['strategy'] in prompt and '### 2026-09-01' not in prompt               # the policy stays, old days go first
    small = ai_trader.build_prompt('Claude', {'US': '정규장'}, {}, {}, [], [], 2, None, notes)
    assert '### 2026-09-01' in small and '- 교훈 0 ' in small                              # nothing cut when it fits
