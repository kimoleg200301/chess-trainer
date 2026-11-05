"""Account isolation and out-of-order statistics responses, with isolated API fixtures."""
from copy import deepcopy
import os
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

from browser_support import chromium_options

BASE = os.environ.get('TEST_FRONTEND_URL', 'http://127.0.0.1:5174').rstrip('/')


class Fixture:
    def __init__(self, browser):
        self.context = browser.new_context(viewport={'width': 1280, 'height': 900})
        self.page = self.context.new_page()
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.user = 'first'
        self.solved = 2
        self.hold_stats = False
        self.held = []
        self.hold_logout = False
        self.held_logout = []
        self.auth_requests = []
        self.context.add_cookies([dict(name='fixture-session', value='first', url=BASE)])
        self.context.route('**/api/**', self.respond)
        self.context.route('https://fonts.googleapis.com/**', lambda route: route.abort())

    def respond(self, route):
        path = urlparse(route.request.url).path.removeprefix('/api')
        if path == '/auth/login':
            self.auth_requests.append(path)
            self.user = route.request.post_data_json['username']
            return route.fulfill(json={'username': self.user},
                                 headers={'Set-Cookie': f'fixture-session={self.user}; Path=/; SameSite=Lax'})
        if self.user is None:
            return route.fulfill(status=401, json={'detail': 'Войдите в аккаунт.'})
        if path == '/auth/logout':
            self.auth_requests.append(path)
            self.user = None
            if self.hold_logout:
                self.held_logout.append(route)
                return
            return route.fulfill(status=204, headers={'Set-Cookie': 'fixture-session=; Max-Age=0; Path=/'})
        if path == '/auth/me':
            result = {'username': self.user}
        elif path == '/meta':
            result = dict(total_puzzles=100, themes=[], min_rating=400, max_rating=2000,
                          collections=[dict(id='lichess', name='Lichess', count=100)])
        elif path == '/training/current':
            result = dict(attempt=None, solution=[], game_url='')
        elif path == '/stats':
            result = dict(solved=self.solved, total_attempts=self.solved, accuracy=100,
                          streak=self.solved, due_count=0, average_solution_ms=10000,
                          timed_solutions=self.solved, puzzle_rating=1200, rated_attempts=2,
                          by_theme=[], recent=[])
            if self.hold_stats:
                self.held.append((route, deepcopy(result)))
                return
        else:
            self.errors.append(f'Unexpected API request: {route.request.method} {path}')
            return route.fulfill(status=500, json={'detail': 'Unexpected fixture request'})
        route.fulfill(json=result)

    def open(self):
        self.page.goto(BASE + '/')
        expect(self.page.locator('.setup-summary')).to_contain_text('2 задач решено')

    def navigate(self, name):
        self.page.get_by_role('navigation', name='Главная навигация').get_by_role('link', name=name, exact=True).click()

    def hold_next_stats(self):
        self.hold_stats = True
        with self.page.expect_request('**/api/stats'):
            self.navigate('Прогресс')
        # The loading state also guarantees that the navigation was committed.
        expect(self.page.get_by_role('button', name='Обновить', exact=True)).to_be_disabled()
        assert self.held, 'Expected a delayed statistics response'

    def release(self, index=0, status=200):
        route, result = self.held.pop(index)
        error = 'Сессия истекла.' if status == 401 else 'Устаревший сбой статистики.'
        with self.page.expect_response('**/api/stats') as response:
            route.fulfill(status=status, json=result if status == 200 else {'detail': error})
        response.value.json()
        self.page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')

    def login(self, username):
        self.page.get_by_label('Имя пользователя').fill(username)
        self.page.get_by_label('Пароль', exact=True).fill('fixture-password')
        self.page.get_by_role('button', name='Начать практику').click()
        expect(self.page.locator('.username')).to_have_text(username)

    def close(self):
        assert not self.errors, self.errors
        assert not self.held, 'Every delayed request must be resolved'
        assert not self.held_logout, 'Every logout must be resolved'
        self.context.close()


def solved_value(page):
    return page.locator('.stat-card').filter(has=page.get_by_role('heading', name='Задач решено', exact=True)).locator('strong')


