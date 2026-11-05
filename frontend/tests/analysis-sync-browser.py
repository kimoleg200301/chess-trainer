"""Saved Chess.com account and synchronization across routes and browser sessions."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
from urllib.parse import urlparse
from browser_support import chromium_options, artifact_path
from playwright.sync_api import sync_playwright, expect

spec=importlib.util.spec_from_file_location('review',Path(__file__).with_name('analysis-browser.py'))
review=importlib.util.module_from_spec(spec);spec.loader.exec_module(review)


def stamp(seconds=0):
    return (datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat()


def state(username=None, age=0):
    games,reports=review.fixtures()
    sync=dict(username=username,status='completed' if username else 'idle',revision=1 if username else 0,
              background_requested=False,requested_at=stamp(-age) if username else None,
              last_synced_at=stamp(-age) if username else None,next_sync_at=stamp(30-age) if username else None,
              added_games=0,checked_games=2,error=None)
    return dict(games=games,reports=reports,sync=sync,posts=[],reads=[])


def setup(browser, data, width=390, clock=False):
    context=browser.new_context(viewport={'width':width,'height':844},is_mobile=width<700,has_touch=True)
    page=context.new_page(); errors=[]
    if clock:
        page.clock.install()
    page.on('pageerror',lambda error:errors.append(str(error)))
    def respond(route):
        req=route.request;path=urlparse(req.url).path.removeprefix('/api')
        if req.method=='POST':
            body=req.post_data_json;data['posts'].append((path,body))
            if path=='/game-analysis/sync':
                data['sync'].update(username=body['username'].strip().lower(),revision=data['sync']['revision']+1,
                    status='ready',background_requested=True,requested_at=stamp(),next_sync_at=stamp(30),error=None,added_games=0)
                route.fulfill(json=data['sync']);return
            if path=='/imports/chesscom':
                username=body['username'].strip().lower()
                data['sync'].update(username=username,revision=data['sync']['revision']+1,status='idle',background_requested=False,requested_at=None,next_sync_at=None,last_synced_at=None)
                route.fulfill(json={'username':username});return
            raise AssertionError(path)
        if path=='/auth/me':result={'username':'tester'}
        elif path=='/training/current':result=dict(attempt=None,solution=[],game_url='')
        elif path=='/meta':result=dict(total_puzzles=1,themes=[],min_rating=800,max_rating=1800,collections=[dict(id='lichess',name='Lichess',count=1)])
        elif path=='/stats':result=dict(solved=0,total_attempts=0,accuracy=0,streak=0,due_count=0,average_solution_ms=None,timed_solutions=0,by_theme=[],recent=[])
        elif path=='/game-analysis/games':
            result=data['reads'].pop(0) if data['reads'] else dict(games=data['games'],sync=data['sync'])
        elif path=='/game-analysis/11':result=data['reports'][0]
        else:raise AssertionError(path)
        route.fulfill(json=result)
    page.route('**/api/**',respond)
    page.goto(review.BASE+'/analysis')
    return context,page,errors


def check_saved_and_completed(browser):
    data=state();context,page,errors=setup(browser,data)
    panel=page.get_by_role('region',name='Синхронизация Chess.com')
    expect(page.get_by_role('region',name='Синхронизация Chess.com').get_by_label('Ник на Chess.com',exact=True)).to_have_value('')
    page.get_by_role('region',name='Синхронизация Chess.com').get_by_label('Ник на Chess.com',exact=True).fill('MyChessName')
    page.get_by_role('button',name='Синхронизировать',exact=True).click()
    expect(panel.get_by_role('status')).to_contain_text('в очереди')
    expect(page.get_by_role('region',name='Синхронизация Chess.com').get_by_label('Ник на Chess.com',exact=True)).to_have_value('mychessname')
    assert data['posts']==[('/game-analysis/sync',{'username':'MyChessName','automatic':False})]
    page.reload()
    expect(page.get_by_role('region',name='Синхронизация Chess.com').get_by_label('Ник на Chess.com',exact=True)).to_have_value('mychessname')
    expect(panel.get_by_role('status')).to_contain_text('в очереди')
    assert len(data['posts'])==1
    page.goto(review.BASE+'/')
    data['games'].append({**data['games'][0],'id':3,'white':'NewOpponent','analysis_id':None,'analysis_status':None})
    data['sync'].update(status='completed',background_requested=False,added_games=1,last_synced_at=stamp())
    page.goto(review.BASE+'/analysis')
    expect(panel.get_by_role('status')).to_contain_text('Добавлено новых партий: 1')
    expect(page.get_by_role('link',name='Открыть партию NewOpponent — Opponent1')).to_be_visible()
    expect(page.get_by_role('link',name='Открыть партию Player1 — Opponent1')).to_be_visible()
    context2,page2,errors2=setup(browser,data,1280)
    expect(page2.get_by_role('region',name='Синхронизация Chess.com').get_by_label('Ник на Chess.com',exact=True)).to_have_value('mychessname')
    assert len(data['posts'])==1
    page.get_by_role('link',name='Открыть партию Player1 — Opponent1',exact=True).click()
    expect(page.get_by_role('region',name='Интерактивный разбор партии')).to_be_visible()
    assert urlparse(page.url).path=='/analysis/1/review'
    assert not errors and not errors2,(errors,errors2)
    context2.close();context.close()
    print('PASS sync: remembered nickname, reload, second device, completion while away, new games, preserved report and URL')


def check_auto_edit_and_error(browser):
    data=state('remembered',age=360);context,page,errors=setup(browser,data)
    panel=page.get_by_role('region',name='Синхронизация Chess.com')
    expect(panel.get_by_role('status')).to_contain_text('в очереди')
    assert data['posts']==[('/game-analysis/sync',{'username':'remembered','automatic':True})]
    old=deepcopy(dict(games=data['games'],sync=data['sync']))
    page.get_by_role('region',name='Синхронизация Chess.com').get_by_label('Ник на Chess.com',exact=True).fill('new-name')
    review.refresh(page)
    expect(page.get_by_role('region',name='Синхронизация Chess.com').get_by_label('Ник на Chess.com',exact=True)).to_have_value('new-name')
    page.get_by_role('button',name='Синхронизировать',exact=True).click()
    expect(panel.get_by_role('status')).to_contain_text('new-name')
    data['reads'].append(old);review.refresh(page)
    expect(page.get_by_role('region',name='Синхронизация Chess.com').get_by_label('Ник на Chess.com',exact=True)).to_have_value('new-name')
    data['sync'].update(status='failed',background_requested=False,error='Chess.com временно недоступен.')
    review.refresh(page)
    expect(panel.get_by_role('alert')).to_contain_text('Chess.com временно недоступен')
    expect(page.get_by_role('link',name='Открыть партию Player1 — Opponent1')).to_be_visible()
    page.reload()
    expect(page.get_by_role('region',name='Синхронизация Chess.com').get_by_label('Ник на Chess.com',exact=True)).to_have_value('new-name')
    assert len(data['posts'])==2
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=artifact_path('analysis-sync-mobile.png'),full_page=True)
    assert not errors,errors
    context.close()
    print('PASS sync: automatic check, edit survives polling, stale response ignored, failed sync retains nickname and games')


def check_refresh_and_history(browser):
    data=state('remembered',age=120);context,page,errors=setup(browser,data,1280)
    expect(page.get_by_role('region',name='Синхронизация Chess.com').get_by_label('Ник на Chess.com',exact=True)).to_have_value('remembered')
    assert not data['posts']
    page.get_by_role('button',name='Обновить партии для анализа',exact=True).click()
    expect(page.get_by_role('region',name='Синхронизация Chess.com').get_by_role('status')).to_contain_text('в очереди')
    assert data['posts'][-1]==('/game-analysis/sync',{'username':'remembered','automatic':False})
    page.get_by_role('region',name='Синхронизация Chess.com').get_by_label('Ник на Chess.com',exact=True).fill('HistoricUser')
    page.get_by_text('Загрузить архив за другой месяц',exact=True).click()
    page.locator('input[type="month"]:visible').fill('2025-01')
    page.get_by_role('button',name='Загрузить партии',exact=True).click()
    expect(page.get_by_role('region',name='Синхронизация Chess.com').get_by_label('Ник на Chess.com',exact=True)).to_have_value('historicuser')
    assert ('/imports/chesscom',{'username':'HistoricUser','month':'2025-01','limit':10}) in data['posts']
    page.reload()
    expect(page.get_by_role('region',name='Синхронизация Chess.com').get_by_label('Ник на Chess.com',exact=True)).to_have_value('historicuser')
    assert not errors,errors
    context.close()
    print('PASS sync: refresh contacts Chess.com, historical import remains available and remembers its nickname')


def check_periodic(browser):
    data=state('remembered',age=120);context,page,errors=setup(browser,data,clock=True)
    panel=page.get_by_role('region',name='Синхронизация Chess.com')
    expect(panel.get_by_label('Ник на Chess.com',exact=True)).to_have_value('remembered')
    page.clock.fast_forward(300001)
    expect(panel.get_by_role('status')).to_contain_text('в очереди')
    assert data['posts']==[('/game-analysis/sync',{'username':'remembered','automatic':True})]
    page.clock.fast_forward(300001)
    assert len(data['posts'])==1, 'Pending work must not be queued twice'
    page.get_by_role('link',name='Открыть партию Player1 — Opponent1',exact=True).click()
    expect(page.get_by_role('region',name='Интерактивный разбор партии')).to_be_visible()
    data['sync'].update(status='completed',background_requested=False)
    page.clock.fast_forward(300001)
    assert len(data['posts'])==1, 'Automatic requests belong to the visible game list'
    assert not errors,errors
    context.close()
    print('PASS sync: five-minute check, no duplicate pending request, no timer outside game list')


if __name__=='__main__':
    with sync_playwright() as p:
        browser=p.chromium.launch(**chromium_options(),args=['--no-sandbox','--disable-features=LocalNetworkAccessChecks'])
        check_saved_and_completed(browser);check_auto_edit_and_error(browser);check_refresh_and_history(browser);check_periodic(browser)
        browser.close()
