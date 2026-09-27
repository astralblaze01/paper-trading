"""Desktop/mobile privacy controls against the disposable browser fixture only."""
import time
from playwright.sync_api import sync_playwright, expect

BASE = 'http://browserweb:8000'
PASSWORD = 'privacy-fixture-123'

with sync_playwright() as p:
    browser = p.chromium.launch(args=['--no-sandbox'])
    for width in (1440, 390):
        name = f'privacy_{width}_{time.time_ns()}'[:32]
        viewer_name = f'viewer_{width}_{time.time_ns()}'[:32]
        owner = p.request.new_context(base_url=BASE)
        viewer = p.request.new_context(base_url=BASE)
        for context, username in ((owner, name), (viewer, viewer_name)):
            token = context.get('/api/session').json()['csrf']
            body = {'username':username, 'password':PASSWORD}
            assert context.post('/api/register', headers={'x-csrf-token':token}, data=body | {'password_confirm':PASSWORD}).ok
            assert context.post('/api/login', headers={'x-csrf-token':token}, data=body).ok
        profile = owner.get('/api/profile').json()
        assert profile['profile_public'] is False and profile['ranking_public'] is False
        page = browser.new_page(viewport={'width':width,'height':1000 if width>500 else 844})
        errors=[]
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(BASE)
        page.locator('#username').fill(name)
        page.locator('#password').fill(PASSWORD)
        page.locator('#loginSubmit').click()
        expect(page.locator('#dashboard')).to_be_visible()
        page.goto(BASE + '/#portfolio')
        form=page.locator('#myProfile .privacy-settings')
        expect(form).to_be_visible()
        profile_box=form.locator('[name=profile_public]')
        ranking_box=form.locator('[name=ranking_public]')
        expect(profile_box).not_to_be_checked()
        expect(ranking_box).not_to_be_checked()
        assert viewer.get('/api/portfolios/' + name).status == 404
        profile_box.check()
        ranking_box.check()
        # Periodic portfolio/ranking refresh must not discard an unsaved choice.
        page.wait_for_timeout(11000)
        expect(profile_box).to_be_checked()
        expect(ranking_box).to_be_checked()
        form.get_by_role('button',name='공개 설정 저장').click()
        expect(page.locator('#toasts')).to_contain_text('공개 설정을 저장했습니다.')
        assert viewer.get('/api/portfolios/' + name).status == 200
        assert name in [row['username'] for row in viewer.get('/api/ranking').json()['rows']]
        page.reload()
        expect(profile_box).to_be_checked()
        expect(ranking_box).to_be_checked()
        profile_box.uncheck()
        ranking_box.uncheck()
        with page.expect_response(lambda r:'/api/profile/privacy' in r.url and r.request.method=='POST') as saved:
            form.get_by_role('button',name='공개 설정 저장').click()
        assert saved.value.ok
        assert viewer.get('/api/portfolios/' + name).status == 404
        assert viewer.get('/api/performance/' + name).status == 404
        assert name not in [row['username'] for row in viewer.get('/api/ranking').json()['rows']]
        page.reload()
        expect(profile_box).not_to_be_checked()
        expect(ranking_box).not_to_be_checked()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        page.screenshot(path=f'/artifacts/privacy-{width}.png', full_page=True)
        assert not errors, errors
        # Remove only accounts created by this test.
        for context in (owner, viewer):
            token=context.get('/api/session').json()['csrf']
            assert context.post('/api/account/delete',headers={'x-csrf-token':token},data={'password':PASSWORD}).ok
            context.dispose()
        page.close()
    browser.close()
print('Privacy desktop/mobile passed: defaults, explicit sharing, persistence, withdrawal, access control, layout.')
