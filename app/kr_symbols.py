import io
import zipfile

import httpx

from .redis_cache import redis_cache

MASTER_URLS = (
    ('kospi', 'https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip'),
    ('kosdaq', 'https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip'),
)
MASTER_KEY = 'market:symbols:kr'


def parse_master(content, exchange):
    rows=[]
    for line in content.splitlines():
        if len(line)<61: continue
        code=line[0:9].decode('euc-kr',errors='ignore').strip()
        name=line[21:61].decode('euc-kr',errors='ignore').strip()
        if len(code)>6: code=code[-6:]
        if code.isdigit() and len(code)==6 and name:
            rows.append({'symbol':'KR:'+code,'name':name,'category':'kr','currency':'KRW','exchange':exchange})
    return rows


def refresh_master():
    rows=[]
    with httpx.Client(timeout=60) as client:
        for exchange,url in MASTER_URLS:
            response=client.get(url);response.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                name=archive.namelist()[0]
                rows.extend(parse_master(archive.read(name),exchange))
    redis_cache.set_json(MASTER_KEY,rows,172800)
    return len(rows)


def search_master(query,limit=30):
    rows=redis_cache.get_json(MASTER_KEY) or []
    needle=''.join(query.casefold().split())
    if not needle:return []
    result=[]
    for row in rows:
        if needle in ''.join(row['symbol'][3:].casefold().split()) or needle in ''.join(row['name'].casefold().split()):
            result.append(row)
            if len(result)>=limit:break
    return result
