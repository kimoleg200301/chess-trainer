"""URL navigation regression checks against Vite, using isolated API fixtures.

Run Vite on 5174, then: python3 frontend/tests/routing-browser.py
Requires Python Playwright and Chromium. No real account data is changed.
"""
from copy import deepcopy
import importlib.util
import os
from pathlib import Path
import re
from urllib.parse import parse_qs, quote, urlparse

from playwright.sync_api import sync_playwright, expect

BASE = os.environ.get('TEST_FRONTEND_URL', 'http://127.0.0.1:5174').rstrip('/')
TESTS = Path(__file__).resolve().parent
FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'


def fixture_module(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), TESTS / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


analysis_fixtures = fixture_module('analysis-browser').fixtures
coach_fixtures = fixture_module('coach-browser').fixtures


class Fixture:
    def __init__(self, browser, authenticated=True):
        self.context = browser.new_context(viewport={'width': 1280, 'height': 900})
        self.authenticated = authenticated
        self.errors = []
        self.posts = []
        self.requests = []
        self.held = []
        self.hold_paths = set()
        self.games, self.reports = analysis_fixtures()
        self.coach = coach_fixtures()
        self.coach['profile'].update(status='tracking', syncing=False, baseline_ready=True)
        self.coach_attempt = dict(id=99, skill_id='fork', skill_title='Вилки', kind='decision',
            fen=FEN, starting_fen=FEN, learner_white=True, status='active', mistakes=0,
            hints_used=0, elapsed_ms=3000, last_move=None, ply_index=0, total_plies=1,
            prompt='Выбери сильный ход.', feedback='', is_new=True,
            source=dict(game_url='', move_number=3, own_game=True), solution=None, target_seconds=None)
        self.attempt = dict(id=41, puzzle_id='saved-position', fen=FEN, starting_fen=FEN,
            resumed=True, rating=1400, themes=[], status='active', turn='white', solved_plies=0,
            total_plies=3, mistakes=0, hints_used=0, last_move=None, elapsed_ms=15000,
            collection='lichess', rating_estimated=False)
        self.saved = dict(attempt=self.attempt, solution=[], game_url='')
        self.jobs = [dict(id=id_, source='chesscom', username=name, month='2026-11',
            status='completed', background_requested=False, total_games=0, completed_games=0,
            candidates_count=0, available_puzzles_count=0, puzzles_count=0, analysis_version=2,
            analysis_outdated=False, error=None, created_at='2026-11-04T00:00:00Z', games=[])
            for id_, name in [(7, 'FirstPlayer'), (8, 'SecondPlayer')]]
        self.context.route('**/api/**', self.respond)
        self.context.route('https://fonts.googleapis.com/**', lambda route: route.abort())
        self.context.on('page', lambda page: page.on('pageerror', lambda error: self.errors.append(str(error))))
        self.page = self.context.new_page()

    def respond(self, route):
        path = urlparse(route.request.url).path.removeprefix('/api')
        self.requests.append((route.request.method, path))
        if route.request.method == 'POST':
            self.posts.append((path, route.request.post_data_json))
        if path == '/auth/login':
            self.authenticated = True
            return route.fulfill(json={'username': 'tester'})
        if not self.authenticated:
            return route.fulfill(status=401, json={'detail': 'Войдите в аккаунт, чтобы продолжить.'})
        if path == '/auth/me': data = {'username': 'tester'}
        elif path == '/auth/logout':
            self.authenticated = False
            return route.fulfill(status=204)
        elif path == '/meta':
            data = dict(total_puzzles=100, themes=[], min_rating=400, max_rating=2000,
                        collections=[dict(id='lichess', name='Lichess', count=100)])
        elif path == '/stats':
            data = dict(solved=2, total_attempts=3, accuracy=66, streak=1, due_count=0,
                        average_solution_ms=12000, timed_solutions=2, puzzle_rating=1200,
                        rated_attempts=2, by_theme=[], recent=[])
        elif path in ('/training/current', '/training/41/resume'): data = self.saved
        elif path == '/training/41/time': data = self.attempt
        elif path == '/imports': data = {'jobs': self.jobs}
        elif path.startswith('/imports/'):
            data = next((job for job in self.jobs if path == f"/imports/{job['id']}"), None)
        elif path == '/game-analysis/games': data = {'games': self.games}
        elif path.startswith('/game-analysis/'):
            data = next((report for report in self.reports if path == f"/game-analysis/{report['id']}"), None)
        elif path == '/coach': data = self.coach
        elif path in ('/coach/practice/99', '/coach/practice/99/time'): data = self.coach_attempt
        elif path.startswith('/coach/practice/'): data = None
        else:
            self.errors.append(f'Unexpected API request: {route.request.method} {path}')
            return route.fulfill(status=500, json={'detail': 'Unexpected fixture request'})
        if path in self.hold_paths:
            self.held.append((route, deepcopy(data)))
        elif data is None:
            route.fulfill(status=404, json={'detail': 'Запись не найдена.'})
        else:
            route.fulfill(json=data)

    def release(self):
        self.hold_paths.clear()
        for route, data in self.held:
            route.fulfill(json=data)
        self.held.clear()

    def close(self):
        assert not self.errors, self.errors
        # Merely reading a URL must not create a training attempt or analysis job.
        assert not any(path in ('/training/start', '/coach/practice', '/game-analysis')
                       for path, _ in self.posts), self.posts
        self.context.close()


