"""Korean symbol master refresh: an empty download never replaces a good master."""
import io
import zipfile
from types import SimpleNamespace

import httpx
import pytest
import app.kr_symbols as kr
from app.kr_symbols import MASTER_KEY


def master_file(*listings):
    """A KIS *_code.mst archive: fixed-width EUC-KR lines, code in 0:9 and name in 21:61."""
    content = b''.join(code.ljust(9).encode() + b' ' * 12 + name.encode('euc-kr').ljust(40) + b' ' * 20 + b'\n'
                       for code, name in listings)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('master.mst', content)
    return buffer.getvalue()


class Store:
    """The two redis_cache calls the master uses, with the TTL of every write."""
    def __init__(self, rows=None):
        self.values, self.writes = ({MASTER_KEY: rows} if rows is not None else {}), []
    def get_json(self, key): return self.values.get(key)
    def set_json(self, key, value, ttl):
        self.values[key] = value; self.writes.append((key, len(value), ttl)); return True


def serve(monkeypatch, files, store):
    """kospi/kosdaq downloads answer with files[exchange]; returns the list of URLs fetched."""
    fetched = []
    def handler(request):
        fetched.append(str(request.url))
        exchange = 'kosdaq' if 'kosdaq' in str(request.url) else 'kospi'
        return httpx.Response(200, content=files[exchange])
    real = httpx.Client
    monkeypatch.setattr(kr, 'httpx', SimpleNamespace(Client=lambda timeout: real(transport=httpx.MockTransport(handler), timeout=timeout)))
    monkeypatch.setattr(kr, 'redis_cache', store)
    return fetched


GOOD = {'kospi': master_file(('005930', '삼성전자'), ('000660', 'SK하이닉스')), 'kosdaq': master_file(('035720', '카카오'))}
KNOWN = [{'symbol': 'KR:005380', 'name': '현대차', 'category': 'kr', 'currency': 'KRW', 'exchange': 'kospi'}]


def test_a_normal_refresh_replaces_the_master_with_every_exchange(monkeypatch):
    store = Store(list(KNOWN))
    serve(monkeypatch, GOOD, store)
    assert kr.refresh_master() == 3
    assert [r['symbol'] for r in store.values[MASTER_KEY]] == ['KR:005930', 'KR:000660', 'KR:035720']
    assert store.writes == [(MASTER_KEY, 3, 172800)]


@pytest.mark.parametrize('files', [
    {'kospi': master_file(), 'kosdaq': master_file()},
    {'kospi': GOOD['kospi'], 'kosdaq': master_file()},            # one exchange file came back empty
    {'kospi': master_file(('123', '')), 'kosdaq': GOOD['kosdaq']},  # lines present, nothing parseable
])
def test_an_empty_download_keeps_the_last_good_master(monkeypatch, files):
    store = Store(list(KNOWN))
    serve(monkeypatch, files, store)
    with pytest.raises(ValueError, match='empty Korean symbol master'):
        kr.refresh_master()
    assert store.values[MASTER_KEY] == KNOWN and store.writes == []
    assert kr.search_master('현대') == KNOWN


def test_an_empty_first_download_stores_nothing_and_a_later_one_recovers(monkeypatch):
    store = Store()
    serve(monkeypatch, {'kospi': master_file(), 'kosdaq': master_file()}, store)
    with pytest.raises(ValueError):
        kr.refresh_master()
    assert store.values == {} and kr.search_master('삼성') == []   # still "no master yet"
    serve(monkeypatch, GOOD, store)
    assert kr.refresh_master() == 3 and kr.search_master('삼성')[0]['symbol'] == 'KR:005930'