def check_response_order(browser, late_status):
    fixture = Fixture(browser)
    fixture.open()
    fixture.hold_next_stats()
    fixture.navigate('Главная')
    fixture.solved = 9
    fixture.hold_next_stats()
    fixture.release(index=1)
    expect(solved_value(fixture.page)).to_have_text('9')
    expect(fixture.page.get_by_role('button', name='Обновить', exact=True)).to_be_enabled()
    fixture.release(status=late_status)
    expect(solved_value(fixture.page)).to_have_text('9')
    expect(fixture.page.get_by_role('alert')).to_have_count(0)
    fixture.close()
    print(f'PASS statistics: an older {late_status} cannot replace the latest result or add an error')


def check_account_isolation(browser, username, late_status):
    fixture = Fixture(browser)
    fixture.open()
    fixture.hold_next_stats()
    fixture.page.get_by_role('button', name='Выйти', exact=True).click()
    expect(fixture.page.get_by_role('heading', name='Вернёмся к практике')).to_be_visible()
    fixture.hold_stats = False
    fixture.solved = 7
    fixture.login(username)
    expect(fixture.page.locator('.setup-summary')).to_contain_text('7 задач решено')
    fixture.release(status=late_status)
    expect(fixture.page.locator('.username')).to_have_text(username)
    expect(fixture.page.locator('.setup-summary')).to_contain_text('7 задач решено')
    expect(fixture.page.get_by_role('alert')).to_have_count(0)
    fixture.navigate('Прогресс')
    expect(solved_value(fixture.page)).to_have_text('7')
    fixture.close()
    print(f'PASS account scope: delayed {late_status} ignored after login as {username}')


def check_current_expiry(browser):
    fixture = Fixture(browser)
    fixture.open()
    fixture.hold_next_stats()
    fixture.release(status=401)
    expect(fixture.page.get_by_role('heading', name='Вернёмся к практике')).to_be_visible()
    fixture.hold_stats = False
    fixture.solved = 4
    fixture.login('first')
    expect(solved_value(fixture.page)).to_have_text('4')
    fixture.close()
    print('PASS current session: a current 401 still requires login and statistics reload')


def check_logout_before_login(browser):
    fixture = Fixture(browser)
    fixture.open()
    fixture.hold_next_stats()
    fixture.hold_logout = True
    fixture.page.get_by_role('button', name='Выйти', exact=True).click()
    expect(fixture.page.get_by_role('button', name='Выйти', exact=True)).to_be_disabled()
    fixture.release(status=401)
    expect(fixture.page.get_by_role('heading', name='Вернёмся к практике')).to_be_visible()
    fixture.page.get_by_label('Имя пользователя').fill('second')
    fixture.page.get_by_label('Пароль', exact=True).fill('fixture-password')
    expect(fixture.page.locator('.login-submit')).to_be_disabled()
    expect(fixture.page.locator('.login-submit')).to_contain_text('Завершаем выход')
    fixture.page.get_by_label('Пароль', exact=True).press('Enter')
    fixture.page.locator('.login-card form').dispatch_event('submit')
    fixture.page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
    assert fixture.auth_requests == ['/auth/logout'], 'Login must wait for the logout cookie to expire'
    assert len(fixture.held_logout) == 1
    fixture.held_logout.pop().fulfill(status=204, headers={'Set-Cookie': 'fixture-session=; Max-Age=0; Path=/'})
    expect(fixture.page.get_by_role('button', name='Начать практику')).to_be_enabled()
    fixture.hold_stats = False
    fixture.solved = 7
    fixture.login('second')
    expect(fixture.page.locator('.setup-summary')).to_contain_text('7 задач решено')
    assert fixture.auth_requests == ['/auth/logout', '/auth/login']
    cookies = {cookie['name']: cookie['value'] for cookie in fixture.context.cookies()}
    assert cookies.get('fixture-session') == 'second', 'The new login cookie must survive the earlier logout'
    fixture.close()
    print('PASS authentication order: expired session waits for pending logout before login, including Enter/form submit')


if __name__ == '__main__':
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**chromium_options(), args=['--no-sandbox', '--disable-features=LocalNetworkAccessChecks'])
        for status in (200, 503):
            check_response_order(browser, status)
        for username in ('first', 'second'):
            for status in (200, 401, 503):
                check_account_isolation(browser, username, status)
        check_current_expiry(browser)
        check_logout_before_login(browser)
        browser.close()
