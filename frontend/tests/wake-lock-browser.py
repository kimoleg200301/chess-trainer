"""Screen-awake lifecycle on insecure HTTP and native-capable origins.

Uses the real training UI and local video assets with isolated API fixtures.
Controlled media/native promises test races separately from real playback.
Desktop Chromium cannot prove whether a physical phone's screen stays powered.
Run through run_browser_tests.py, or with Vite on TEST_FRONTEND_URL.
"""
from copy import deepcopy
import importlib.util
import os
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

from browser_support import chromium_options

BASE = os.environ.get('TEST_FRONTEND_URL', 'http://127.0.0.1:5174').rstrip('/')
# Route this documentation-only address to the local Vite server. Chromium still
# sees an actual non-loopback HTTP origin: isSecureContext is naturally false.
HTTP = 'http://192.0.2.1:5174'
spec = importlib.util.spec_from_file_location('coach_fixture', Path(__file__).with_name('coach-browser.py'))
coach = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coach)

OBSERVE = r'''(() => {
  window.__visible = 'visible';
  Object.defineProperty(document, 'visibilityState', {get: () => window.__visible, configurable: true});
  window.__visibility = value => {
    window.__visible = value;
    document.dispatchEvent(new Event('visibilitychange'));
  };
  const state = window.__media = {videos: [], calls: [], pending: [], mode: 'real', pauses: 0};
  const create = document.createElement.bind(document);
  document.createElement = function(tag, ...args) {
    const element = create(tag, ...args);
    if (tag.toLowerCase() === 'video') {
      element.__ticks = 0;
      element.addEventListener('timeupdate', () => element.__ticks++);
      state.videos.push(element);
    }
    return element;
  };
  const play = HTMLMediaElement.prototype.play;
  const pause = HTMLMediaElement.prototype.pause;
  HTMLMediaElement.prototype.play = function() {
    if (!(this instanceof HTMLVideoElement)) return play.call(this);
    state.calls.push(this);
    if (state.mode === 'real') return play.call(this);
    if (!Object.hasOwn(this, '__paused')) {
      this.__paused = true;
      Object.defineProperty(this, 'paused', {get: () => this.__paused});
    }
    if (state.mode === 'reject') return Promise.reject(new DOMException('Fixture autoplay rejection', 'NotAllowedError'));
    if (state.mode === 'pending') {
      this.__paused = false; // play() changes paused before playback is ready.
      return new Promise((resolve, reject) => {
        state.pending.push({video: this, resolve: () => { this.__paused = false; resolve(); }, reject});
      });
    }
    this.__paused = false;
    return Promise.resolve();
  };
  HTMLMediaElement.prototype.pause = function() {
    if (this instanceof HTMLVideoElement) {
      state.pauses++;
      if (Object.hasOwn(this, '__paused')) this.__paused = true;
    }
    return pause.call(this);
  };
  window.__mediaSnapshot = () => state.videos.map(video => ({
    paused: video.paused, muted: video.muted, inline: video.playsInline,
    connected: video.isConnected, ticks: video.__ticks, time: video.currentTime,
    sources: [...video.querySelectorAll('source')].map(source => source.getAttribute('src')),
    src: video.getAttribute('src'), error: video.error?.code,
  }));
})();'''

NATIVE = r'''(() => {
  const state = window.__native = {mode: 'resolve', requests: [], locks: [], pending: [], releases: 0, maxActive: 0};
  function sentinel() {
    const lock = new EventTarget();
    lock.released = false;
    lock.release = () => {
      state.releases++;
      if (!lock.released) { lock.released = true; lock.dispatchEvent(new Event('release')); }
      return Promise.resolve();
    };
    state.locks.push(lock);
    state.maxActive = Math.max(state.maxActive, state.locks.filter(item => !item.released).length);
    return lock;
  }
  Object.defineProperty(navigator, 'wakeLock', {configurable: true, value: {
    request: type => {
      state.requests.push(type);
      if (state.mode === 'reject') return Promise.reject(new DOMException('Fixture denial', 'NotAllowedError'));
      if (state.mode === 'pending') return new Promise(resolve => state.pending.push(() => resolve(sentinel())));
      return Promise.resolve(sentinel());
    }
  }});
  window.__releaseUnexpectedly = () => {
    const lock = state.locks.find(item => !item.released);
    if (!lock) throw new Error('Expected a live native lock');
    lock.released = true;
    lock.dispatchEvent(new Event('release'));
  };
})();'''


