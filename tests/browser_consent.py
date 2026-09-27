"""Sign-up agreement, the one-time consent for pre-agreement accounts, and the policy
pages, on desktop and mobile, against the disposable browser fixture only."""
import time
from playwright.sync_api import sync_playwright, expect

BASE = 'http://browserweb:8000'
PASSWORD = 'browser-fixture-password'

with sync_playwright() as p:
    browser = p.chromium.launch(args=['--no-sandbox'])
    viewer = p.request.new_context(base_url=BASE)
    session = viewer.get('/api/session').json()
    viewer_name = f'viewer_{time.time_ns()}'[:32]
    body = {'username': viewer_name, 'password': 'abcd1234'}
    assert viewer.post('/api/register', headers={'x-csrf-token': session['csrf']}, data=body | {
        'password_confirm': 'abcd1234', 'agree_terms': True, 'over_14': True, 'notice_version': session['notice_version']}).ok
    assert viewer.post('/api/login', headers={'x-csrf-token': session['csrf']}, data=body).ok
    for width in (1440, 390):
        page = browser.new_page(viewport={'width': width, 'height': 1000 if width > 500 else 844})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        fits = 'document.documentElement.scrollWidth <= innerWidth + 1'

        # Policy pages: readable signed out, reachable from the footer, no sideways scroll.
        page.goto(BASE)
        expect(page.locator('footer .policy-link')).to_have_text('개인정보 처리방침')
        page.locator('footer .policy-link').click()
        expect(page.locator('h1')).to_have_text('개인정보 처리방침')
        expect(page.locator('.policy')).to_contain_text('7829hw@gmail.com')
        expect(page.locator('.policy')).to_contain_text('jack3618@knu.ac.kr')
        assert page.evaluate(fits)
        page.screenshot(path=f'/artifacts/privacy-policy-{width}.png', full_page=True)
        page.goto(BASE + '/terms')
        expect(page.locator('h1')).to_have_text('이용약관')
        assert page.evaluate(fits)

        # The sign-up form links both documents next to its two required boxes.
        page.goto(BASE + '/#signup')
        form = page.locator('#registerForm')
        expect(form.locator('a[href="/privacy"]')).to_be_visible()
        expect(form.locator('a[href="/terms"]')).to_be_visible()
        expect(form).to_contain_text('순위가 공개됩니다')
        assert page.evaluate(fits)
        page.screenshot(path=f'/artifacts/signup-agreement-{width}.png', full_page=True)

        # A pre-agreement account is asked once, cannot dismiss the question, and joins on agreeing.
        legacy = f'browser_legacy_{width}'
        assert legacy not in [r['username'] for r in viewer.get('/api/ranking').json()['rows']]
        assert viewer.get('/api/portfolios/' + legacy).status == 404
        page.goto(BASE)
        page.locator('#username').fill(legacy)
        page.locator('#password').fill(PASSWORD)
        page.locator('#loginSubmit').click()
        dialog = page.locator('#consentDialog')
        expect(dialog).to_be_visible()
        page.keyboard.press('Escape')
        expect(dialog).to_be_visible()
        dialog.get_by_role('button', name='동의하고 계속').click()
        expect(page.locator('#consentError')).to_have_text('두 항목에 모두 동의해야 계속 이용할 수 있습니다.')
        assert page.evaluate(fits)
        page.screenshot(path=f'/artifacts/consent-{width}.png')
        page.locator('#consentOver14').check()
        page.locator('#consentAgree').check()
        dialog.get_by_role('button', name='동의하고 계속').click()
        expect(dialog).to_be_hidden()
        expect(page.locator('#toasts')).to_contain_text('동의가 저장되었습니다.')
        assert viewer.get('/api/portfolios/' + legacy).status == 200
        page.reload()
        expect(page.locator('#dashboard')).to_be_visible()
        page.wait_for_timeout(1000)
        expect(dialog).to_be_hidden()
        assert not errors, errors
        page.close()
    token = viewer.get('/api/session').json()['csrf']
    assert viewer.post('/api/account/delete', headers={'x-csrf-token': token}, data={'password': 'abcd1234'}).ok
    viewer.dispose()
    browser.close()
print('Consent desktop/mobile passed: policy pages, sign-up agreement, pre-agreement consent, layout.')
