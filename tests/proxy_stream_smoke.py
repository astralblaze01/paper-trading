"""HTTP and HTTPS production nginx configs against isolated fixture app."""
import asyncio
import json
import time
import httpx


async def login(client, base, name):
    session = (await client.get(base+'/api/session')).json()
    credentials = {'username': name, 'password': 'proxy-fixture-password'}
    headers = {'x-csrf-token': session['csrf']}
    response = await client.post(base+'/api/register', json=credentials | {'password_confirm': credentials['password']}, headers=headers)
    response.raise_for_status()
    (await client.post(base+'/api/login', json=credentials, headers=headers)).raise_for_status()


async def probe(base, name):
    async with httpx.AsyncClient(verify=False, timeout=110) as client:
        await login(client, base, name)
        started = time.monotonic()
        async with client.stream('GET', base+'/api/market-stream/AAPL') as response:
            assert response.status_code == 200
            assert response.headers['content-type'].startswith('text/event-stream')
            assert response.headers['cache-control'] == 'no-store'
            name = None
            first = None
            heartbeat = 0
            prices = set()
            async for line in response.aiter_lines():
                if line.startswith('event: '):
                    name = line[7:]
                if line.startswith('data: '):
                    data = json.loads(line[6:])
                    if name == 'snapshot' and first is None:
                        first = time.monotonic()-started
                        assert first < 2, first
                    if name in ('snapshot','quote'):
                        prices.add(data['quote']['native_price'])
                    if name == 'heartbeat':
                        heartbeat += 1
                    if time.monotonic()-started > 95:
                        assert first is not None and heartbeat >= 6 and len(prices) >= 2, (first,heartbeat,prices)
                        print(f'{base}: first snapshot {first*1000:.1f}ms, {heartbeat} named heartbeats, idle connection >95s passed', flush=True)
                        return


async def main():
    async with httpx.AsyncClient() as control:
        await login(control, 'http://browserweb:8000', 'proxy_control_'+str(int(time.time())))
        (await control.post('http://browserweb:8000/internal/test-quote/AAPL?price=200')).raise_for_status()
        (await control.post('http://browserweb:8000/internal/test-worker?paused=true')).raise_for_status()
        try:
            tasks = [asyncio.create_task(probe(base, 'proxy_'+str(i)+'_'+str(int(time.time()))))
                     for i,base in enumerate(('http://proxy', 'https://tlsproxy'))]
            await asyncio.sleep(2)
            (await control.post('http://browserweb:8000/internal/test-quote/AAPL?price=201')).raise_for_status()
            await asyncio.wait_for(asyncio.gather(*tasks), 110)
        finally:
            await control.post('http://browserweb:8000/internal/test-worker?paused=false')

asyncio.run(main())
