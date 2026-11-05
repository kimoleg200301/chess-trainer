"""Personal coach UI integration and viewport checks, using a bounded API fixture.

Run Vite on 5174, then: python3 frontend/tests/coach-browser.py
Requires Python Playwright and Chromium. No real account data is changed.
"""
from copy import deepcopy
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from browser_support import chromium_options
from playwright.sync_api import sync_playwright, expect

BASE = os.environ.get('TEST_FRONTEND_URL', 'http://127.0.0.1:5174')
FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
E4 = 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1'
# Validate a realistic 20-ply principal variation with the same installed chess
# library as the app, retaining legal SAN and FEN at every replay step.
LONG_SOLUTION = json.loads(subprocess.check_output([
    'node', '--input-type=module', '-e', '''
      import { Chess } from 'chess.js';
      const board = new Chess();
      const line = 'e2e4 e7e5 g1f3 b8c6 f1b5 a7a6 b5a4 g8f6 e1g1 f8e7 f1e1 b7b5 a4b3 d7d6 c2c3 e8g8 h2h3 c6b8 d2d4 b8d7'.split(' ');
      console.log(JSON.stringify(line.map(uci => {
        const move = board.move({from: uci.slice(0, 2), to: uci.slice(2, 4)});
        return {uci, san: move.san, fen: board.fen()};
      })));
    '''], cwd=Path(__file__).resolve().parents[1], text=True))
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
                # The accepted move differs from the source exercise's reference.
                # Replay must keep the accepted line instead of replacing it with d4.
                attempt['solution'] = dict(initial_fen=FEN, moves=deepcopy(LONG_SOLUTION), kind='accepted',
                                           reference_move=dict(uci='d2d4', san='d4'), explanation='Проверь каждый шах и каждое взятие. ' * 30)
                data['active_attempt_id'] = None
                result = dict(accepted=True, attempt=attempt, feedback=attempt['feedback'])
            elif path.endswith('/reveal'):
                attempt.update(status='revealed', solution=dict(initial_fen=FEN, moves=deepcopy(LONG_SOLUTION), kind='reference', explanation='Сначала проверь безопасность своего короля. ' * 30))
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
            scrollHeight: document.documentElement.scrollHeight, scrollX,
            board: {top:b.top,bottom:b.bottom,left:b.left,right:b.right,width:b.width},
            actions: {top:c.top,bottom:c.bottom,left:c.left,right:c.right},
            buttons: [...document.querySelectorAll('.coach-session-actions button')].map(button => {
              const r = button.getBoundingClientRect(); return {left:r.left,right:r.right};
            }), text:{top:t.top,bottom:t.bottom,height:t.height} };
        }''')
        assert metrics['scrollWidth'] <= width, (stage, metrics)
        assert metrics['scrollHeight'] <= height + 2, (stage, metrics)
        assert metrics['scrollX'] == 0, (stage, metrics)
        assert metrics['board']['top'] >= 0 and metrics['board']['bottom'] <= metrics['actions']['top'], (stage, metrics)
        assert metrics['board']['left'] >= 0 and metrics['board']['right'] <= width + 1, (stage, metrics)
        assert metrics['actions']['left'] >= 0 and metrics['actions']['right'] <= width + 1, (stage, metrics)
        assert metrics['actions']['bottom'] <= height + 2, (stage, metrics)
        assert all(button['left'] >= 0 and button['right'] <= width + 1 for button in metrics['buttons']), (stage, metrics)
        assert metrics['text']['height'] >= 18, (stage, metrics)
        return metrics

    def assert_board_unchanged(stage, initial):
        current = assert_fits(stage)
        # Short phones intentionally use a smaller review board. On tall phones
        # the successful move must not alter its horizontal size or placement.
        if width in (390, 412) and height > 700:
            for attribute in ('left', 'width'):
                assert abs(current['board'][attribute] - initial['board'][attribute]) <= 1, (stage, initial, current)

    def assert_position(expected_fen):
        expected = {}
        for row, rank in enumerate(expected_fen.split()[0].split('/')):
            file = 0
            for piece in rank:
                if piece.isdigit():
                    file += int(piece)
                else:
                    expected[f'{chr(97 + file)}{8 - row}'] = ('w' if piece.isupper() else 'b') + piece.upper()
                    file += 1
        actual = page.locator('.coach-session .chessboard').evaluate('''board => Object.fromEntries(
          [...board.querySelectorAll('[data-square]')].filter(square => square.querySelector('img')).map(square =>
            [square.dataset.square, square.querySelector('img').getAttribute('src').split('/').pop().replace('.svg', '')]))''')
        assert actual == expected, (expected_fen, actual)

    def select_last_solution_move(stage, initial):
        moves = page.locator('.coach-solution-moves')
        expect(moves.locator('button')).to_have_count(len(LONG_SOLUTION))
        dimensions = moves.evaluate('(e) => ({width:e.clientWidth,scrollWidth:e.scrollWidth,height:e.clientHeight,scrollHeight:e.scrollHeight})')
        assert dimensions['scrollWidth'] > dimensions['width'] or dimensions['scrollHeight'] > dimensions['height'], (stage, dimensions)
        last = moves.locator('button').last
        last.scroll_into_view_if_needed()
        bounds = moves.evaluate('''e => {
          const container = e.getBoundingClientRect(), last = e.lastElementChild.getBoundingClientRect();
          return {left:container.left,right:container.right,top:container.top,bottom:container.bottom,
            lastLeft:last.left,lastRight:last.right,lastTop:last.top,lastBottom:last.bottom,
            scrollLeft:e.scrollLeft,scrollTop:e.scrollTop};
        }''')
        assert bounds['scrollLeft'] > 0 or bounds['scrollTop'] > 0, (stage, bounds)
        assert bounds['lastLeft'] >= bounds['left'] - 1 and bounds['lastRight'] <= bounds['right'] + 1, (stage, bounds)
        assert bounds['lastTop'] >= bounds['top'] - 1 and bounds['lastBottom'] <= bounds['bottom'] + 1, (stage, bounds)
        last.click()
        expect(page.locator('.coach-solution-label')).to_contain_text(f'{len(LONG_SOLUTION)} / {len(LONG_SOLUTION)}')
        assert_board_unchanged(stage, initial)

    page.route('**/api/**', respond)
    page.route('https://fonts.googleapis.com/**', lambda route: route.abort())
    page.goto(BASE)
    page.get_by_role('link', name='Тренер', exact=True).click()
    expect(page.get_by_role('textbox', name='Ник на Chess.com')).to_have_value('kimskiy1114')
    expect(page.get_by_label('Контроль времени', exact=True)).to_have_value('300+5')
    viewport_width = page.evaluate('({viewport: innerWidth, content: document.documentElement.scrollWidth})')
    assert viewport_width['content'] <= viewport_width['viewport'], viewport_width
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
    initial_board = assert_fits('active')
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
    expect(page.locator('.coach-solution-label')).to_contain_text('Твой вариант')
    expect(page.locator('.coach-solution-label')).to_contain_text('Эталон: d4')
    expect(page.locator('.coach-solution-label')).to_contain_text(f'1 / {len(LONG_SOLUTION)}')
    assert_position(E4)
    assert_board_unchanged('completed-long-pv', initial_board)
    page.get_by_role('button', name='Предыдущий ход', exact=True).click()
    expect(page.locator('.coach-solution-label')).to_contain_text(f'0 / {len(LONG_SOLUTION)}')
    assert_position(FEN)
    page.get_by_role('button', name='Следующий ход', exact=True).click()
    assert_position(E4)
    select_last_solution_move('completed-scroll-to-last-move', initial_board)
    assert_position(LONG_SOLUTION[-1]['fen'])
    page.get_by_role('button', name='Предыдущий ход', exact=True).click()
    expect(page.locator('.coach-solution-label')).to_contain_text(f'{len(LONG_SOLUTION) - 1} / {len(LONG_SOLUTION)}')
    page.get_by_role('button', name='Воспроизвести решение', exact=True).click()
    expect(page.locator('.coach-solution-label')).to_contain_text(f'{len(LONG_SOLUTION)} / {len(LONG_SOLUTION)}')
    assert_board_unchanged('completed-long-pv-replay', initial_board)
    screenshot_dir = os.environ.get('COACH_SCREENSHOT_DIR')
    if screenshot_dir:
        Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(screenshot_dir) / f'coach-session-{width}x{height}.png'))
    # A stale dashboard can offer resume after another request completed the
    # attempt. Reopening must show the accepted move, not the initial position.
    data['active_attempt_id'] = attempt['id']
    page.get_by_role('button', name='К плану', exact=True).click()
    page.get_by_role('tab', name='План', exact=True).click()
    page.get_by_role('button', name='Продолжить занятие', exact=True).click()
    expect(page.get_by_role('heading', name='Сохрани эту идею', exact=True)).to_be_visible()
    expect(page.locator('.coach-solution-label')).to_contain_text(f'1 / {len(LONG_SOLUTION)}')
    expect(page.locator('.coach-session-underboard')).to_contain_text('Разобрано: 1 / 10')
    assert_position(E4)
    assert_board_unchanged('resumed-completed-alternative', initial_board)
    data['active_attempt_id'] = None
    if screenshot_dir:
        page.screenshot(path=str(Path(screenshot_dir) / f'coach-accepted-{width}x{height}.png'))
    page.get_by_role('button', name='Дальше', exact=True).click()
    expect(page.get_by_role('heading', name='Выбери сильный ход', exact=True)).to_be_visible()
    current_id = attempt['id']
    page.get_by_role('button', name='К плану', exact=True).click()
    page.get_by_role('tab', name='План', exact=True).click()
    expect(page.get_by_role('button', name='Продолжить занятие', exact=True)).to_be_visible()
    old_posts = len([p for p in posts if p[0] == '/coach/pause'])
    page.get_by_role('link', name='Главная', exact=True).click()
    page.get_by_role('link', name='Тренер', exact=True).click()
    assert len([p for p in posts if p[0] == '/coach/pause']) == old_posts
    page.get_by_role('button', name='Продолжить занятие', exact=True).click()
    assert attempt['id'] == current_id
    page.get_by_role('button', name='К плану', exact=True).click()
    page.reload()
    page.get_by_role('link', name='Тренер', exact=True).click()
    page.get_by_role('button', name='Продолжить занятие', exact=True).click()
    expect(page.get_by_role('region', name='Занятие с личным тренером')).to_be_visible()
    assert attempt['id'] == current_id
    page.get_by_role('button', name='Разобрать', exact=True).click()
    expect(page.get_by_role('heading', name='Пойми продолжение', exact=True)).to_be_visible()
    expect(page.locator('.coach-solution-label')).to_contain_text('Продолжение')
    expect(page.locator('.coach-solution-label')).not_to_contain_text('Эталон:')
    assert_board_unchanged('revealed-long-pv', initial_board)
    select_last_solution_move('revealed-scroll-to-last-move', initial_board)
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
    print(f'PASS {width}x{height}: setup, tracking, 11 skills, timeout evidence, moves/retry, accepted alternative/reference, legal 20-ply replay, completed/active resume, internal scroll, viewport, pause, pagination')
    context.close()


def check_autosave_actions(browser):
    """A slow heartbeat must finish before any practice mutation is sent.

    Pending routes represent real overlapping requests, rather than a sleeping
    route callback that would prevent Playwright from observing the overlap.
    The fixture returns the production 409 when an action races a /time write.
    """
    for operation, fail_time in [('move', False), ('hint', False), ('reveal', False), ('move', True)]:
        context = browser.new_context(viewport=dict(width=390, height=844), has_touch=True, is_mobile=True)
        page = context.new_page()
        page.add_init_script('''(() => {
          const originalNow = Date.now;
          window.coachTestTimeOffset = 0;
          Date.now = () => originalNow() + window.coachTestTimeOffset;
        })();''')
        errors, pending_times, pending_actions, action_requests, conflicts, saved_times = [], [], [], [], [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        data = fixtures()
        data['profile'].update(status='tracking', baseline_ready=True, baseline_games=40,
                               baseline_completed=40, progress=100, syncing=False)
        data['active_attempt_id'] = 1
        attempt = dict(id=1, skill_id='calculation', skill_title='Расчёт вариантов', kind='decision',
                       fen=FEN, starting_fen=FEN, learner_white=True, status='active', mistakes=0, hints_used=0,
                       elapsed_ms=0, last_move=None, ply_index=0, total_plies=1,
                       prompt='Найди сильное продолжение.', feedback='', is_new=True,
                       source=dict(game_url='https://www.chess.com/game/live/123456', move_number=1, own_game=True),
                       solution=None, target_seconds=None)

        def respond(route):
            request = route.request
            path = urlparse(request.url).path.removeprefix('/api')
            body = request.post_data_json if request.method == 'POST' else None
            if path == '/auth/me': result = dict(username='tester')
            elif path == '/meta':
                result = dict(total_puzzles=100, themes=[], min_rating=400, max_rating=2000,
                              collections=[dict(id='lichess', name='Lichess', count=100)])
            elif path == '/stats':
                result = dict(solved=0, total_attempts=0, accuracy=0, streak=0, due_count=0,
                              average_solution_ms=None, timed_solutions=0, puzzle_rating=1200,
                              rated_attempts=0, by_theme=[], recent=[])
            elif path == '/training/current': result = dict(attempt=None, solution=[], game_url='')
            elif path == '/coach': result = deepcopy(data)
            elif path == '/coach/practice/1': result = deepcopy(attempt)
            elif path == '/coach/practice/1/time':
                pending_times.append((route, body))
                return
            elif path.startswith('/coach/practice/1/'):
                action = path.rsplit('/', 1)[1]
                assert action in {'move', 'hint', 'reveal'}, path
                action_requests.append((action, body))
                if pending_times:
                    conflicts.append(action)
                    route.fulfill(status=409, json={'detail': 'Другая операция с вашей подборкой ещё выполняется. Повторите через несколько секунд.'})
                else:
                    pending_actions.append((route, action, body))
                return
            else:
                raise AssertionError(path)
            route.fulfill(json=result)

        def wait_for(predicate, description):
            for _ in range(150):
                if predicate():
                    return
                page.wait_for_timeout(20)
            raise AssertionError(description)

        page.route('**/api/**', respond)
        page.route('https://fonts.googleapis.com/**', lambda route: route.abort())
        try:
            page.goto(BASE)
            page.get_by_role('link', name='Тренер', exact=True).click()
            page.get_by_role('button', name='Продолжить занятие', exact=True).click()
            expect(page.get_by_role('region', name='Занятие с личным тренером')).to_be_visible()
            page.wait_for_timeout(120)
            page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            wait_for(lambda: bool(pending_times), 'The timer did not start the delayed heartbeat')
            assert pending_times[0][1]['elapsed_ms'] > 0

            if operation == 'move':
                page.locator('.coach-session [data-square="e2"]').click()
                page.locator('.coach-session [data-square="e4"]').click()
            else:
                page.get_by_role('button', name='Подсказка' if operation == 'hint' else 'Разобрать', exact=True).click()
            page.wait_for_timeout(160)
            assert not action_requests, (operation, 'Action overlapped an unfinished /time request', action_requests, conflicts)
            expect(page.locator('.coach-session-underboard')).to_contain_text('Разобрано: 0 / 10')

            # Resolve every heartbeat already sent, including pause()'s forced
            # checkpoint. Even a failed save must not discard the actual move.
            while pending_times:
                route, body = pending_times.pop(0)
                saved_times.append(body['elapsed_ms'])
                if fail_time:
                    route.fulfill(status=503, json={'detail': 'Временная ошибка сохранения времени.'})
                else:
                    attempt['elapsed_ms'] = max(attempt['elapsed_ms'], body['elapsed_ms'])
                    route.fulfill(json=deepcopy(attempt))
            wait_for(lambda: bool(pending_actions), 'Action was not sent after the heartbeat settled')
            assert len(action_requests) == 1 and not conflicts

            # Keep the action in flight while the timer's 15-second retry becomes
            # due. A failed preceding save leaves an unacknowledged checkpoint,
            # so this exercises the guard against fresh autosaves during actions.
            page.evaluate("window.coachTestTimeOffset += 16000; document.dispatchEvent(new Event('visibilitychange'))")
            page.wait_for_timeout(350)
            assert not pending_times, (operation, 'Autosave started while the action was in flight')
            route, action, body = pending_actions.pop()
            assert action == operation and body['elapsed_ms'] >= max(saved_times)
            assert body['elapsed_ms'] < 5000, 'Waiting for the server was counted as solving time'
            attempt['elapsed_ms'] = max(attempt['elapsed_ms'], body['elapsed_ms'])
            if action == 'hint':
                attempt['hints_used'] += 1
                result = dict(attempt=deepcopy(attempt), message='Проверь центральные пешки.', from_square='e2')
            else:
                if action == 'move':
                    assert body['move'] == 'e2e4'
                    attempt.update(status='completed', fen=E4, last_move='e2e4', ply_index=1,
                                   feedback='Ход принят.')
                else:
                    attempt.update(status='revealed', feedback='Разбери идею.')
                attempt['solution'] = dict(initial_fen=FEN,
                    moves=[dict(uci='e2e4', san='e4', fen=E4)], explanation='Проверь безопасность фигур.')
                data['active_attempt_id'] = None
                result = dict(accepted=True, attempt=deepcopy(attempt), feedback=attempt['feedback']) if action == 'move' else deepcopy(attempt)
            route.fulfill(json=result)
            if action == 'hint':
                expect(page.get_by_text('Проверь центральные пешки.', exact=True)).to_be_visible()
            else:
                expect(page.get_by_role('heading', name='Сохрани эту идею' if action == 'move' else 'Пойми продолжение', exact=True)).to_be_visible()
            expect(page.locator('.coach-session-underboard')).to_contain_text(f"Разобрано: {0 if action == 'hint' else 1} / 10")
            expect(page.locator('.coach-session-detail')).to_contain_text('Без ошибок')
            expect(page.get_by_role('alert')).to_have_count(0)
            expect(page.locator('.coach-time-warning')).to_have_count(0)
            page.wait_for_timeout(100)
            assert len(action_requests) == 1 and attempt['mistakes'] == 0 and not conflicts
            assert not errors, errors
            print(f'PASS autosave/{operation}{"/save-failure" if fail_time else ""}: ordered writes, one action, preserved timing, no false mistakes')
        finally:
            for pending in [*pending_times, *pending_actions]:
                try:
                    pending[0].abort()
                except Exception:
                    pass
            context.close()


if __name__ == '__main__':
    with sync_playwright() as p:
        browser = p.chromium.launch(**chromium_options(),
                                    args=['--no-sandbox', '--disable-features=LocalNetworkAccessChecks'])
        check_autosave_actions(browser)
        if os.environ.get('COACH_AUTOSAVE_ONLY') != '1':
            for viewport in [(390, 844), (360, 640), (412, 915), (1280, 900), (651, 390), (700, 390), (740, 390)]:
                check(browser, *viewport)
        browser.close()
