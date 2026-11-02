"""Audio integration checks: run Vite on 5174, then python tests/sound-browser.py.
Uses real Web Audio with observed buffer starts; no speakers are required.
Requires Python Playwright and Chromium (or PLAYWRIGHT_CHROMIUM_EXECUTABLE).
"""
import os
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect

BASE = os.environ.get('TEST_FRONTEND_URL', 'http://localhost:5174')
INITIAL = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
REPLY = 'rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2'
FINISHED = 'rnbqkbnr/ppp1pppp/8/3P4/8/8/PPPP1PPP/RNBQKBNR b KQkq - 0 2'
SPY = '''
window.audioStarts = []; window.audioContexts = [];
const NativeAudio = window.AudioContext;
window.AudioContext = class extends NativeAudio {
  constructor(...args) { super(...args); window.audioContexts.push(this); }
  createBufferSource() {
    const source = super.createBufferSource(), start = source.start.bind(source);
    source.start = (...args) => {
      const samples = source.buffer.getChannelData(0);
      const energy = samples.reduce((sum, value) => sum + value * value, 0);
      window.audioStarts.push({duration: source.buffer.duration, rms: Math.sqrt(energy / samples.length),
        peak: samples.reduce((peak, value) => Math.max(peak, Math.abs(value)), 0), state: this.state,
        signature: Array.from(samples.slice(40, 48)), stopped: false});
      const record = window.audioStarts.at(-1), stop = source.stop.bind(source);
      source.stop = (...args) => { record.stopped = true; return stop(...args); };
      return start(...args);
    };
    return source;
  }
};
window.setHidden = hidden => {
  Object.defineProperty(document, 'visibilityState', {configurable: true, value: hidden ? 'hidden' : 'visible'});
  document.dispatchEvent(new Event('visibilitychange'));
};
'''


def setup(browser, *, reduced=False, unavailable=False):
    context = browser.new_context(viewport={'width':360, 'height':800}, reduced_motion='reduce' if reduced else 'no-preference')
    context.add_init_script(SPY if not unavailable else "window.AudioContext = class { constructor() { throw new Error('Audio unavailable'); } };")
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    attempt = dict(id=3, puzzle_id='audio-test', fen=INITIAL, starting_fen=INITIAL, resumed=False,
                   rating=1200, themes=['opening'], status='active', turn='white', solved_plies=0,
                   total_plies=3, mistakes=0, hints_used=0, last_move=None, elapsed_ms=0,
                   collection='lichess', rating_estimated=False)

    def respond(route):
        path = urlparse(route.request.url).path.removeprefix('/api')
        if path == '/auth/me': data = {'username':'tester'}
        elif path == '/meta': data = dict(total_puzzles=1, themes=[], min_rating=800, max_rating=1800, collections=[dict(id='lichess', name='Lichess', count=1)])
        elif path == '/stats': data = dict(solved=0, total_attempts=0, accuracy=0, streak=0, due_count=0, average_solution_ms=None, timed_solutions=0, by_theme=[], recent=[])
        elif path == '/training/start': data = attempt
        elif path == '/training/3/time': data = attempt
        elif path == '/training/3/move':
            move = route.request.post_data_json['move']
            accepted = move in ('e2e4', 'e4d5')
            reply = 'd7d5' if move == 'e2e4' else None
            if move == 'e2e4': attempt.update(fen=REPLY, last_move=reply, solved_plies=2)
            elif move == 'e4d5': attempt.update(fen=FINISHED, last_move=move, status='completed', solved_plies=3)
            else: attempt['mistakes'] += 1
            data = dict(accepted=accepted, attempt=attempt, reply_move=reply, feedback='Верно!' if accepted else 'Попробуйте ещё раз.')
        elif path == '/training/3/reveal':
            attempt.update(status='revealed')
            data = dict(attempt=attempt, game_url='', solution=[{'uci':uci, 'san':san} for uci, san in [('e2e4','e4'), ('d7d5','d5'), ('e4d5','exd5')]])
        else: raise AssertionError(path)
        route.fulfill(json=data)

    page.route('**/api/**', respond)
    page.goto(BASE)
    expect(page.get_by_role('button', name='Начать тренировку', exact=True)).to_be_visible()
    return context, page, errors