def assert_url(page, path):
    expect(page).to_have_url(BASE + path)


def check_main_routes(browser):
    fixture = Fixture(browser)
    page = fixture.page
    page.goto(BASE + '/progress')
    expect(page.get_by_role('heading', name='Прогресс складывается из ходов')).to_be_visible()
    assert_url(page, '/progress')
    routes = [('Главная', '/'), ('Тренер', '/coach'), ('Прогресс', '/progress'),
              ('Мои партии', '/games'), ('Анализ', '/analysis')]
    for name, path in routes:
        nav = page.get_by_role('navigation', name='Главная навигация')
        expect(nav.get_by_role('link', name=name, exact=True)).to_have_attribute('href', path)
        nav.get_by_role('link', name=name, exact=True).click()
        assert_url(page, path)
        expect(nav.get_by_role('link', name=name, exact=True)).to_have_attribute('aria-current', 'page')
        page.reload()
        expect(nav.get_by_role('link', name=name, exact=True)).to_be_visible()
        assert_url(page, path)
    page.go_back()
    assert_url(page, '/games')
    expect(page.locator('.game-library')).to_be_visible()
    page.go_forward()
    assert_url(page, '/analysis')
    expect(page.get_by_role('heading', name='Анализ партий', exact=True)).to_be_visible()
    with fixture.context.expect_page() as new_page:
        page.get_by_role('link', name='Прогресс', exact=True).click(modifiers=['Control'])
    popup = new_page.value
    expect(popup.get_by_role('heading', name='Прогресс складывается из ходов')).to_be_visible()
    assert_url(popup, '/progress')
    assert_url(page, '/analysis')
    fixture.close()
    print('PASS main URLs: links, active state, refresh, Back/Forward, new tab, saved training stays hidden')


def check_training(browser):
    fixture = Fixture(browser)
    page = fixture.page
    page.goto(BASE + '/training')
    expect(page.get_by_role('region', name='Решение позиции')).to_be_visible()
    assert_url(page, '/training')
    page.reload()
    expect(page.get_by_role('region', name='Решение позиции')).to_be_visible()
    page.get_by_role('button', name='К настройкам', exact=True).click()
    assert_url(page, '/')
    expect(page.get_by_role('button', name='Вернуться к позиции', exact=True)).to_be_visible()
    page.go_back()
    assert_url(page, '/training')
    expect(page.get_by_role('region', name='Решение позиции')).to_be_visible()
    page.go_forward()
    assert_url(page, '/')
    expect(page.get_by_role('navigation', name='Главная навигация')).to_be_visible()
    fixture.close()
    print('PASS training URL: saved position, reload, setup, Back/Forward without a new attempt')


def check_details(browser):
    fixture = Fixture(browser)
    page = fixture.page
    for path, selector, text in [('/games/7', '.import-detail-heading', 'FirstPlayer'),
                                  ('/games/8', '.import-detail-heading', 'SecondPlayer'),
                                  ('/analysis/1/review', '.review-position-caption', 'Начальная позиция'),
                                  ('/analysis/2', '.analysis-progress-card', 'Player2'),
                                  ('/coach/skills?skill=fork', '.coach-skill-detail', 'Вилки'),
                                  ('/coach/progress?skill=pin', '.coach-skill-detail', 'Связки'),
                                  ('/coach/games', '.coach-games', 'Исходные'),
                                  ('/coach/settings', '.coach-setup', 'С чего начнём?'),
                                  ('/coach/practice/99', '.coach-session', 'Вилки')]:
        page.goto(BASE + path)
        expect(page.locator(selector)).to_contain_text(text)
        assert_url(page, path)
        page.reload()
        expect(page.locator(selector)).to_contain_text(text)
        assert_url(page, path)
    assert ('GET', '/coach/practice/99') in fixture.requests
    page.goto(BASE + '/analysis')
    page.get_by_role('link', name='Открыть партию Player1 — Opponent1', exact=True).click()
    assert_url(page, '/analysis/1/review')
    page.go_back()
    assert_url(page, '/analysis')
    expect(page.get_by_role('heading', name='Анализ партий', exact=True)).to_be_visible()
    page.go_forward()
    expect(page.get_by_role('region', name='Интерактивный разбор партии')).to_be_visible()
    page.goto(BASE + '/coach')
    page.get_by_role('tab', name='Навыки', exact=True).click()
    assert_url(page, '/coach/skills')
    page.get_by_role('tab', name='Прогресс', exact=True).click()
    assert_url(page, '/coach/progress')
    page.go_back()
    expect(page.get_by_role('tab', name='Навыки', exact=True)).to_have_attribute('aria-selected', 'true')
    page.go_forward()
    expect(page.get_by_role('tab', name='Прогресс', exact=True)).to_have_attribute('aria-selected', 'true')
    fixture.close()
    print('PASS deep links: library selection, analysis, coach tabs/settings/attempt, reload and history')


