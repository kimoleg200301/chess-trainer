"""Personal coach UI integration and viewport checks, using a bounded API fixture.

Run Vite on 5174, then: python3 frontend/tests/coach-browser.py
Requires Python Playwright and Chromium. No real account data is changed.
"""
from copy import deepcopy
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, expect

BASE = os.environ.get('TEST_FRONTEND_URL', 'http://127.0.0.1:5174')
FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
E4 = 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1'
E5 = 'rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2'
NF3 = 'rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2'
IDS = ['hanging_piece', 'fork', 'pin', 'mate', 'king_safety', 'defense', 'conversion', 'opening', 'endgame', 'time_management', 'calculation']
TITLES = ['Безопасность фигур', 'Вилки', 'Связки', 'Матовые угрозы', 'Безопасность короля', 'Защита', 'Реализация перевеса', 'Дебют', 'Эндшпиль', 'Управление временем', 'Расчёт вариантов']


def fixtures():
    metric = dict(games=40, opportunities=1200, errors=30, rate=2.5)
    recent = dict(games=4, opportunities=100, errors=3, rate=3.0)
    skills = [dict(id=id_, title=title, description='Проверяй угрозы и безопасность фигур перед каждым ходом.',
                   recommendation='Сначала проверь шахи, взятия и угрозы соперника. Затем выбери свой ответ.',
                   practice_tip='Назови угрозу соперника, прежде чем искать продолжение.', themes=['fork'],
                   priority=10 if i < 3 else 1, status='focus' if i < 3 else 'insufficient',
                   baseline=deepcopy(metric), recent=deepcopy(recent), delta=.5, comparable=False,
                   metric_label='Ошибок на 100 оценённых ходов', evidence=[],
                   training=dict(completed=3, clean=2, new_clean=1)) for i, (id_, title) in enumerate(zip(IDS, TITLES))]
    skills[0]['evidence'] = [dict(game_id=1, ply=4, move_number=3, played_san='a3', best_san='Nf3',
                                 explanation='В этой позиции важно завершить развитие и проверить угрозы.',
                                 game_url='https://www.chess.com/game/live/123456', phase='opening')]
    skills[9]['evidence'] = [dict(game_id=2, ply=None, move_number=None, played_san=None, best_san=None,
                                 explanation='Партия завершилась поражением по времени.',
                                 game_url='https://www.chess.com/game/live/123457', phase=None)]
    profile = dict(id=1, username='kimskiy1114', time_control='300+5', started_at='2026-11-03T08:00:00Z',
                   active=True, status='loading', baseline_target=40, baseline_games=7, baseline_completed=0,
                   baseline_ready=False, new_games=0, new_completed=0, processed_moves=0, total_moves=400,
                   progress=0, last_sync_at=None, next_sync_at=None, error=None, syncing=True, review_due=3)
    return dict(profile=profile, skills=skills, priorities=IDS[:3], summary={}, methodology='Проверка Stockfish.',
                games=[dict(id=i, cohort='baseline', white='kimskiy1114', black=f'Opponent{i}', source_game_at='2026-11-02T08:00:00Z',
                            result='1-0', player_result='win', status='completed', cursor=42, total_plies=42,
                            game_url=f'https://www.chess.com/game/live/{123456+i}', error=None) for i in range(1, 13)],
                active_attempt_id=None)


