"""scripts/ai_trader.py carries an AI's research requests and orders to the site's API, and nothing else."""
import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location('ai_trader', Path(__file__).resolve().parents[1] / 'scripts' / 'ai_trader.py')
ai_trader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ai_trader)

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
        return answers[len(prompts) - 1]
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
    assert client.posts == [('/api/orders', client.posts[0][1])] and client.posts[0][1]['symbol'] == 'AAPL'


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
    assert [p for p, _ in client.posts] == ['/api/fx/exchange', '/api/orders', '/api/orders']
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
    assert entry['results'] == [] and client.posts == []


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
    assert claude[claude.index('--model') + 1] == 'claude-sonnet-5-5' and claude[claude.index('--effort') + 1] == 'medium'
    assert codex[codex.index('-m') + 1] == 'gpt-6.1-sol' and 'model_reasoning_effort="medium"' in codex
