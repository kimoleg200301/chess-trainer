"""Favorites survive reload, other devices, stale polls and review navigation."""
from copy import deepcopy
import importlib.util
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, expect

spec = importlib.util.spec_from_file_location('review', Path(__file__).with_name('analysis-browser.py'))
review = importlib.util.module_from_spec(spec); spec.loader.exec_module(review)


def state():
    games, reports = review.fixtures()
    for game in games:
        game.update(favorite_key='fingerprint-'+str(game['id']), is_favorite=False, favorite_revision=0)
    return dict(games=games, reports=reports, posts=[], reads=[], fail=False, hold=False, pending=[])


def setup(browser, data, width=390, path='/analysis'):
    context = browser.new_context(viewport={'width':width,'height':844}, is_mobile=width<700, has_touch=True)
    page=context.new_page(); errors=[]
    page.on('pageerror', lambda error: errors.append(str(error)))
    def respond(route):
        req=route.request; path=urlparse(req.url).path.removeprefix('/api')
        if req.method=='POST':
            assert path.endswith('/favorite'), path
            data['posts'].append((path,req.post_data_json))
            if data['fail']:
                route.fulfill(status=503,json={'detail':'Не удалось сохранить избранное. Повторите позже.'}); return
            game=next(game for game in data['games'] if game['id']==int(path.split('/')[-2]))
            game.update(is_favorite=req.post_data_json['is_favorite'],favorite_revision=game['favorite_revision']+1)
            result={key:game[key] for key in ('favorite_key','is_favorite','favorite_revision')}
            if data['hold']:
                data['pending'].append((route,result)); return
            route.fulfill(json=result); return
        if path=='/auth/me': result={'username':'tester'}
        elif path=='/training/current': result=dict(attempt=None,solution=[],game_url='')
        elif path=='/meta': result=dict(total_puzzles=1,themes=[],min_rating=800,max_rating=1800,collections=[dict(id='lichess',name='Lichess',count=1)])
        elif path=='/stats': result=dict(solved=0,total_attempts=0,accuracy=0,streak=0,due_count=0,average_solution_ms=None,timed_solutions=0,by_theme=[],recent=[])
        elif path=='/game-analysis/games': result={'games':data['reads'].pop(0) if data['reads'] else data['games']}
        elif path=='/game-analysis/11': result=data['reports'][0]
        else: raise AssertionError(path)
        route.fulfill(json=result)
    page.route('**/api/**',respond)
    page.goto(review.BASE+path)
    return context,page,errors


def star(page, selected=False, player='Player1', opponent='Opponent1'):
    return page.get_by_role('button',name=f"{'Убрать из избранного' if selected else 'Добавить в избранное'}: {player} — {opponent}",exact=True)


def check_saved(browser, width):
    data=state(); context,page,errors=setup(browser,data,width)
    page.get_by_role('button',name='Избранные',exact=True).click()
    expect(page.get_by_role('heading',name='Пока нет избранных партий')).to_be_visible()
    page.get_by_role('button',name='Показать все партии',exact=True).click()
    expect(page.locator('a button')).to_have_count(0)
    star(page).click()
    expect(star(page,True)).to_have_attribute('aria-pressed','true')
    assert urlparse(page.url).path=='/analysis', 'A star must not open the game'
    assert data['posts']==[('/game-analysis/games/1/favorite',{'is_favorite':True})]
    page.reload(); expect(star(page,True)).to_be_visible()
    page.get_by_role('button',name='Избранные',exact=True).click()
    expect(page.locator('.analysis-game-card')).to_have_count(1)
    page.reload(); expect(page.get_by_role('button',name='Избранные',exact=True)).to_have_attribute('aria-pressed','true')
    page.get_by_role('link',name='Открыть партию Player1 — Opponent1',exact=True).click()
    expect(page.get_by_role('region',name='Интерактивный разбор партии')).to_be_visible()
    assert urlparse(page.url).query=='filter=favorites'
    expect(star(page,True)).to_be_visible()
    board=page.locator('.analysis-position-board').bounding_box()
    star(page,True).click();expect(star(page)).to_have_attribute('aria-pressed','false')
    assert page.locator('.analysis-position-board').bounding_box()==board
    star(page).click();expect(star(page,True)).to_have_attribute('aria-pressed','true')
    page.get_by_role('button',name='К партиям',exact=True).click()
    expect(page.locator('.analysis-game-card')).to_have_count(1)
    expect(page.get_by_role('button',name='Избранные',exact=True)).to_have_attribute('aria-pressed','true')
    page.screenshot(path=f'/workspace/scratch/game-favorites-{width}.png',full_page=True)
    other,other_page,other_errors=setup(browser,data,width)
    expect(star(other_page,True)).to_be_visible()
    star(other_page,True).click();expect(star(other_page)).to_be_visible()
    review.refresh(page)
    expect(page.get_by_role('heading',name='Пока нет избранных партий')).to_be_visible()
    assert not errors and not other_errors,(errors,other_errors)
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    other.close();context.close()
    print(f'PASS {width}px favorites: star, reload, saved filter URL, review star, stable board, back navigation, second device, removal')


def check_errors_and_races(browser):
    data=state();context,page,errors=setup(browser,data)
    data['fail']=True; star(page).click()
    expect(page.get_by_role('alert')).to_contain_text('Не удалось сохранить избранное')
    expect(star(page)).to_have_attribute('aria-pressed','false')
    data['fail']=False; old=deepcopy(data['games']);star(page).click()
    expect(star(page,True)).to_be_visible()
    data['reads'].append(old);review.refresh(page)
    page.wait_for_timeout(300)
    expect(star(page,True)).to_have_attribute('aria-pressed','true')
    # An in-flight write cannot override a newer response from another device.
    data['hold']=True; star(page,True).click()
    expect(star(page,True)).to_be_disabled()
    assert len(data['posts'])==3
    data['games'][0].update(is_favorite=True,favorite_revision=3)
    review.refresh(page)
    page.wait_for_timeout(300)
    route,result=data['pending'].pop();route.fulfill(json=result)
    expect(star(page,True)).to_be_enabled()
    expect(star(page,True)).to_have_attribute('aria-pressed','true')
    assert len(data['posts'])==3 and not errors,errors
    context.close()
    print('PASS favorite errors/races: no false success, retry, stale GET and POST ignored, pending button disabled')


if __name__=='__main__':
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path='/usr/bin/chromium',args=['--no-sandbox','--disable-features=LocalNetworkAccessChecks'])
        check_saved(browser,390);check_saved(browser,1280);check_errors_and_races(browser)
        browser.close()
