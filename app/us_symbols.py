"""KIS overseas master files: Korean names for US-listed securities.

Finnhub's symbol search only knows English names, while market lists show
the Korean names KIS provides. This list makes those names searchable."""
import io
import re
import time
import zipfile

import httpx

from .instruments import valid_symbol
from .redis_cache import redis_cache

MASTER_URLS = (
    ('NAS', 'https://new.real.download.dws.co.kr/common/master/nasmst.cod.zip'),
    ('NYS', 'https://new.real.download.dws.co.kr/common/master/nysmst.cod.zip'),
    ('AMS', 'https://new.real.download.dws.co.kr/common/master/amsmst.cod.zip'),
)
MASTER_KEY = 'market:symbols:us'
HANGUL = re.compile('[가-힣ㄱ-ㅎㅏ-ㅣ]')
_loaded = (0.0, [])  # ~13k rows; parsed once per process for five minutes.
_names = (None, {}, {})  # see _index()


def _rows():
    global _loaded
    if time.monotonic() - _loaded[0] > 300 or not _loaded[1]:
        _loaded = (time.monotonic(), redis_cache.get_json(MASTER_KEY) or [])
    return _loaded[1]



def _index():
    """(rows, {symbol: row}, {symbol: Korean name}) for the current master list.

    Rebuilt only when _rows() hands back a newly loaded list. Names are rendered on
    first use: rendering all 12k at once (5.6k ETF names) took most of a second."""
    global _names
    rows = _rows()
    if _names[0] is not rows:
        _names = (rows, {row['symbol']: row for row in rows}, {})
    return _names


def name_of(symbol):
    """The Korean display name of a US listing (see us_names), or None when unknown.

    instrument() calls this on every quote."""
    from .us_names import POPULAR, korean_name
    _, by_symbol, names = _index()
    if symbol not in names:
        row = by_symbol.get(symbol)
        names[symbol] = (korean_name(row) if row else None) or POPULAR.get(symbol)
    return names[symbol]


def exchange_of(symbol):
    """'NAS', 'NYS' or 'AMS' from the KIS master, or None when the master lacks the listing."""
    row = _index()[1].get(symbol)
    return row.get('exchange') if row else None


def _search_text(value):
    return re.sub(r'[\s._()·-]+', '', value.casefold())


def parse_master(content, exchange):
    """Tab-separated rows: ..., symbol (4), Korean name (6), English name (7), type (8)."""
    rows = []
    for line in content.decode('cp949', errors='ignore').splitlines():
        fields = line.split('\t')
        if len(fields) < 9: continue
        symbol, korean = fields[4].strip().upper(), fields[6].strip()
        if not korean or not valid_symbol(symbol): continue
        rows.append({'symbol': symbol, 'name': korean, 'english': fields[7].strip(), 'exchange': exchange, 'etf': fields[8].strip() == '3'})
    return rows


def refresh_master():
    rows, seen = [], set()
    with httpx.Client(timeout=60) as client:
        for exchange, url in MASTER_URLS:
            response = client.get(url); response.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                for row in parse_master(archive.read(archive.namelist()[0]), exchange):
                    if row['symbol'] not in seen:
                        seen.add(row['symbol']); rows.append(row)
    if not rows: raise ValueError('empty US symbol master')
    redis_cache.set_json(MASTER_KEY, rows, 172800)
    return len(rows)


def has_hangul(query):
    return bool(HANGUL.search(query))


def search_master(query, limit=20):
    """Match the Korean name, ignoring spaces and punctuation.

    Exact names come first, then names starting with the query, then other
    matches; stocks before ETFs, shorter names first."""
    needle = _search_text(query)
    if not needle: return []
    matches = []
    for row in _rows():
        name = _search_text(row['name'])
        position = name.find(needle)
        if position >= 0:
            matches.append((name != needle, position != 0, row.get('etf', False), len(name), row['symbol'], row))
    matches.sort(key=lambda m: m[:5])
    return [m[5] for m in matches[:limit]]