class Fixture:
    def __init__(self, browser, *, native=False, media='real', native_mode='resolve'):
        self.context = browser.new_context(viewport={'width': 390, 'height': 844}, has_touch=True)
        self.page = self.context.new_page()
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.origin = BASE if native else HTTP
        self.context.add_init_script(OBSERVE)
        if native:
            self.context.add_init_script(NATIVE)
        # Separate init scripts have unspecified execution order; select mode
        # after navigation, before the practice screen enables the hook.
        self.media_mode, self.native_mode = media, native_mode
        if not native:
            def local_asset(route):
                parsed = urlparse(route.request.url)
                local = BASE + parsed.path + ('?' + parsed.query if parsed.query else '')
                route.fulfill(response=route.fetch(url=local))
            self.context.route(HTTP + '/**', local_asset)
        self.context.route('**/api/**', self.respond)
        self.context.route('https://fonts.googleapis.com/**', lambda route: route.abort())
        self.dashboard = coach.fixtures()
        self.dashboard['profile'].update(status='tracking', baseline_games=40, baseline_completed=40,
                                         baseline_ready=True, progress=100, syncing=False)
        self.attempt = None
        self.ordinary = None
        self.serial = 0
        self.logged_out = False
        self.expire_next_action = False

    def respond(self, route):
        path = urlparse(route.request.url).path.removeprefix('/api')
        body = route.request.post_data_json if route.request.method == 'POST' else {}
        if path == '/auth/logout':
            self.logged_out = True
            return route.fulfill(status=204)
        if self.expire_next_action and path.endswith('/hint'):
            self.expire_next_action = False
            return route.fulfill(status=401, json={'detail': 'Сессия истекла.'})
        if path == '/auth/me': result = {'username': 'tester'}
        elif path == '/meta':
            result = dict(total_puzzles=100, themes=[], min_rating=400, max_rating=2000,
                          collections=[dict(id='lichess', name='Lichess', count=100)])
        elif path == '/stats':
            result = dict(solved=0, total_attempts=0, accuracy=0, streak=0, due_count=0,
                          average_solution_ms=None, timed_solutions=0, puzzle_rating=1200,
                          rated_attempts=0, by_theme=[], recent=[])
        elif path == '/training/current': result = dict(attempt=None, solution=[], game_url='')
        elif path == '/coach': result = self.dashboard
        elif path == '/coach/practice':
            self.serial += 1
            self.attempt = dict(id=self.serial, skill_id=coach.IDS[0], skill_title=coach.TITLES[0], kind='decision',
                fen=coach.FEN, starting_fen=coach.FEN, learner_white=True, status='active', mistakes=0, hints_used=0,
                elapsed_ms=0, last_move=None, ply_index=0, total_plies=1, prompt='Найди сильный ход.', feedback='',
                is_new=True, source=dict(game_url='', move_number=1, own_game=True), solution=None, target_seconds=None)
            self.dashboard['active_attempt_id'] = self.serial
            result = self.attempt
        elif path.startswith('/coach/practice/'):
            if path.endswith('/move'):
                assert body['move'] == 'e2e4', body
                self.attempt.update(status='completed', fen=coach.E4, last_move='e2e4', ply_index=1,
                    solution=dict(initial_fen=coach.FEN, moves=deepcopy(coach.LONG_SOLUTION), kind='reference', explanation='Проверь продолжение.'))
                self.dashboard['active_attempt_id'] = None
                result = dict(accepted=True, attempt=self.attempt, feedback='Верно.')
            elif path.endswith('/reveal'):
                self.attempt.update(status='revealed', solution=dict(initial_fen=coach.FEN,
                    moves=deepcopy(coach.LONG_SOLUTION), kind='reference', explanation='Проверь продолжение.'))
                result = self.attempt
            else: result = self.attempt
        elif path == '/training/start':
            self.ordinary = dict(id=100, puzzle_id='awake', fen=coach.FEN, starting_fen=coach.FEN, rating=1200,
                themes=['opening'], status='active', turn='white', solved_plies=0, total_plies=1, mistakes=0,
                hints_used=0, last_move=None, elapsed_ms=0, collection='lichess', rating_estimated=False)
            result = self.ordinary
        elif path.startswith('/training/100/'):
            if path.endswith('/reveal'):
                self.ordinary['status'] = 'revealed'
                result = dict(attempt=self.ordinary, solution=deepcopy(coach.LONG_SOLUTION), game_url='')
            else: result = self.ordinary
        else:
            self.errors.append(f'Unexpected API {path}')
            return route.fulfill(status=500, json={'detail': 'Unknown fixture request'})
        route.fulfill(json=result)

    def open(self, route='/coach'):
        self.page.goto(self.origin + route)
        self.page.evaluate('(mode) => { __media.mode = mode; }', self.media_mode)
        if self.origin == BASE:
            self.page.evaluate('(mode) => { __native.mode = mode; }', self.native_mode)
        else:
            assert self.page.evaluate('!isSecureContext && !("wakeLock" in navigator)')
        expect(self.page.locator('h1:visible')).to_be_visible()

    def start(self):
        self.page.get_by_role('button', name='Начать занятие', exact=True).click()
        expect(self.page.get_by_role('region', name='Занятие с личным тренером')).to_be_visible()

    def interact(self):
        self.page.locator('.coach-session [data-square="e2"]').click()

    def back(self):
        self.page.get_by_role('button', name='К плану', exact=True).click()
        expect(self.page.get_by_role('heading', name='Игра становится практикой')).to_be_visible()

    def close(self):
        assert not self.errors, self.errors
        self.context.unroute_all(behavior='wait')
        self.context.close()