def check(browser, width, height):
    context = browser.new_context(viewport=dict(width=width, height=height), has_touch=width < 700, is_mobile=width < 700)
    page = context.new_page()
    errors, posts = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    data = fixtures()
    enabled = False
    attempt = None
    next_id = 0
    reject_next_move = True
    fail_next_practice = False

    def respond(route):
        nonlocal enabled, attempt, next_id, reject_next_move, fail_next_practice
        request = route.request
        path = urlparse(request.url).path.removeprefix('/api')
        body = request.post_data_json if request.method == 'POST' else None
        if request.method == 'POST': posts.append((path, body))
        if path == '/auth/me': result = {'username': 'tester'}
        elif path == '/meta':
            result = dict(total_puzzles=100, themes=[], min_rating=400, max_rating=2000,
                          collections=[dict(id='lichess', name='Lichess', count=100)])
        elif path == '/stats':
            result = dict(solved=0, total_attempts=0, accuracy=0, streak=0, due_count=0,
                          average_solution_ms=None, timed_solutions=0, puzzle_rating=1200, rated_attempts=0, by_theme=[], recent=[])
        elif path == '/training/current': result = dict(attempt=None, solution=[], game_url='')
        elif path == '/coach': result = deepcopy(data) if enabled else dict(profile=None, skills=[], priorities=[], summary={}, methodology='', games=[], active_attempt_id=None)
        elif path == '/coach/start': enabled = True; result = data
        elif path == '/coach/pause': data['profile'].update(active=False, status='paused'); result = data
        elif path == '/coach/resume': data['profile'].update(active=True, status='tracking'); result = data
        elif path == '/coach/sync': data['profile'].update(syncing=True); result = data
        elif path == '/coach/practice':
            if fail_next_practice:
                fail_next_practice = False
                return route.fulfill(status=404, json={'detail':'Все новые позиции этой темы разобраны. Выберите другую тему.'})
            next_id += 1
            skill_id = body.get('skill_id') or IDS[0]
            attempt = dict(id=next_id, skill_id=skill_id, skill_title=TITLES[IDS.index(skill_id)], kind='decision',
                           fen=FEN, starting_fen=FEN, learner_white=True, status='active', mistakes=0, hints_used=0,
                           elapsed_ms=0, last_move=None, ply_index=0, total_plies=1,
                           prompt='Проверь угрозы соперника и выбери сильный ход.', feedback='', is_new=True,
                           source=dict(game_url='https://www.chess.com/game/live/123456', move_number=3, own_game=True),
                           solution=None, target_seconds=30 if skill_id == 'time_management' else None)
            data['active_attempt_id'] = next_id
            result = attempt
        elif path.startswith('/coach/practice/'):
            if path.endswith('/time'):
                if attempt['status'] == 'active': attempt['elapsed_ms'] = max(attempt['elapsed_ms'], body['elapsed_ms'])
                result = attempt
            elif path.endswith('/hint'):
                attempt['hints_used'] += 1
                result = dict(attempt=attempt, message='Проверь центральные пешки.', from_square='e2')
            elif path.endswith('/move'):
                if reject_next_move:
                    reject_next_move = False
                    return route.fulfill(status=503, json={'detail': 'Движок занят. Попробуйте ход ещё раз.'})
                assert body['move'] == 'e2e4', body
                attempt.update(status='completed', fen=E4, last_move='e2e4', ply_index=1,
                               elapsed_ms=body['elapsed_ms'], feedback='Ход принят. Разбери продолжение.')
                attempt['solution'] = dict(initial_fen=FEN, moves=[dict(uci='e2e4', san='e4', fen=E4), dict(uci='e7e5', san='e5', fen=E5), dict(uci='g1f3', san='Nf3', fen=NF3)], explanation='Проверь каждый шах и каждое взятие. ' * 30)
                data['active_attempt_id'] = None
                result = dict(accepted=True, attempt=attempt, feedback=attempt['feedback'])
            elif path.endswith('/reveal'):
                attempt.update(status='revealed', solution=dict(initial_fen=FEN, moves=[dict(uci='e2e4', san='e4', fen=E4)], explanation='Сначала проверь безопасность своего короля. ' * 30))
                data['active_attempt_id'] = None
                result = attempt
            else: result = attempt
        else: raise AssertionError(path)
        route.fulfill(json=result)

    def assert_fits(stage):
        metrics = page.evaluate('''() => {
          const b = document.querySelector('.coach-board-wrap').getBoundingClientRect();
          const c = document.querySelector('.coach-session-actions').getBoundingClientRect();
          const t = document.querySelector('.coach-lesson-text').getBoundingClientRect();
          return { width: innerWidth, height: innerHeight, scrollWidth: document.documentElement.scrollWidth,
            scrollHeight: document.documentElement.scrollHeight, board: {top:b.top,bottom:b.bottom,left:b.left,right:b.right},
            actions: {top:c.top,bottom:c.bottom}, text:{top:t.top,bottom:t.bottom,height:t.height} };
        }''')
        assert metrics['scrollWidth'] <= width, (stage, metrics)
        assert metrics['scrollHeight'] <= height + 2, (stage, metrics)
        assert metrics['board']['top'] >= 0 and metrics['board']['bottom'] <= metrics['actions']['top'], (stage, metrics)
        assert metrics['actions']['bottom'] <= height + 2, (stage, metrics)
        assert metrics['text']['height'] >= 18, (stage, metrics)
        return metrics

    page.route('**/api/**', respond)
    page.route('https://fonts.googleapis.com/**', lambda route: route.abort())
    page.goto(BASE)
    page.get_by_role('button', name='Тренер', exact=True).click()
    expect(page.get_by_role('textbox', name='Ник на Chess.com')).to_have_value('kimskiy1114')
    expect(page.get_by_label('Контроль времени', exact=True)).to_have_value('300+5')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.get_by_role('button', name='Создать мой план').click()
    expect(page.get_by_role('heading', name='Игра становится практикой')).to_be_visible()
    assert ('/coach/start', {'username': 'kimskiy1114', 'time_control': '300+5'}) in posts
    expect(page.get_by_text('Собираем партии', exact=True)).to_be_visible()
    data['profile'].update(status='tracking', baseline_games=40, baseline_completed=40, baseline_ready=True,
                           new_games=4, new_completed=4, progress=100, syncing=False, last_sync_at='2026-11-03T09:00:00Z')
    page.evaluate("window.dispatchEvent(new Event('online'))")
    expect(page.get_by_text('Следим за новыми партиями', exact=True)).to_be_visible()
    screenshot_dir = os.environ.get('COACH_SCREENSHOT_DIR')
    if screenshot_dir:
        Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(screenshot_dir) / f'coach-dashboard-{width}x{height}.png'), full_page=True)

    page.get_by_role('tab', name='Навыки', exact=True).click()
    expect(page.locator('.coach-skill-picker > button')).to_have_count(11)
    page.get_by_role('button', name='Управление временем', exact=False).click()
    expect(page.get_by_text('Поражение по времени', exact=True)).to_be_visible()
    page.get_by_role('button', name='Тренировать навык', exact=True).click()
    expect(page.get_by_role('region', name='Занятие с личным тренером')).to_be_visible()
    assert posts[-1][0] == '/coach/practice' and posts[-1][1] == {'skill_id': 'time_management'}
    assert_fits('active')
    if screenshot_dir:
        page.screenshot(path=str(Path(screenshot_dir) / f'coach-active-{width}x{height}.png'))
    page.get_by_role('button', name='Подсказка', exact=True).click()
    expect(page.locator('.coach-session [data-square="e2"]')).to_have_class(re.compile(r'.*hinted.*'))
    page.locator('.coach-session [data-square="e2"]').click(); page.locator('.coach-session [data-square="e4"]').click()
    expect(page.get_by_role('alert')).to_contain_text('Движок занят')
    assert attempt['status'] == 'active'
    assert_fits('network-error')
    page.locator('.coach-session [data-square="e2"]').click(); page.locator('.coach-session [data-square="e4"]').click()
    expect(page.get_by_role('heading', name='Сохрани эту идею', exact=True)).to_be_visible()
    assert_fits('completed-long-explanation')
    page.get_by_role('button', name='Предыдущий ход', exact=True).click()
    expect(page.locator('.coach-solution-label')).to_contain_text('0 / 3')
    page.get_by_role('button', name='Воспроизвести решение', exact=True).click()
    expect(page.locator('.coach-solution-label')).to_contain_text('3 / 3')
    screenshot_dir = os.environ.get('COACH_SCREENSHOT_DIR')
    if screenshot_dir:
        Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(screenshot_dir) / f'coach-session-{width}x{height}.png'))
    page.get_by_role('button', name='Дальше', exact=True).click()
    expect(page.get_by_role('heading', name='Выбери сильный ход', exact=True)).to_be_visible()
    current_id = attempt['id']
    page.get_by_role('button', name='К плану', exact=True).click()
    page.get_by_role('tab', name='План', exact=True).click()
    expect(page.get_by_role('button', name='Продолжить занятие', exact=True)).to_be_visible()
    old_posts = len([p for p in posts if p[0] == '/coach/pause'])
    page.get_by_role('button', name='Главная', exact=True).click()
    page.get_by_role('button', name='Тренер', exact=True).click()
    assert len([p for p in posts if p[0] == '/coach/pause']) == old_posts
    page.get_by_role('button', name='Продолжить занятие', exact=True).click()
    assert attempt['id'] == current_id
    page.get_by_role('button', name='К плану', exact=True).click()
    page.reload()
    page.get_by_role('button', name='Тренер', exact=True).click()
    page.get_by_role('button', name='Продолжить занятие', exact=True).click()
    expect(page.get_by_role('region', name='Занятие с личным тренером')).to_be_visible()
    assert attempt['id'] == current_id
    page.get_by_role('button', name='Разобрать', exact=True).click()
    expect(page.get_by_role('heading', name='Пойми продолжение', exact=True)).to_be_visible()
    assert_fits('revealed-long-explanation')
    page.get_by_role('button', name='К плану', exact=True).click()
    data['profile']['syncing'] = True
    page.get_by_role('button', name='Пауза', exact=True).click()
    expect(page.get_by_text('Тренер на паузе', exact=True)).to_be_visible()
    expect(page.get_by_role('button', name='Проверить партии', exact=True)).to_be_disabled()
    expect(page.locator('.coach-dashboard-controls > button:first-child .spinner')).to_have_count(0)
    data['profile']['syncing'] = False
    page.get_by_role('button', name='Продолжить', exact=True).click()
    expect(page.get_by_text('Следим за новыми партиями', exact=True)).to_be_visible()
    page.get_by_role('tab', name='Партии', exact=False).click()
    page.get_by_role('button', name='Исходные', exact=False).click()
    expect(page.locator('.coach-game-row')).to_have_count(8)
    page.get_by_role('button', name='Далее', exact=True).click()
    expect(page.locator('.coach-game-row')).to_have_count(4)
    page.get_by_role('tab', name='Навыки', exact=True).click()
    fail_next_practice = True
    page.get_by_role('button', name='Тренировать этот навык', exact=True).click()
    expect(page.get_by_role('alert')).to_contain_text('Все новые позиции')
    with page.expect_response(lambda response: urlparse(response.url).path == '/api/coach'):
        page.evaluate("window.dispatchEvent(new Event('online'))")
    expect(page.get_by_role('alert')).to_contain_text('Все новые позиции')
    data['profile'].update(username='typo', time_control='420+7', baseline_games=0, baseline_completed=0, new_games=0, new_completed=0, error='Игрок не найден', baseline_ready=True, status='error')
    with page.expect_response(lambda response: urlparse(response.url).path == '/api/coach'):
        page.evaluate("window.dispatchEvent(new Event('online'))")
    page.get_by_role('button', name='Исправить настройки', exact=True).click()
    expect(page.get_by_label('Контроль времени', exact=True)).to_have_value('custom')
    expect(page.get_by_label('Минуты', exact=True)).to_have_value('7')
    expect(page.get_by_label('Добавление, сек.', exact=True)).to_have_value('7')
    page.get_by_role('button', name='Вернуться к тренеру', exact=True).click()
    expect(page.get_by_role('button', name='Повторить анализ', exact=True)).to_be_visible()
    assert not errors, errors
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    print(f'PASS {width}x{height}: setup, tracking, 11 skills, timeout evidence, moves/retry, replay, viewport, persisted resume, pause, pagination')
    context.close()


if __name__ == '__main__':
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE', '/usr/bin/chromium'),
                                    args=['--no-sandbox', '--disable-features=LocalNetworkAccessChecks'])
        for viewport in [(390, 844), (360, 640), (1280, 900), (740, 390)]:
            check(browser, *viewport)
        browser.close()