def check_login_return(browser):
    fixture = Fixture(browser, authenticated=False)
    page = fixture.page
    target = '/coach/progress?skill=fork'
    page.goto(BASE + target)
    expect(page.get_by_label('Имя пользователя', exact=True)).to_be_visible()
    parsed = urlparse(page.url)
    assert parsed.path == '/login' and parse_qs(parsed.query)['next'] == [target], page.url
    page.get_by_label('Имя пользователя', exact=True).fill('tester')
    page.get_by_label('Пароль', exact=True).fill('fixture-password')
    page.get_by_role('button', name='Начать практику', exact=False).click()
    assert_url(page, target)
    expect(page.locator('.coach-skill-detail')).to_contain_text('Вилки')
    fixture.authenticated = False
    page.evaluate("window.dispatchEvent(new Event('session-expired'))")
    expect(page.get_by_label('Имя пользователя', exact=True)).to_be_visible()
    page.get_by_label('Имя пользователя', exact=True).fill('tester')
    page.get_by_label('Пароль', exact=True).fill('fixture-password')
    page.get_by_role('button', name='Начать практику', exact=False).click()
    assert_url(page, target)
    expect(page.locator('.coach-skill-detail')).to_contain_text('Вилки')
    for next_value in ('https://example.com/', '//example.com/', '/login?next=/progress', '/\\['):
        page.goto(BASE + '/login?next=' + quote(next_value, safe=''))
        assert_url(page, '/')
        expect(page.get_by_role('navigation', name='Главная навигация')).to_be_visible()
    fixture.close()
    print('PASS auth: login/expiry return URL and query, safe fallback for external, recursive or malformed next')


def check_missing_and_stale(browser):
    fixture = Fixture(browser)
    page = fixture.page
    page.goto(BASE + '/does-not-exist')
    expect(page.get_by_role('heading', name=re.compile('не найден', re.I))).to_be_visible()
    assert_url(page, '/does-not-exist')
    page.goto(BASE + '/games/999')
    expect(page.get_by_role('alert')).to_contain_text('не найдена')
    expect(page.locator('.import-detail-heading')).to_have_count(0)
    page.goto(BASE + '/coach/practice/999')
    expect(page.get_by_role('alert')).to_contain_text('не найдена')
    expect(page.locator('.coach-session .chessboard')).to_have_count(0)
    page.goto(BASE + '/analysis/999')
    expect(page.locator('.analysis-focused')).to_contain_text('Не удалось найти')
    expect(page.locator('.analysis-position-board')).to_have_count(0)
    fixture.hold_paths.add('/game-analysis/11')
    page.goto(BASE + '/analysis')
    page.get_by_role('link', name='Открыть партию Player1 — Opponent1', exact=True).click()
    expect(page.locator('.analysis-focused')).to_be_visible()
    page.go_back()
    assert_url(page, '/analysis')
    fixture.release()
    expect(page.get_by_role('heading', name='Анализ партий', exact=True)).to_be_visible()
    assert_url(page, '/analysis')
    page.goto(BASE + '/')
    fixture.hold_paths.add('/training/41/resume')
    page.get_by_role('button', name='Вернуться к позиции', exact=True).click()
    expect(page.get_by_role('button', name='Вернуться к позиции', exact=True)).to_be_disabled()
    page.get_by_role('link', name='Прогресс', exact=True).click()
    assert_url(page, '/progress')
    page.go_back()
    assert_url(page, '/')
    assert fixture.held, 'Resume response must still be in flight when returning to setup'
    fixture.release()
    expect(page.get_by_role('button', name='Вернуться к позиции', exact=True)).to_be_enabled()
    assert_url(page, '/')
    expect(page.get_by_role('region', name='Решение позиции')).not_to_be_visible()
    fixture.close()
    print('PASS missing routes/records and delayed responses after Back or leaving and returning to setup')


if __name__ == '__main__':
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE', '/usr/bin/chromium'),
            args=['--no-sandbox', '--disable-features=LocalNetworkAccessChecks'])
        for check in (check_main_routes, check_training, check_details, check_login_return, check_missing_and_stale):
            check(browser)
        browser.close()
