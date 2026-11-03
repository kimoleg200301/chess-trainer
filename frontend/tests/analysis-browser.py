"""Run against Vite on port 5174. Requires Python Playwright and Chromium."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect

BASE = os.environ.get('TEST_FRONTEND_URL', 'http://localhost:5174')
FRONTEND = Path(__file__).resolve().parents[1]
# Legal fixture positions; evaluations below are deliberately chosen to catch
# mover-perspective/white-perspective sign mistakes independently of the engine.
POSITIONS = json.loads(subprocess.check_output(['node', '--input-type=module', '-e', '''
import {Chess} from 'chess.js';
console.log(JSON.stringify([['e4','d5','exd5'],['f3','e5','g4','Qh4#']].map(sans => {
 const chess = new Chess();
 return sans.map(san => { const before=chess.fen(), side=chess.turn(), number=Number(before.split(' ')[5]);
 const move=chess.move(san); return {before_fen:before,after_fen:chess.fen(),played_san:san,played_uci:move.from+move.to,
 side:side==='w'?'white':'black',move_number:number}; });
})));'''], cwd=FRONTEND, text=True))


def fixtures():
    games = [dict(id=i+1, white=f'Player{i+1}', black=f'Opponent{i+1}', result='1-0' if i == 0 else '0-1', date='2026.11.03', game_url='', source_game_at=None, username=f'Player{i+1}', total_plies=len(moves), analysis_id=11 if i == 0 else None, analysis_status='completed' if i == 0 else None, analysis_outdated=False, background_requested=False) for i, moves in enumerate(POSITIONS)]
    reports = []
    for index, positions in enumerate(POSITIONS):
        moves = []
        for ply, position in enumerate(positions):
            moves.append(dict(position, ply=ply, best_uci=position['played_uci'], best_san=position['played_san'], category='mistake' if ply == 1 else 'best', accuracy=72 if ply == 1 else 100, expected_loss=0, best_expected=.5, played_expected=.5, score_before=35 if ply == 0 else -40, score_after=40 if ply == 0 else -180 if ply == 1 else 150, mate_before=None, mate_after=None, best_line_uci=[position['played_uci']], best_line_san=[position['played_san']], depth=18, assessed=True, reason='Тестовое пояснение хода.', brilliant_evidence=None))
        if index == 1:
            moves[-1].update(score_after=None, mate_after=1)
        side=dict(accuracy=90,provisional_accuracy=90,counts={},total_moves=2,analysed_moves=2,assessed_moves=2,accuracy_moves=2,coverage=100,complete=True,accuracy_complete=True)
        reports.append(dict(id=11+index,game_id=index+1,game={**games[index],'initial_fen':positions[0]['before_fen']},status='completed',background_requested=False,cursor=len(moves),total_plies=len(moves),analysis_version=2,analysis_outdated=False,error=None,created_at='',updated_at='',summary={'white':deepcopy(side),'black':deepcopy(side),'complete':True},moves=moves))
    return games, reports


def setup(browser, width=390):
    context=browser.new_context(viewport={'width':width,'height':844},is_mobile=width<700,has_touch=True)
    page=context.new_page(); errors=[]; posts=[]
    page.on('pageerror', lambda error: errors.append(str(error)))
    games,reports=fixtures(); partial=deepcopy(reports[1]); partial.update(status='ready',cursor=0,moves=[],background_requested=False)
    def respond(route):
        request=route.request; path=urlparse(request.url).path.removeprefix('/api')
        if request.method=='POST':
            posts.append((path,request.post_data_json))
            if path=='/game-analysis':
                partial.update(background_requested=True,status='running')
                games[1].update(analysis_id=12,analysis_status='running',background_requested=True)
                return route.fulfill(json=partial)
            if path in ('/game-analysis/12/pause','/game-analysis/12/resume'):
                partial['background_requested']=path.endswith('/resume'); games[1]['background_requested']=partial['background_requested']
                return route.fulfill(json=partial)
            if path=='/game-analysis/11/resume':
                reports[0].update(status='running',background_requested=True)
                return route.fulfill(json=reports[0])
            if path=='/game-analysis/11/restart':
                reports[0].update(status='ready',background_requested=True,cursor=0,moves=[],analysis_version=2,analysis_outdated=False)
                games[0].update(analysis_status='ready',background_requested=True)
                return route.fulfill(json=reports[0])
            raise AssertionError(path)
        if path=='/auth/me': data={'username':'tester'}
        elif path=='/training/current': data=dict(attempt=None,solution=[],game_url='')
        elif path=='/meta': data=dict(total_puzzles=1,themes=[],min_rating=800,max_rating=1800,collections=[dict(id='lichess',name='Lichess',count=1)])
        elif path=='/stats': data=dict(solved=0,total_attempts=0,accuracy=0,streak=0,due_count=0,average_solution_ms=None,timed_solutions=0,by_theme=[],recent=[])
        elif path=='/game-analysis/games': data={'games':games}
        elif path=='/game-analysis/11': data=reports[0]
        elif path=='/game-analysis/12': data=partial
        else: raise AssertionError(path)
        route.fulfill(json=data)
    page.route('**/api/**',respond)
    page.goto(BASE)
    page.get_by_role('button',name='Анализ',exact=True).click()
    return context,page,errors,posts,games,reports,partial


def refresh(page):
    page.evaluate("window.dispatchEvent(new Event('online'))")


def check_review(browser,width):
    context,page,errors,posts,games,reports,partial=setup(browser,width)
    expect(page.get_by_role('heading',name='Анализ партий',exact=True)).to_be_visible()
    expect(page.locator('.analysis-position-board')).to_have_count(0)
    page.get_by_role('button',name='Открыть партию Player1 — Opponent1',exact=True).click()
    expect(page.get_by_role('region',name='Интерактивный разбор партии')).to_be_visible()
    expect(page.get_by_role('navigation',name='Главная навигация')).to_have_count(0)
    expect(page.locator('.analysis-game-options')).to_have_count(0)
    expect(page.locator('.review-position-caption')).to_contain_text('Начальная позиция')
    expect(page.get_by_role('button',name='Предыдущий ход',exact=True)).to_be_disabled()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    board=page.locator('.analysis-position-board').bounding_box(); bar=page.locator('.evaluation-bar').bounding_box()
    assert bar['x'] > board['x']+board['width'] and abs(bar['height']-board['height']) < 2
    page.get_by_role('button',name='Следующий ход',exact=True).click()
    expect(page.locator('.review-position-caption')).to_contain_text('После 1. e4')
    expect(page.locator('.analysis-position-board [data-square="e4"] img')).to_have_attribute('src','/pieces/wP.svg')
    page.keyboard.press('ArrowRight')
    expect(page.locator('.evaluation-bar')).to_have_attribute('aria-label','Оценка позиции: Преимущество белых: 1.80 пешки')
    white_height=page.locator('.evaluation-white').bounding_box()['height']
    assert white_height > bar['height']/2
    page.get_by_role('button',name='Перевернуть доску',exact=True).click()
    expect(page.locator('.evaluation-bar')).to_have_class('evaluation-bar reversed ')
    expect(page.locator('.analysis-position-board > div').first).to_have_attribute('data-square','h1')
    expect(page.locator('.evaluation-bar')).to_have_attribute('aria-label','Оценка позиции: Преимущество белых: 1.80 пешки')
    page.get_by_role('button',name='Лучший вариант',exact=True).click()
    expect(page.locator('.review-position-caption')).to_contain_text('Продолжение движка')
    expect(page.get_by_role('button',name='Далее по варианту',exact=True)).to_be_disabled()
    expect(page.locator('.evaluation-bar')).to_have_attribute('aria-label','Оценка позиции: Для этой позиции оценка не рассчитана')
    page.get_by_role('button',name='Сыграно',exact=True).click()
    page.get_by_role('button',name='Последний рассчитанный ход',exact=True).click()
    expect(page.get_by_role('button',name='Следующий ход',exact=True)).to_be_disabled()
    page.get_by_role('button',name='Начальная позиция',exact=True).click()
    page.get_by_role('button',name='Воспроизвести партию',exact=True).click()
    expect(page.locator('.review-position-caption')).to_contain_text('После 2. exd5',timeout=7000)
    expect(page.get_by_role('button',name='Воспроизвести партию',exact=True)).to_be_visible()
    page.get_by_role('button',name='Начальная позиция',exact=True).click()
    page.locator('.review-board-with-evaluation').dispatch_event('touchstart',{'touches':[{'identifier':0,'clientX':200,'clientY':300}]})
    page.locator('.review-board-with-evaluation').dispatch_event('touchend',{'changedTouches':[{'identifier':0,'clientX':100,'clientY':305}]})
    expect(page.locator('.review-position-caption')).to_contain_text('После 1. e4')
    page.get_by_role('button',name='Ход 1 чёрных: d5, Ошибка',exact=True).click()
    expect(page.locator('.review-position-caption')).to_contain_text('После 1… d5')
    refresh(page)
    expect(page.locator('.review-position-caption')).to_contain_text('После 1… d5')
    page.screenshot(path=f'/workspace/scratch/analysis-review-{width}.png',full_page=True)
    page.get_by_role('button',name='К партиям',exact=True).click()
    expect(page.get_by_role('navigation',name='Главная навигация')).to_be_visible()
    assert not posts and not errors, (posts,errors)
    context.close()
    print(f'PASS review {width}px: isolated screen, board, right-side evaluation, correct black-move sign, flip, missing evaluation, navigation, autoplay, swipe, list')


def check_progress(browser):
    context,page,errors,posts,games,reports,partial=setup(browser)
    page.get_by_role('button',name='Открыть партию Player2 — Opponent2',exact=True).click()
    expect(page.get_by_role('region',name='Ход анализа')).to_be_visible()
    expect(page.locator('.analysis-position-board')).to_have_count(0)
    page.get_by_role('button',name='Анализировать партию',exact=True).click()
    expect(page.get_by_role('button',name='Пауза',exact=True)).to_be_visible()
    assert posts[-1]==('/game-analysis',{'game_id':2,'background':True})
    partial.update(cursor=1,moves=deepcopy(reports[1]['moves'][:1]));refresh(page)
    expect(page.get_by_role('progressbar')).to_have_attribute('aria-valuenow','25')
    page.screenshot(path='/workspace/scratch/analysis-progress.png',full_page=True)
    page.get_by_role('button',name='К партиям',exact=True).click()
    assert len(posts)==1 and partial['background_requested']
    partial.update(cursor=2,moves=deepcopy(reports[1]['moves'][:2]))
    page.get_by_role('button',name='Открыть партию Player2 — Opponent2',exact=True).click()
    expect(page.get_by_role('progressbar')).to_have_attribute('aria-valuenow','50')
    page.get_by_role('button',name='Пауза',exact=True).click()
    expect(page.get_by_role('button',name='Продолжить анализ',exact=True)).to_be_visible()
    page.get_by_role('button',name='Открыть разбор',exact=True).click()
    expect(page.get_by_role('region',name='Интерактивный разбор партии')).to_be_visible()
    page.get_by_role('button',name='Ход анализа · 50%',exact=True).click()
    page.get_by_role('button',name='Продолжить анализ',exact=True).click()
    expect(page.get_by_role('button',name='Пауза',exact=True)).to_be_visible()
    partial.update(cursor=4,moves=deepcopy(reports[1]['moves']),retry_pending=1);refresh(page)
    expect(page.get_by_role('progressbar')).to_have_attribute('aria-valuenow','99')
    expect(page.get_by_role('status')).to_contain_text('Уточняем оценки ходов: осталось 1')
    expect(page.get_by_role('region',name='Интерактивный разбор партии')).to_have_count(0)
    partial.update(status='completed',cursor=4,moves=deepcopy(reports[1]['moves']),background_requested=False)
    games[1].update(analysis_status='completed',background_requested=False);refresh(page)
    expect(page.get_by_role('region',name='Интерактивный разбор партии')).to_be_visible(timeout=6000)
    page.get_by_role('button',name='Последний рассчитанный ход',exact=True).click()
    expect(page.locator('.evaluation-bar')).to_have_attribute('aria-label','Оценка позиции: Мат. Победа чёрных')
    page.wait_for_function("document.querySelector('.evaluation-white').getBoundingClientRect().height < 1")
    assert not any(path.endswith('/step') for path,_ in posts)
    assert not errors, errors
    context.close()
    print('PASS progress: start, 25/50%, no pause on leaving, persisted partial report, explicit pause/resume, automatic completed review, terminal mate')


def check_old_report(browser):
    context,page,errors,posts,games,reports,partial=setup(browser)
    reports[0].update(analysis_version=1,analysis_outdated=True,status='running',background_requested=True,cursor=1)
    reports[0]['moves']=reports[0]['moves'][:1]
    games[0].update(analysis_outdated=True,analysis_status='running',background_requested=True)
    refresh(page)
    page.get_by_role('button',name='Открыть партию Player1 — Opponent1',exact=True).click()
    expect(page.get_by_role('button',name='Пересчитать партию',exact=True)).to_be_visible()
    expect(page.get_by_role('button',name='Пауза',exact=True)).to_have_count(0)
    expect(page.get_by_role('button',name='Продолжить анализ',exact=True)).to_have_count(0)
    expect(page.locator('.analysis-notice')).to_contain_text('Stockfish 19')
    assert not posts and len(reports[0]['moves'])==1
    page.get_by_role('button',name='Пересчитать партию',exact=True).click()
    expect(page.get_by_role('button',name='Пауза',exact=True)).to_be_visible()
    assert posts==[('/game-analysis/11/restart',{'background':True})]
    assert not errors,errors
    context.close()
    print('PASS old report: saved positions retained, explicit restart available, no false running state, upgraded default analysis')


def check_missing_score_resume(browser):
    context,page,errors,posts,games,reports,partial=setup(browser)
    reports[0]['retry_pending']=1
    page.get_by_role('button',name='Открыть партию Player1 — Opponent1',exact=True).click()
    expect(page.get_by_role('status')).to_contain_text('Уточняем оценки ходов: осталось 1')
    expect(page.get_by_role('button',name='Пауза',exact=True)).to_be_visible()
    assert posts==[('/game-analysis/11/resume',{})]
    assert reports[0]['cursor']==3 and len(reports[0]['moves'])==3
    assert not errors,errors
    context.close()
    print('PASS missing score: opening a current saved report resumes only pending retries')


if __name__=='__main__':
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE','/usr/bin/chromium'),args=['--no-sandbox','--disable-features=LocalNetworkAccessChecks'])
        check_review(browser,390);check_review(browser,1280);check_progress(browser);check_old_report(browser);check_missing_score_resume(browser)
        browser.close()