def move(page, origin, destination):
    expect(page.locator('.chessboard')).to_have_attribute('aria-busy', 'false')
    page.locator(f'.chessboard [data-square="{origin}"]').click()
    page.locator(f'.chessboard [data-square="{destination}"]').click()
    expect(page.locator('.chessboard')).to_have_attribute('aria-busy', 'false')


def starts(page, count):
    try:
        page.wait_for_function('(count) => audioStarts.length === count', arg=count, timeout=5000)
    except Exception:
        print(page.evaluate('({starts: audioStarts, states: audioContexts.map(c => c.state), visibility: document.visibilityState})'))
        raise
    return page.evaluate('audioStarts')


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE', '/usr/bin/chromium'), args=['--no-sandbox', '--disable-features=LocalNetworkAccessChecks', '--autoplay-policy=user-gesture-required'])
        for reduced in (False, True):
            context, page, errors = setup(browser, reduced=reduced)
            assert page.evaluate('audioContexts.length') == 0  # No autoplay before interaction.
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.get_by_role('button', name='Начать тренировку', exact=True).click()
            page.wait_for_function("audioContexts.length === 1 && audioContexts[0].state === 'running'")
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            move(page, 'd2', 'd4')
            assert round(starts(page, 1)[0]['duration'], 2) == .24
            move(page, 'e2', 'e4')
            records = starts(page, 3)
            assert [round(item['duration'], 2) for item in records] == [.24, .13, .13]
            move(page, 'e4', 'd5')
            records = starts(page, 5)
            assert [round(item['duration'], 2) for item in records[-2:]] == [.13, .7]
            assert records[1]['signature'] != records[3]['signature']  # Capture has its own timbre.
            assert all(0.005 < item['rms'] < .2 and item['peak'] < 1 and item['state'] == 'running' for item in records)
            page.get_by_role('button', name='К настройкам', exact=True).click()
            page.get_by_role('button', name='Вернуться к позиции', exact=True).click()
            assert len(page.evaluate('audioStarts')) == 5  # Re-rendering a solved task is silent.
            page.get_by_role('button', name='Выключить звук', exact=True).click()
            assert page.evaluate("localStorage.getItem('tactics.sound')") == 'off'
            page.reload()
            expect(page.get_by_role('button', name='Включить звук', exact=True)).to_be_visible()
            assert page.evaluate('audioContexts.length') == 0
            assert not errors, errors
            context.close()
            print(f'PASS training audio: move, reply, capture, error, completion once, persisted mute, mobile layout; reduced_motion={reduced}')

        context, page, errors = setup(browser)
        page.get_by_role('button', name='Начать тренировку', exact=True).click()
        page.get_by_role('button', name='Решение', exact=True).click()
        page.get_by_role('button', name='Показать позицию после e4', exact=True).click()
        starts(page, 1)
        # Skip multiple replay moves: no misleading burst of intermediate sounds.
        page.get_by_role('button', name='Показать позицию после exd5', exact=True).click()
        assert len(page.evaluate('audioStarts')) == 1
        page.get_by_role('button', name='Выключить звук', exact=True).click()
        page.get_by_role('button', name='Показать позицию после d5', exact=True).click()
        assert len(page.evaluate('audioStarts')) == 1
        page.get_by_role('button', name='Включить звук', exact=True).click()
        page.evaluate("async () => { window.sounds = await import(performance.getEntriesByType('resource').find(entry => new URL(entry.name).pathname === '/src/sound.ts').name); sounds.playSound('complete', 10); setHidden(true); }")
        assert starts(page, 2)[-1]['stopped']
        page.evaluate("sounds.playSound('complete'); setHidden(false)")
        assert len(page.evaluate('audioStarts')) == 2
        page.evaluate("async () => { await audioContexts[0].suspend(); sounds.playSound('complete'); }")
        assert len(page.evaluate('audioStarts')) == 2
        page.get_by_role('button', name='Показать позицию после exd5', exact=True).click()
        starts(page, 3)
        assert not errors, errors
        context.close()
        print('PASS replay steps, muted playback, cancellation on hide, no hidden/suspended audio queue, gesture resume')

        context, page, errors = setup(browser, unavailable=True)
        page.get_by_role('button', name='Начать тренировку', exact=True).click()
        move(page, 'e2', 'e4'); move(page, 'e4', 'd5')
        expect(page.locator('.board-solved')).to_be_visible()
        assert not errors, errors
        context.close()
        print('PASS unavailable audio does not interrupt solving')
        browser.close()


if __name__ == '__main__':
    run()
