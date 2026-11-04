"""Run against Vite: npm run dev -- --port 5174; python tests/background-browser.py.
Requires Python Playwright and Chromium (or PLAYWRIGHT_CHROMIUM_EXECUTABLE).
"""
from copy import deepcopy
import os
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, expect

BASE = os.environ.get("TEST_FRONTEND_URL", "http://localhost:5174")
HARNESS = '''<!doctype html><html><body><div id="root"></div><script type="module">
import RefreshRuntime from '/@react-refresh';
RefreshRuntime.injectIntoGlobalHook(window);
window.$RefreshReg$ = () => {}; window.$RefreshSig$ = () => type => type;
window.__vite_plugin_react_preamble_installed__ = true;
const {default: React} = await import('/node_modules/.vite/deps/react.js');
const {default: {createRoot}} = await import('/node_modules/.vite/deps/react-dom_client.js');
const {useAttemptTimer} = await import('/src/useAttemptTimer.ts');
const {default: GameLibrary} = await import('/src/GameLibrary.tsx');
const {default: GameAnalysis} = await import('/src/GameAnalysis.tsx');
const {navigate} = await import('/src/router.tsx');
window.clockNow = Date.now(); Date.now = () => window.clockNow;
window.setHidden = hidden => {
  Object.defineProperty(document, 'visibilityState', {configurable: true, value: hidden ? 'hidden' : 'visible'});
  document.dispatchEvent(new Event('visibilitychange'));
};
window.saves = [];
function Harness() {
  const [options, setOptions] = React.useState({attemptId: 1, elapsedMs: 1200, running: true});
  const [screen, setScreen] = React.useState('timer');
  const paths = React.useRef({library: '/games/1', analysis: '/analysis'});
  window.options = patch => setOptions(old => ({...old, ...patch}));
  window.screen = next => {
    if (screen !== 'timer') paths.current[screen] = location.pathname;
    if (next !== 'timer') navigate(paths.current[next]);
    setScreen(next);
  };
  const timer = useAttemptTimer({...options, onPersist: (id, value, keepalive) => window.saves.push({id, value, keepalive})});
  window.snapshot = timer.snapshot;
  return React.createElement(React.Fragment, null,
    React.createElement('output', {id: 'elapsed'}, timer.elapsedMs),
    React.createElement('div', {hidden: screen !== 'library'}, React.createElement(GameLibrary, {visible: screen === 'library', onChanged: () => {}, onPractice: () => {}})),
    React.createElement('div', {hidden: screen !== 'analysis'}, React.createElement(GameAnalysis, {visible: screen === 'analysis'})));
}
createRoot(document.getElementById('root')).render(React.createElement(React.StrictMode, null, React.createElement(Harness)));
</script></body></html>'''
FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE', '/usr/bin/chromium'), args=['--no-sandbox', '--disable-features=LocalNetworkAccessChecks'])
        page = browser.new_page()
        errors, posts = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/background-check', lambda route: route.fulfill(content_type='text/html', body=HARNESS))
        game = dict(id=1, white='Tester', black='Opponent', result='1-0', date='2025.01.02', event='', game_url='', status='pending', cursor=0, total_plies=4, error=None, candidates=[], analysis_version=2, source_game_at=None)
        job = dict(id=1, source='chesscom', username='tester', month='2025-01', status='ready', background_requested=False, total_games=1, completed_games=0, candidates_count=0, available_puzzles_count=0, puzzles_count=0, analysis_version=2, analysis_outdated=False, error=None, created_at='2025-01-02T00:00:00Z', games=[game])
        source = dict(id=1, white='Tester', black='Opponent', result='1-0', date='2025.01.02', event='', game_url='', source_game_at=None, username='tester', total_plies=4, analysis_id=None, analysis_status=None, analysis_outdated=False, background_requested=False)
        side = dict(accuracy=None, provisional_accuracy=None, counts={}, total_moves=2, analysed_moves=0, assessed_moves=0, accuracy_moves=0, coverage=0, complete=False, accuracy_complete=False)
        report = dict(id=2, game_id=1, game={**source, 'initial_fen': FEN}, status='ready', background_requested=False, cursor=0, total_plies=4, analysis_version=1, analysis_outdated=False, error=None, created_at='', updated_at='', summary={'white':deepcopy(side), 'black':deepcopy(side), 'complete':False}, moves=[])

        def respond(route):
            request = route.request
            path = urlparse(request.url).path.removeprefix('/api')
            if request.method == 'POST':
                body = request.post_data_json
                posts.append((path, body))
                target = job if path.startswith('/imports') else report
                target['background_requested'] = not path.endswith('/pause')
                if target is report:
                    source.update(analysis_id=2, analysis_status='ready', background_requested=target['background_requested'])
                return route.fulfill(json=target)
            if path == '/imports': data = {'jobs':[job]}
            elif path == '/imports/1': data = job
            elif path == '/game-analysis/games': data = {'games':[source]}
            elif path == '/game-analysis/2': data = report
            else: raise AssertionError(path)
            route.fulfill(json=data)

        page.route('**/api/**', respond)
        page.goto(BASE + '/background-check')
        expect(page.locator('#elapsed')).to_have_text('1200')
        page.evaluate('clockNow += 1000; setHidden(true)')
        expect(page.locator('#elapsed')).to_have_text('2200')
        # No timer callbacks for ten simulated minutes. Hidden time is still counted.
        page.evaluate('clockNow += 600000')
        assert page.evaluate('snapshot()') == 602200
        page.evaluate('setHidden(false)')
        expect(page.locator('#elapsed')).to_have_text('602200')
        page.evaluate('options({elapsedMs: 5000})')
        expect(page.locator('#elapsed')).to_have_text('602200')
        page.evaluate('options({running: false})')
        page.evaluate('clockNow += 20000; setHidden(true); setHidden(false)')
        expect(page.locator('#elapsed')).to_have_text('602200')
        page.evaluate('options({attemptId: 2, elapsedMs: 0, running: true})')
        expect(page.locator('#elapsed')).to_have_text('0')
        page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true})); clockNow += 120000; window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
        expect(page.locator('#elapsed')).to_have_text('120000')
        assert any(save['id'] == 1 and save['value'] == 602200 for save in page.evaluate('saves'))
        print('PASS timer: hidden/frozen time, BFCache, stale saves, explicit pause, new attempt, StrictMode')

        page.evaluate("screen('library')")
        expect(page.get_by_role('button', name='Продолжить анализ', exact=True)).to_be_visible()
        page.get_by_role('button', name='Продолжить анализ', exact=True).click()
        expect(page.get_by_role('button', name='Пауза', exact=True)).to_be_visible()
        count = len(posts)
        page.evaluate("setHidden(true); screen('timer')")
        assert len(posts) == count and job['background_requested']
        job['games'][0].update(cursor=4, status='completed')
        job.update(status='completed', background_requested=False, completed_games=1)
        page.evaluate("setHidden(false); screen('library')")
        expect(page.get_by_role('progressbar', name='Анализ подборки')).to_have_attribute('aria-valuenow', '4')
        assert len(posts) == count
        page.get_by_role('button', name='Пересчитать анализ', exact=True).click()
        assert posts[-1] == ('/imports/1/reanalyze', {'background':True})
        print('PASS library: server resume, no pause/step on hide or navigation, restored completion, background restart')

        page.evaluate("screen('analysis')")
        page.get_by_role('link', name='Открыть партию Tester — Opponent', exact=True).click()
        page.get_by_role('button', name='Анализировать партию', exact=True).click()
        expect(page.get_by_role('button', name='Пауза', exact=True)).to_be_visible()
        assert posts[-1] == ('/game-analysis', {'game_id':1, 'background':True})
        count = len(posts)
        page.evaluate("setHidden(true); screen('timer')")
        assert len(posts) == count and report['background_requested']
        report.update(cursor=3)
        page.evaluate("setHidden(false); screen('analysis')")
        expect(page.get_by_role('progressbar', name='Анализ ходов партии')).to_have_attribute('aria-valuenow', '75')
        page.get_by_role('button', name='Пауза', exact=True).click()
        expect(page.get_by_role('button', name='Продолжить анализ', exact=True)).to_be_visible()
        assert posts[-1][0] == '/game-analysis/2/pause'
        page.get_by_role('button', name='Продолжить анализ', exact=True).click()
        expect(page.get_by_role('button', name='Пауза', exact=True)).to_be_visible()
        assert posts[-1][0] == '/game-analysis/2/resume'
        assert not any(path.endswith('/step') for path, _ in posts)
        assert not errors, errors
        print('PASS game analysis: initial background start, foreground refresh, explicit server pause/resume; no JS errors')
        check_training(browser)
        browser.close()


