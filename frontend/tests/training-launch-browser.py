"""Queued personal puzzles survive a pending resume or another puzzle launch."""
from copy import deepcopy
import importlib.util
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

from browser_support import chromium_options

spec = importlib.util.spec_from_file_location('routing', Path(__file__).with_name('routing-browser.py'))
routing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(routing)


class Fixture(routing.Fixture):
    def __init__(self, browser):
        super().__init__(browser)
        self.launches = []
        self.hold_start = False
        self.held_starts = []
        candidates = [dict(id=index, ply=0, move_number=index, side='white', played_uci='d2d4',
                           played_san='d4', best_uci='e2e4', best_san='e4', loss_cp=120,
                           score_before=150, score_after=30, explanation='Найди сильный ход.',
                           puzzle_available=True, puzzle_id=f'personal-{index}',
                           puzzle_reason_code=None, puzzle_reason=None, analysis_depth=16,
                           analysis_pending=False) for index in (1, 2)]
        game = dict(id=1, white='FirstPlayer', black='Opponent', result='1-0', date='2026.11.04',
                    event='', game_url='', status='completed', cursor=4, total_plies=4,
                    error=None, candidates=candidates, source_game_at=None, analysis_version=2)
        self.jobs[0].update(games=[game], total_games=1, completed_games=1, candidates_count=2,
                            available_puzzles_count=2, puzzles_count=2)

    def respond(self, route):
        path = urlparse(route.request.url).path.removeprefix('/api')
        if path == '/meta':
            return route.fulfill(json=dict(total_puzzles=100, themes=[], min_rating=400, max_rating=2000,
                collections=[dict(id='lichess', name='Lichess', count=100), dict(id='personal', name='Мои задачи', count=2)]))
        if path == '/training/start':
            body = route.request.post_data_json
            self.launches.append(body)
            self.attempt = dict(self.attempt, id=41 + len(self.launches), puzzle_id=body['puzzle_id'],
                                collection=body['collection'], elapsed_ms=0)
            self.saved = dict(attempt=self.attempt, solution=[], game_url='')
            if self.hold_start:
                self.held_starts.append((route, deepcopy(self.attempt)))
                return
            return route.fulfill(json=self.attempt)
        if path.startswith('/training/') and path.endswith('/time'):
            return route.fulfill(json=self.attempt)
        return super().respond(route)

    def open(self):
        self.page.goto(routing.BASE + '/')
        expect(self.page.get_by_role('button', name='Вернуться к позиции', exact=True)).to_be_enabled()

    def choose(self, index):
        self.page.get_by_role('navigation', name='Главная навигация').get_by_role('link', name='Мои партии', exact=True).click()
        self.page.locator('.import-history-list').get_by_role('link', name='Chess.com · FirstPlayer', exact=False).click()
        self.page.locator('.improvement-card').nth(index - 1).get_by_role('button', name='Решить задачу', exact=True).click()
        expect(self.page).to_have_url(routing.BASE + '/')

    def assert_launched(self, index):
        expect(self.page).to_have_url(routing.BASE + '/training')
        expect(self.page.get_by_role('region', name='Решение позиции')).to_be_visible()
        expect(self.page.locator('.puzzle-caption')).to_contain_text(f'personal-{index}')
        self.page.get_by_role('button', name='К настройкам', exact=True).click()
        expect(self.page.get_by_label('База задач', exact=True)).to_have_value('personal')

    def close(self):
        assert not self.errors, self.errors
        assert not self.held and not self.held_starts
        self.context.close()


def check_pending_resume(browser):
    fixture = Fixture(browser)
    fixture.open()
    fixture.hold_paths.add('/training/41/resume')
    fixture.page.get_by_role('button', name='Вернуться к позиции', exact=True).click()
    expect(fixture.page.get_by_role('button', name='Вернуться к позиции', exact=True)).to_be_disabled()
    fixture.choose(1)
    assert fixture.held and not fixture.launches
    fixture.release()
    fixture.assert_launched(1)
    assert [item['puzzle_id'] for item in fixture.launches] == ['personal-1']
    fixture.close()
    print('PASS training launch: a personal puzzle waits for resume and retains its collection')


def check_newer_launch(browser):
    fixture = Fixture(browser)
    fixture.open()
    fixture.hold_start = True
    fixture.choose(1)
    expect(fixture.page.get_by_role('button', name='Вернуться к позиции', exact=True)).to_be_disabled()
    fixture.choose(2)
    assert len(fixture.held_starts) == 1
    assert [item['puzzle_id'] for item in fixture.launches] == ['personal-1']
    fixture.hold_start = False
    route, result = fixture.held_starts.pop()
    route.fulfill(json=result)
    fixture.assert_launched(2)
    assert [item['puzzle_id'] for item in fixture.launches] == ['personal-1', 'personal-2']
    fixture.close()
    print('PASS training launch: completion of an earlier launch preserves the newer puzzle choice')


if __name__ == '__main__':
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**chromium_options(), args=['--no-sandbox', '--disable-features=LocalNetworkAccessChecks'])
        check_pending_resume(browser)
        check_newer_launch(browser)
        browser.close()