def no_playing(page):
    page.wait_for_function('__media.videos.every(video => video.paused)')


def active_media(page):
    page.wait_for_function('__media.videos.some(video => !video.paused)')
    assert page.evaluate('__media.videos.filter(video => !video.paused).length') == 1


def check_http_playback(browser):
    fixture = Fixture(browser)
    page = fixture.page
    fixture.open()
    assert page.evaluate('__media.calls.length') == 0
    fixture.start()
    # Starting the exercise is itself a real gesture. Do not hide a failed
    # initial activation by touching the board before this assertion.
    active_media(page)
    page.wait_for_function('__media.videos.some(video => video.__ticks >= 12 && video.currentTime > 0 && !video.paused)')
    video = page.evaluate('__mediaSnapshot().find(video => !video.paused)')
    assert video['inline'] and not video['muted'] and not video['error'], video
    assert video['sources'] and all(source.startswith('data:video/') or urlparse(source).netloc == urlparse(HTTP).netloc
                                    for source in video['sources']), video
    assert not video['connected'], video
    fixture.interact()
    page.locator('.coach-session [data-square="e4"]').click()
    expect(page.get_by_role('heading', name='Сохрани эту идею')).to_be_visible()
    page.locator('.coach-solution-moves button').last.click()
    active_media(page)
    page.evaluate('__visibility("hidden")')
    no_playing(page)
    page.evaluate('__visibility("visible")')
    page.locator('.coach-solution-moves button').first.click()
    active_media(page)
    page.evaluate('window.dispatchEvent(new Event("pagehide"))')
    no_playing(page)
    page.evaluate('window.dispatchEvent(new Event("pageshow"))')
    active_media(page)
    fixture.back()
    no_playing(page)
    assert page.evaluate('__media.videos.every(video => !video.getAttribute("src") && !video.querySelector("source"))')
    before = page.evaluate('__media.calls.length')
    page.get_by_role('heading', name='Игра становится практикой').click()
    assert page.evaluate('__media.calls.length') == before
    page.get_by_role('button', name='Выйти', exact=True).click()
    expect(page.get_by_role('heading', name='Вернёмся к практике')).to_be_visible()
    assert fixture.logged_out
    no_playing(page)
    fixture.close()
    print('PASS insecure HTTP: start click starts real local video for 12+ updates; playback persists through completed/replay; hide/show/back/logout cleanup')


def check_media_races(browser):
    fixture = Fixture(browser, media='reject')
    page = fixture.page
    fixture.open(); fixture.start()
    page.wait_for_function('__media.calls.length > 0')
    no_playing(page)
    rejected = page.evaluate('__media.calls.length')
    page.evaluate('document.dispatchEvent(new Event("click"))')
    page.wait_for_timeout(150)
    assert page.evaluate('__media.calls.length') == rejected
    page.evaluate('__media.mode = "resolve"')
    fixture.interact()
    active_media(page)
    # An expired API session unmounts practice directly, without visiting the
    # dashboard first (the ordinary logout button lives on that dashboard).
    fixture.expire_next_action = True
    page.get_by_role('button', name='Подсказка', exact=True).click()
    expect(page.get_by_role('heading', name='Вернёмся к практике')).to_be_visible()
    no_playing(page)
    assert page.evaluate('__media.videos.every(video => !video.querySelector("source"))')
    fixture.close()

    fixture = Fixture(browser, media='pending')
    page = fixture.page
    fixture.open(); fixture.start()
    page.wait_for_function('__media.pending.length === 1')
    fixture.interact(); fixture.interact()
    assert page.evaluate('__media.pending.length') == 1
    page.evaluate('__visibility("hidden"); __visibility("visible")')
    assert page.evaluate('__media.pending.length') == 1
    fixture.back()
    page.evaluate('__media.pending.shift().resolve()')
    no_playing(page)
    assert page.evaluate('__media.videos.every(video => !video.getAttribute("src") && !video.querySelector("source"))')
    fixture.close()
    print('PASS HTTP fallback races: rejected play retries on interaction; logout and late play release resources')


