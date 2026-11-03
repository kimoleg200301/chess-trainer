"""Audio integration checks: run Vite on 5174, then python tests/sound-browser.py.
Uses actual HTML audio playback promises and touch activation; no speakers required.
Requires Python Playwright and Chromium (or PLAYWRIGHT_CHROMIUM_EXECUTABLE).
"""
import os
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect

BASE = os.environ.get('TEST_FRONTEND_URL', 'http://localhost:5174')
INITIAL = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
REPLY = 'rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2'
FINISHED = 'rnbqkbnr/ppp1pppp/8/3P4/8/8/PPPP1PPP/RNBQKBNR b KQkq - 0 2'
SPY = """
window.audioStarts = []; window.audioFailures = []; window.audioElements = [];
const NativeAudio = window.Audio;
window.Audio = function(...args) {
  const audio = new NativeAudio(...args); window.audioElements.push(audio); return audio;
};
const nativePlay = HTMLMediaElement.prototype.play;
HTMLMediaElement.prototype.play = function() {
  const audible = !this.muted && this.volume > 0, audio = this;
  const promise = nativePlay.call(this);
  if (audible) promise.then(() => window.audioStarts.push({src: audio.src, duration: audio.duration, volume: audio.volume}),
    error => window.audioFailures.push(error.name));
  return promise;
};
window.setHidden = hidden => {
  Object.defineProperty(document, 'visibilityState', {configurable: true, value: hidden ? 'hidden' : 'visible'});
  document.dispatchEvent(new Event('visibilitychange'));
};
"""


def setup(browser, *, reduced=False, unavailable=False):
    context = browser.new_context(viewport={'width':360, 'height':800}, reduced_motion='reduce' if reduced else 'no-preference', is_mobile=True, has_touch=True)
    context.add_init_script(SPY if not unavailable else "window.Audio = class { constructor() { throw new Error('Audio unavailable'); } };")
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
    page.locator(f'.chessboard [data-square="{origin}"]').tap()
    page.locator(f'.chessboard [data-square="{destination}"]').tap()
    expect(page.locator('.chessboard')).to_have_attribute('aria-busy', 'false')


def starts(page, count):
    try:
        page.wait_for_function('(count) => audioStarts.length === count', arg=count, timeout=5000)
    except Exception:
        print(page.evaluate('({starts: audioStarts, failures: audioFailures, visibility: document.visibilityState})'))
        raise
    return page.evaluate('audioStarts')


def sound_settings(page):
    page.get_by_role('button', name='Настройки звука', exact=True).tap()
    expect(page.get_by_role('dialog', name='Звук', exact=True)).to_be_visible()
    box = page.get_by_role('dialog', name='Звук', exact=True).bounding_box()
    assert box['x'] >= 0 and box['x'] + box['width'] <= page.viewport_size['width']


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE', '/usr/bin/chromium'), args=['--no-sandbox', '--disable-features=LocalNetworkAccessChecks', '--autoplay-policy=user-gesture-required'])
        for reduced in (False, True):
            context, page, errors = setup(browser, reduced=reduced)
            assert page.evaluate('audioElements.length') == 0
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.get_by_role('button', name='Начать тренировку', exact=True).tap()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            move(page, 'd2', 'd4')
            assert '/wrong' in starts(page, 1)[0]['src']
            move(page, 'e2', 'e4')
            records = starts(page, 3)
            assert all('/move' in item['src'] for item in records[1:])
            move(page, 'e4', 'd5')
            records = starts(page, 5)
            assert '/capture' in records[3]['src'] and '/complete' in records[4]['src']
            assert all(item['duration'] > .1 and item['volume'] == .8 for item in records)
            page.get_by_role('button', name='К настройкам', exact=True).tap()
            page.get_by_role('button', name='Вернуться к позиции', exact=True).tap()
            assert len(page.evaluate('audioStarts')) == 5
            sound_settings(page)
            page.get_by_role('switch').tap()
            assert page.evaluate("localStorage.getItem('tactics.sound')") == 'off'
            page.reload()
            sound_settings(page)
            expect(page.get_by_role('switch')).to_have_attribute('aria-checked', 'false')
            page.get_by_role('button', name='Проверить звук', exact=True).tap()
            records = starts(page, 1)
            assert '/complete' in records[0]['src']
            expect(page.get_by_role('switch')).to_have_attribute('aria-checked', 'true')
            page.get_by_role('slider', name='Громкость').fill('50')
            page.get_by_role('button', name='Проверить звук', exact=True).tap()
            assert starts(page, 2)[1]['volume'] == .5
            page.reload()
            sound_settings(page)
            expect(page.get_by_role('slider', name='Громкость')).to_have_value('50')
            assert not errors, errors
            context.close()
            print(f'PASS actual audio: touch, move/reply/capture/error/completion, mute, preview, volume persistence; reduced={reduced}')

        context, page, errors = setup(browser)
        page.get_by_role('button', name='Начать тренировку', exact=True).tap()
        page.get_by_role('button', name='Решение', exact=True).tap()
        page.get_by_role('button', name='Показать позицию после e4', exact=True).tap()
        starts(page, 1)
        page.get_by_role('button', name='Показать позицию после exd5', exact=True).tap()
        assert len(page.evaluate('audioStarts')) == 1
        page.evaluate("async () => { window.sounds = await import(performance.getEntriesByType('resource').find(entry => new URL(entry.name).pathname === '/src/sound.ts').name); sounds.playSound('complete', .05); setHidden(true); }")
        page.wait_for_timeout(100)
        page.evaluate("sounds.playSound('complete'); setHidden(false)")
        assert len(page.evaluate('audioStarts')) == 1
        page.get_by_role('button', name='Показать позицию после d5', exact=True).tap()
        starts(page, 2)
        assert not errors, errors
        context.close()
        print('PASS replay, no burst on jumps, no hidden/delayed stale audio')

        context, page, errors = setup(browser)
        sound_settings(page)
        page.evaluate("""() => {
          const play = HTMLMediaElement.prototype.play; let blocked = false;
          HTMLMediaElement.prototype.play = function() {
            if (!this.muted && !blocked) { blocked = true; return Promise.reject(new DOMException('Blocked', 'NotAllowedError')); }
            return play.call(this);
          };
        }""")
        page.get_by_role('button', name='Проверить звук', exact=True).tap()
        expect(page.get_by_role('alert')).to_contain_text('Не удалось включить звук')
        page.get_by_role('button', name='Проверить звук', exact=True).tap()
        starts(page, 1)
        expect(page.get_by_role('alert')).to_have_count(0)
        assert not errors, errors
        context.close()
        print('PASS blocked playback: visible feedback and successful retry by touch')

        context, page, errors = setup(browser, unavailable=True)
        page.get_by_role('button', name='Начать тренировку', exact=True).tap()
        move(page, 'e2', 'e4'); move(page, 'e4', 'd5')
        expect(page.locator('.board-solved')).to_be_visible()
        sound_settings(page)
        expect(page.get_by_role('alert')).to_contain_text('Звук недоступен')
        assert not errors, errors
        context.close()
        print('PASS unavailable audio reports the problem without interrupting solving')
        browser.close()


if __name__ == '__main__':
    run()
