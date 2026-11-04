"""Nickname search, archive results, saved routes, and out-of-order responses."""
import importlib.util
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright, expect

spec = importlib.util.spec_from_file_location('favorites', Path(__file__).with_name('game-favorites-browser.py'))
favorites = importlib.util.module_from_spec(spec); spec.loader.exec_module(favorites)


def setup(browser, width=390):
    data = favorites.state()
    data['games'][0].update(is_favorite=True, favorite_revision=1)
    context, page, errors = favorites.setup(browser, data, width)
    expect(page.locator('.analysis-game-card')).to_have_count(2)
    archive = [*data['games'], dict(data['games'][1], id=3, white='ArchivePlayer', black='OldOpponent', favorite_key='old')]
    controls = dict(hold='', pending=[], failed=False, queries=[])

    def respond(route):
        params = parse_qs(urlparse(route.request.url).query)
        query = params.get('q', [''])[0]
        controls['queries'].append(query)
        selected = params.get('selected_game_id', [''])[0]
        result = {'games': [g for g in (archive if query else data['games']) if
                           not query or query in g['white'].lower() or query in g['black'].lower() or str(g['id']) == selected]}
        if query == controls['hold'] and query:
            controls['pending'].append((route, result)); return
        if controls['failed']:
            route.fulfill(status=503, json={'detail': 'Сбой поиска. Повторите позже.'}); return
        route.fulfill(json=result)

    page.route('**/api/game-analysis/games*', respond)
    return context, page, errors, controls


def check_search(browser, width):
    context, page, errors, controls = setup(browser, width)
    field = page.get_by_label('Поиск по нику', exact=True)
    field.fill('  pLaYeR1  ')
    expect(page.locator('.analysis-game-card')).to_have_count(1)
    page.get_by_role('button', name='Избранные', exact=True).click()
    page.get_by_role('link', name='Открыть партию Player1 — Opponent1', exact=True).click()
    expect(page.get_by_role('region', name='Интерактивный разбор партии')).to_be_visible()
    assert parse_qs(urlparse(page.url).query) == {'filter': ['favorites'], 'q': ['  pLaYeR1  ']}
    page.reload()
    expect(page.get_by_role('region', name='Интерактивный разбор партии')).to_be_visible()
    page.get_by_role('button', name='К партиям', exact=True).click()
    expect(field).to_have_value('  pLaYeR1  ')
    expect(page.get_by_role('button', name='Избранные', exact=True)).to_have_attribute('aria-pressed', 'true')
    field.fill('oPpOnEnT2')
    expect(page.get_by_role('heading', name='Партии не найдены', exact=True)).to_be_visible()
    page.get_by_role('button', name='Все партии', exact=True).click()
    expect(field).to_have_value('oPpOnEnT2')
    expect(page.get_by_role('link', name='Открыть партию Player2 — Opponent2', exact=True)).to_be_visible()
    field.fill('ARCHIVE')
    expect(page.get_by_role('link', name='Открыть партию ArchivePlayer — OldOpponent', exact=True)).to_be_visible()
    assert 'archive' in controls['queries'], 'Old games must be searched on the server'
    page.screenshot(path=f'/workspace/scratch/game-search-{width}.png', full_page=True)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    field.fill('not-a-player')
    expect(page.get_by_role('heading', name='Партии не найдены', exact=True)).to_be_visible()
    page.get_by_role('button', name='Сбросить поиск', exact=True).click()
    expect(field).to_have_value('')
    expect(page.locator('.analysis-game-card')).to_have_count(2)
    field.fill('opponent1')
    expect(page.locator('.analysis-game-card')).to_have_count(1)
    page.get_by_role('button', name='Очистить поиск по нику', exact=True).click()
    expect(page.locator('.analysis-game-card')).to_have_count(2)
    assert not errors, errors
    context.close()
    print(f'PASS {width}px search: both players, case/whitespace, archive, favorites, reload/review/back, empty/reset, layout')


def check_races(browser):
    context, page, errors, controls = setup(browser)
    field = page.get_by_label('Поиск по нику', exact=True)
    controls['hold'] = 'player1'; field.fill('player1')
    page.wait_for_timeout(400)
    assert controls['pending']
    field.fill('player2')
    expect(page.get_by_role('link', name='Открыть партию Player2 — Opponent2', exact=True)).to_be_visible()
    page.wait_for_timeout(400)
    assert 'player2' in controls['queries']
    controls['hold'] = ''
    for route, result in controls['pending']:
        route.fulfill(json=result)
    controls['pending'].clear()
    page.wait_for_timeout(150)
    expect(page.get_by_role('link', name='Открыть партию Player2 — Opponent2', exact=True)).to_be_visible()
    expect(page.locator('.analysis-game-card')).to_have_count(1)
    controls['failed'] = True; field.fill('missing')
    expect(page.get_by_role('heading', name='Не удалось выполнить поиск', exact=True)).to_be_visible()
    expect(page.get_by_role('alert')).to_contain_text('Сбой поиска')
    controls['failed'] = False; field.fill('archive')
    expect(page.get_by_role('link', name='Открыть партию ArchivePlayer — OldOpponent', exact=True)).to_be_visible()
    expect(page.get_by_role('alert')).to_have_count(0)
    assert not errors, errors
    context.close()
    print('PASS search races: delayed results ignored, network failure distinguished from empty results, recovery')


if __name__ == '__main__':
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path='/usr/bin/chromium', args=['--no-sandbox', '--disable-features=LocalNetworkAccessChecks'])
        check_search(browser, 390); check_search(browser, 1280); check_races(browser)
        browser.close()