def check_native(browser):
    fixture = Fixture(browser, native=True, native_mode='pending')
    page = fixture.page
    fixture.open(); fixture.start()
    page.wait_for_function('__native.pending.length === 1')
    fixture.interact(); fixture.interact()
    assert page.evaluate('__native.requests') == ['screen']
    page.evaluate('__visibility("hidden"); __visibility("visible")')
    assert page.evaluate('__native.requests') == ['screen']
    fixture.expire_next_action = True
    page.get_by_role('button', name='Подсказка', exact=True).click()
    expect(page.get_by_role('heading', name='Вернёмся к практике')).to_be_visible()
    page.evaluate('__native.pending.shift()()')
    page.wait_for_function('__native.locks.every(lock => lock.released)')
    assert page.evaluate('__native.releases') == 1
    assert page.evaluate('__media.calls.length') == 0
    fixture.close()

    fixture = Fixture(browser, native=True)
    page = fixture.page
    fixture.open(); fixture.start()
    page.wait_for_function('__native.locks.length === 1')
    fixture.interact(); fixture.interact()
    assert page.evaluate('__native.requests.length') == 1
    page.evaluate('__releaseUnexpectedly()')
    page.wait_for_function('__native.requests.length === 2')
    page.evaluate('__releaseUnexpectedly()')
    page.wait_for_timeout(1200)
    assert page.evaluate('__native.requests.length') == 2, 'Unexpected releases must not produce a retry loop'
    fixture.interact()
    page.wait_for_function('__native.requests.length === 3')
    page.evaluate('__visibility("hidden")')
    page.wait_for_function('__native.locks.every(lock => lock.released)')
    page.evaluate('__visibility("visible")')
    page.wait_for_function('__native.requests.length === 4')
    page.evaluate('window.dispatchEvent(new Event("pagehide"))')
    page.wait_for_function('__native.locks.every(lock => lock.released)')
    page.evaluate('window.dispatchEvent(new Event("pageshow"))')
    page.wait_for_function('__native.requests.length === 5')
    fixture.back()
    page.wait_for_function('__native.locks.every(lock => lock.released)')
    assert page.evaluate('__native.maxActive') == 1
    assert page.evaluate('__media.calls.length') == 0
    fixture.close()

    fixture = Fixture(browser, native=True, native_mode='reject')
    page = fixture.page
    fixture.open(); fixture.start()
    page.wait_for_function('__native.requests.length === 1')
    page.evaluate('document.dispatchEvent(new Event("click"))')
    page.wait_for_timeout(150)
    assert page.evaluate('__native.requests.length') == 1
    assert page.evaluate('__media.videos.length') == 0, 'A native denial must not be bypassed with video'
    page.evaluate('__native.mode = "resolve"')
    fixture.interact()
    page.wait_for_function('__native.locks.length === 1')
    fixture.back()
    page.wait_for_function('__native.locks.every(lock => lock.released)')
    fixture.close()
    print('PASS native: single acquisition, pending navigation cleanup, bounded release recovery and visibility lifecycle')


def check_ordinary(browser):
    fixture = Fixture(browser, media='resolve')
    page = fixture.page
    fixture.open('/')
    page.get_by_role('button', name='Начать тренировку', exact=True).click()
    expect(page.locator('.session-toolbar')).to_be_visible()
    page.locator('.chessboard [data-square="e2"]').click()
    active_media(page)
    page.get_by_role('button', name='Решение', exact=True).click()
    expect(page.locator('.training-result')).to_be_visible()
    active_media(page)
    page.get_by_role('button', name='К настройкам', exact=True).click()
    expect(page.get_by_role('heading', name='Начните тренировку')).to_be_visible()
    no_playing(page)
    fixture.close()
    print('PASS ordinary trainer: shared HTTP fallback covers active/revealed practice and releases on settings')


if __name__ == '__main__':
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**chromium_options(), args=['--no-sandbox', '--disable-features=LocalNetworkAccessChecks'])
        check_http_playback(browser)
        check_media_races(browser)
        check_native(browser)
        check_ordinary(browser)
        browser.close()