def check_training(browser):
    page = browser.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.add_init_script('''window.clockNow = Date.now(); Date.now = () => window.clockNow;
      window.setHidden = hidden => {
        Object.defineProperty(document, 'visibilityState', {configurable: true, value: hidden ? 'hidden' : 'visible'});
        document.dispatchEvent(new Event('visibilitychange'));
      };''')
    attempt = dict(id=3, puzzle_id='browser-test', fen=FEN, starting_fen=FEN, resumed=False,
                   rating=1200, themes=['opening'], status='active', turn='white', solved_plies=0,
                   total_plies=4, mistakes=0, hints_used=0, last_move=None, elapsed_ms=0,
                   collection='lichess', rating_estimated=False)

    def respond(route):
        path = urlparse(route.request.url).path.removeprefix('/api')
        if path == '/auth/me': data = {'username':'tester'}
        elif path == '/meta': data = dict(total_puzzles=1, themes=[], min_rating=800, max_rating=1800, collections=[dict(id='lichess', name='Lichess', count=1)])
        elif path == '/stats': data = dict(solved=0, total_attempts=0, accuracy=0, streak=0, due_count=0, average_solution_ms=None, timed_solutions=0, by_theme=[], recent=[])
        elif path == '/training/current': data = dict(attempt=None,solution=[],game_url='')
        elif path == '/training/3/resume': data = dict(attempt=attempt,solution=[],game_url='')
        elif path == '/training/start': data = attempt
        elif path == '/training/3/time':
            if attempt['status'] == 'active': attempt['elapsed_ms'] = route.request.post_data_json['elapsed_ms']
            data = attempt
        elif path == '/training/3/reveal':
            attempt.update(status='revealed', elapsed_ms=route.request.post_data_json['elapsed_ms'])
            data = dict(attempt=attempt, game_url='', solution=[{'uci':uci, 'san':san} for uci, san in [('e2e4','e4'), ('e7e5','e5'), ('g1f3','Nf3'), ('b8c6','Nc6')]])
        else: raise AssertionError(path)
        route.fulfill(json=data)

    page.route('**/api/**', respond)
    page.goto(BASE)
    page.get_by_role('button', name='Начать тренировку', exact=True).click()
    expect(page.get_by_role('timer')).to_contain_text('0:00')
    page.evaluate('setHidden(true); clockNow += 61000; setHidden(false)')
    expect(page.get_by_role('timer')).to_contain_text('1:01')
    page.get_by_role('button', name='Решение', exact=True).click()
    expect(page.get_by_role('button', name='Воспроизвести', exact=True)).to_be_visible()
    page.get_by_role('button', name='Воспроизвести', exact=True).click()
    page.evaluate('setHidden(true); clockNow += 10000; setHidden(false)')
    expect(page.get_by_role('button', name='Показать позицию после Nc6', exact=True)).to_have_attribute('aria-pressed', 'true')
    expect(page.get_by_role('button', name='Воспроизвести', exact=True)).to_be_visible()
    expect(page.get_by_role('timer')).to_contain_text('1:01')
    page.get_by_role('button', name='Воспроизвести', exact=True).click()
    page.get_by_role('button', name='Пауза', exact=True).click()
    page.evaluate('clockNow += 10000; setHidden(true); setHidden(false)')
    expect(page.get_by_role('button', name='Показать позицию после Nc6', exact=True)).to_have_attribute('aria-pressed', 'false')
    assert not errors, errors
    print('PASS training: background time in the app, finished time frozen, replay catches up, manual replay pause; no JS errors')
    page.close()


if __name__ == '__main__':
    run()
