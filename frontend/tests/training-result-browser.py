"""Mobile/desktop result layout, server-side restoration and first-attempt rating feedback.
Run with Vite on 5174 and Python Playwright / Chromium installed.
"""
from copy import deepcopy
import importlib.util
import os
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect

spec=importlib.util.spec_from_file_location('sound',Path(__file__).with_name('sound-browser.py'))
sound=importlib.util.module_from_spec(spec);spec.loader.exec_module(sound)
LINE=[dict(uci=uci,san=san) for uci,san in [('e2e4','e4'),('d7d5','d5'),('e4d5','exd5')]]
CHANGE=dict(before=1200,after=1216,delta=16,puzzle_before=1200,puzzle_after=1192,clean=True)


def check(browser,width):
    context=browser.new_context(viewport={'width':width,'height':844},has_touch=True,is_mobile=width<700)
    page=context.new_page();errors=[];requests=[];attempt=None;rating=1200;graded=0
    page.on('pageerror',lambda error:errors.append(str(error)))
    def saved():
        return dict(attempt=attempt,solution=LINE if attempt and attempt['status']!='active' else [],game_url='')
    def respond(route):
        nonlocal attempt,rating,graded
        path=urlparse(route.request.url).path.removeprefix('/api')
        if path=='/auth/me':data=dict(username='tester')
        elif path=='/meta':data=dict(total_puzzles=2,themes=[],min_rating=200,max_rating=3000,collections=[dict(id='lichess',name='Lichess',count=2)])
        elif path=='/stats':data=dict(solved=graded,total_attempts=len(requests),accuracy=100,streak=graded,due_count=0,average_solution_ms=4321,timed_solutions=graded,by_theme=[],recent=[],puzzle_rating=rating,rated_attempts=graded)
        elif path=='/training/current' or path.endswith('/resume'):data=saved()
        elif path=='/training/start':
            body=route.request.post_data_json;requests.append(body)
            attempt=dict(id=len(requests),puzzle_id='rated-fixture',fen=sound.INITIAL,starting_fen=sound.INITIAL,resumed=False,
                         rating=1200,themes=['opening'],status='active',turn='white',solved_plies=0,total_plies=3,
                         mistakes=0,hints_used=0,last_move=None,elapsed_ms=0,collection='lichess',rating_estimated=False,
                         rated=not body.get('puzzle_id'),rating_change=None,
                         training_options={key:value for key,value in body.items() if key not in ['puzzle_id','exclude_attempt_id']})
            data=attempt
        elif path.endswith('/move'):
            move=route.request.post_data_json['move']
            assert move in ['e2e4','e4d5']
            if move=='e2e4':attempt.update(fen=sound.REPLY,last_move='d7d5',solved_plies=2,elapsed_ms=2000)
            else:
                attempt.update(fen=sound.FINISHED,last_move=move,status='completed',solved_plies=3,elapsed_ms=4321)
                if attempt['rated']:attempt['rating_change']=CHANGE;rating=1216;graded=1
            data=dict(accepted=True,attempt=attempt,reply_move='d7d5' if move=='e2e4' else None,feedback='Верно!')
        elif path.endswith('/time'):
            if attempt['status']=='active':attempt['elapsed_ms']=max(attempt['elapsed_ms'],route.request.post_data_json['elapsed_ms'])
            data=attempt
        elif path.endswith('/reveal'):
            if attempt['status']=='active':attempt['status']='revealed'
            data=saved()
        else:raise AssertionError(path)
        route.fulfill(json=data)
    page.route('**/api/**',respond)
    page.goto(os.environ.get('TEST_FRONTEND_URL','http://localhost:5174'))
    expect(page.get_by_role('checkbox',name='Подбирать по моему рейтингу',exact=False)).to_be_checked()
    expect(page.locator('#rating-min')).to_have_count(0)
    page.get_by_role('button',name='Начать тренировку',exact=True).click()
    assert requests[-1]['adaptive'] is True
    expect(page.get_by_role('button',name='Следующая позиция',exact=True)).to_have_count(0)
    sound.move(page,'e2','e4')
    page.reload()
    expect(page.get_by_role('region',name='Решение позиции')).to_be_visible()
    expect(page.locator('[data-square="e4"] img')).to_have_attribute('src','/pieces/wP.svg')
    expect(page.locator('[data-square="d5"] img')).to_have_attribute('src','/pieces/bP.svg')
    assert len(requests)==1 and attempt['solved_plies']==2
    before=page.locator('.chessboard').evaluate('(el)=>{const r=el.getBoundingClientRect();return [r.x,r.y+scrollY,r.width,r.height]}')
    sound.move(page,'e4','d5')
    expect(page.get_by_label('Результат задачи',exact=True)).to_be_visible()
    expect(page.locator('.rating-delta')).to_have_text('+16')
    expect(page.locator('.result-rating-value strong')).to_have_text('1216')
    expect(page.get_by_role('button',name='Следующая позиция',exact=True)).to_be_visible()
    expect(page.get_by_role('button',name='Решение',exact=True)).to_have_count(0)
    after=page.locator('.chessboard').evaluate('(el)=>{const r=el.getBoundingClientRect();return [r.x,r.y+scrollY,r.width,r.height]}')
    assert all(abs(a-b)<1 for a,b in zip(before,after)),(before,after)
    expect(page.locator('.solved-marker')).to_have_count(1)
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.evaluate('window.scrollTo(0,0)')
    page.screenshot(path=f'/workspace/scratch/training-result-{width}.png',full_page=True)
    page.reload()
    expect(page.locator('.rating-delta')).to_have_text('+16')
    expect(page.locator('[data-square="d5"] img')).to_have_attribute('src','/pieces/wP.svg')
    assert len(requests)==1 and graded==1
    page.get_by_role('button',name='Разбор',exact=True).click()
    expect(page.get_by_label('Навигация по решению',exact=True)).to_be_visible()
    page.get_by_role('button',name='Далее по решению',exact=True).click()
    assert attempt['rating_change']==CHANGE and graded==1
    page.get_by_role('button',name='Повторить',exact=True).click()
    expect(page.get_by_role('button',name='Следующая позиция',exact=True)).to_have_count(0)
    assert requests[-1]['puzzle_id']=='rated-fixture' and not attempt['rated']
    sound.move(page,'e2','e4');sound.move(page,'e4','d5')
    expect(page.locator('.rating-delta')).to_have_count(0)
    expect(page.locator('.result-rating-value strong')).to_have_text('1216')
    page.get_by_role('button',name='Следующая позиция',exact=True).click()
    assert requests[-1]['adaptive'] is True and 'puzzle_id' not in requests[-1]
    assert not errors,errors
    context.close()
    print(f'PASS {width}px: adaptive default; no next button while active; partial and completed resume; stable board; rating result; review; unrated retry; next')


if __name__=='__main__':
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path='/usr/bin/chromium',args=['--no-sandbox','--disable-features=LocalNetworkAccessChecks'])
        for width in [360,390,1280]:check(browser,width)
        browser.close()
