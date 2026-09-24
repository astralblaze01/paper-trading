"""Run against the isolated SSE compose; restart browserweb after the ready marker."""
import time
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

with sync_playwright() as p:
    browser = p.chromium.launch(args=['--no-sandbox'])
    page = browser.new_page()
    page.goto('http://browserweb:8000/')
    username = 'restart_'+str(int(time.time()))
    page.evaluate("""async username=>{const s=await api('session');csrf=s.csrf;
        await api('register',{username,password:'abcd1234',password_confirm:'abcd1234'});
        await api('login',{username,password:'abcd1234'});await boot();openStock('MSFT');}""", username)
    expect(page.locator('#quoteConnection')).to_contain_text('연결됨')
    page.locator('#displayCurrency').select_option('USD')
    assert page.request.post('http://browserweb:8000/internal/test-quote/MSFT?price=321').ok
    expect(page.locator('#detailPrice strong')).to_have_text('$321.00')
    requests = []
    page.on('request', lambda r: requests.append(r.url))
    Path('/artifacts/sse-restart-ready').write_text(username)
    # Restart erases fixture overrides; the new process publishes 100 again.
    expect(page.locator('#detailPrice strong')).to_have_text('$100.00', timeout=90000)
    expect(page.locator('#quoteConnection')).to_contain_text('연결됨')
    assert any('/api/market-stream/MSFT' in url for url in requests)
    assert page.evaluate('quoteSource.readyState===1&&quoteRetry===null&&restTimer===null')
    print('Web process restart: fresh snapshot, one EventSource, no REST fallback passed', flush=True)
    browser.close()
