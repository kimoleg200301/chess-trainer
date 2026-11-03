"""Exercise alternate lines on the actual board, including special chess moves."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from playwright.sync_api import sync_playwright, expect

spec = importlib.util.spec_from_file_location('review', Path(__file__).with_name('analysis-browser.py'))
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


def piece(page, square, name):
    locator = page.locator(f'.analysis-position-board [data-square="{square}"] img')
    if name:
        expect(locator).to_have_attribute('src', f'/pieces/{name}.svg')
    else:
        expect(locator).to_have_count(0)


def badge(page, square, category):
    locator = page.locator(f'.analysis-position-board [data-square="{square}"] .analysis-piece-badge')
    expect(locator).to_have_class(f'analysis-piece-badge analysis-category-symbol {category}')
    cell = page.locator(f'.analysis-position-board [data-square="{square}"]').bounding_box()
    box = locator.bounding_box()
    assert cell['x'] + cell['width']/2 < box['x'] < box['x'] + box['width'] <= cell['x'] + cell['width']
    assert cell['y'] <= box['y'] < box['y'] + box['height'] < cell['y'] + cell['height']/2
    assert page.locator('.analysis-piece-badge').count() == 1


def swipe_forward(page):
    board = page.locator('.review-board-with-evaluation')
    board.dispatch_event('touchstart', {'touches': [{'identifier': 0, 'clientX': 200, 'clientY': 300}]})
    board.dispatch_event('touchend', {'changedTouches': [{'identifier': 0, 'clientX': 100, 'clientY': 300}]})


def check_line(browser, width):
    context,page,errors,posts,games,reports,partial = review.setup(browser,width)
    selected = reports[0]['moves'][1]
    selected.update(best_uci='e7e5',best_line_uci=['e7e5','g1f3','b8c6','f1b5'],best_line_san=['stale SAN'])
    page.get_by_role('button',name='Открыть партию Player1 — Opponent1',exact=True).click()
    page.locator('.review-move-list [data-ply="1"]').click()
    piece(page,'d5','bP');badge(page,'d5','mistake')
    board_before = page.locator('.analysis-position-board').evaluate('(el)=>{const r=el.getBoundingClientRect();return [r.x,r.y+scrollY,r.width,r.height]}')
    page.get_by_role('button',name='Лучший вариант',exact=True).click()
    piece(page,'e5','bP');piece(page,'d5',None);badge(page,'e5','best')
    expect(page.locator('.review-position-caption')).to_contain_text('1… e5 · 1/4')
    expect(page.locator('.review-feedback strong')).to_have_text('Лучший вариант')
    expect(page.locator('.review-move-list [data-ply="1"]')).to_have_attribute('aria-pressed','true')
    page.get_by_role('button',name='Следующий ход варианта',exact=True).click()
    piece(page,'f3','wN');expect(page.locator('.analysis-piece-badge')).to_have_count(0)
    page.keyboard.press('ArrowRight');piece(page,'c6','bN')
    page.keyboard.press('ArrowLeft');piece(page,'c6',None)
    swipe_forward(page);piece(page,'c6','bN')
    page.locator('.analysis-variation').get_by_role('button',name='1… e5',exact=True).click()
    piece(page,'g1','wN');piece(page,'b8','bN');badge(page,'e5','best')
    page.get_by_role('button',name='Перевернуть доску',exact=True).click()
    badge(page,'e5','best')
    page.keyboard.press('Home')
    piece(page,'e7','bP');expect(page.locator('.analysis-piece-badge')).to_have_count(0)
    expect(page.get_by_role('button',name='Предыдущий ход варианта',exact=True)).to_be_disabled()
    page.get_by_role('button',name='Далее по варианту',exact=True).click();piece(page,'e5','bP')
    page.get_by_role('button',name='Конец варианта',exact=True).click();piece(page,'b5','wB')
    expect(page.get_by_role('button',name='Следующий ход варианта',exact=True)).to_be_disabled()
    page.get_by_role('button',name='Воспроизвести вариант',exact=True).click()
    expect(page.locator('.review-position-caption')).to_contain_text('2… Nc6 · 3/4',timeout=6000)
    expect(page.locator('.review-position-caption')).to_contain_text('3. Bb5 · 4/4',timeout=3000)
    expect(page.get_by_role('button',name='Воспроизвести вариант',exact=True)).to_be_visible()
    expect(page.get_by_role('button',name='Лучший вариант',exact=True)).to_have_attribute('aria-pressed','true')
    expect(page.locator('.review-move-list [data-ply="1"]')).to_have_attribute('aria-pressed','true')
    page.get_by_role('button',name='Сыграно',exact=True).click()
    piece(page,'d5','bP');piece(page,'e7','bP');piece(page,'f1','wB');badge(page,'d5','mistake')
    board_after = page.locator('.analysis-position-board').evaluate('(el)=>{const r=el.getBoundingClientRect();return [r.x,r.y+scrollY,r.width,r.height]}')
    assert all(abs(a-b)<1 for a,b in zip(board_before,board_after))
    page.get_by_role('button',name='Лучший вариант',exact=True).click()
    page.locator('.review-move-list [data-ply="0"]').click()
    piece(page,'e4','wP');piece(page,'e7','bP');badge(page,'e4','best')
    expect(page.get_by_role('button',name='Сыграно',exact=True)).to_have_attribute('aria-pressed','true')
    page.get_by_role('button',name='До хода',exact=True).click()
    expect(page.locator('.analysis-piece-badge')).to_have_count(0)
    page.get_by_role('button',name='Сыграно',exact=True).click()
    page.evaluate('window.scrollTo(0,0)')
    page.screenshot(path=f'/workspace/scratch/analysis-badges-{width}.png',full_page=True)
    assert not errors and not posts,(errors,posts)
    context.close()
    print(f'PASS {width}px: alternate line from before position, first move, dock/keyboard/swipe/autoplay, original selection, badge geometry in both orientations, fixed board')


SPECIAL = json.loads(subprocess.check_output(['node','--input-type=module','-e','''
import {Chess} from 'chess.js';
const cases=[
 ['castle','r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1','e1c1',['e1g1','e8c8']],
 ['promotion','7k/P7/8/8/8/8/8/7K w - - 0 1','a7a8n',['a7a8q','h8h7']],
 ['en-passant','7k/8/8/3pP3/8/8/8/7K w - d6 0 2','e5e6',['e5d6','h8h7']]
];
console.log(JSON.stringify(cases.map(([name,fen,played,pv])=>{
 const b=new Chess(fen),number=b.moveNumber(),move=b.move(played);
 return {name,before_fen:fen,after_fen:b.fen(),move_number:number,played_san:move.san,played_uci:played,best_uci:pv[0],best_line_uci:pv,best_line_san:['stale']};
})));'''],cwd=review.FRONTEND,text=True))


def check_special(browser):
    for case in SPECIAL:
        context,page,errors,posts,games,reports,partial=review.setup(browser)
        reports[0]['moves'][0].update({key:value for key,value in case.items() if key!='name'})
        reports[0].update(moves=reports[0]['moves'][:1],cursor=1,total_plies=1)
        reports[0]['game']['initial_fen']=case['before_fen']
        page.get_by_role('button',name='Открыть партию Player1 — Opponent1',exact=True).click()
        page.locator('.review-move-list [data-ply="0"]').click()
        page.get_by_role('button',name='Лучший вариант',exact=True).click()
        if case['name']=='castle':
            piece(page,'g1','wK');piece(page,'f1','wR');piece(page,'h1',None);badge(page,'g1','best')
        elif case['name']=='promotion':
            piece(page,'a8','wQ');piece(page,'a7',None);badge(page,'a8','best')
            page.get_by_role('button',name='Перевернуть доску',exact=True).click();badge(page,'a8','best')
        else:
            piece(page,'d6','wP');piece(page,'d5',None);piece(page,'e5',None);badge(page,'d6','best')
        page.get_by_role('button',name='Следующий ход варианта',exact=True).click()
        if case['name']=='castle': piece(page,'c8','bK');piece(page,'d8','bR')
        else: piece(page,'h7','bK')
        expect(page.get_by_role('button',name='Следующий ход варианта',exact=True)).to_be_disabled()
        page.get_by_role('button',name='Сыграно',exact=True).click()
        target=case['played_uci'][2:4]
        piece(page,target,'wK' if case['name']=='castle' else 'wN' if case['name']=='promotion' else 'wP')
        assert not errors,errors
        context.close()
        print(f"PASS legal variation: {case['name']}, return to actual move")


def check_missing_and_invalid(browser):
    context,page,errors,posts,games,reports,partial=review.setup(browser)
    reports[0]['moves'][0]['best_line_uci']=['e2e4','invalid','e7e5']
    reports[0]['moves'][1]['best_line_uci']=['e7e4']
    reports[0]['moves'][2]['best_line_uci']=[]
    page.get_by_role('button',name='Открыть партию Player1 — Opponent1',exact=True).click()
    expect(page.get_by_role('button',name='Лучший вариант',exact=True)).to_be_disabled()
    page.locator('.review-move-list [data-ply="0"]').click()
    page.get_by_role('button',name='Лучший вариант',exact=True).click()
    piece(page,'e4','wP')
    expect(page.get_by_role('button',name='Следующий ход варианта',exact=True)).to_be_disabled()
    expect(page.locator('.analysis-variation > div button')).to_have_count(1)
    for ply in [1,2]:
        page.locator(f'.review-move-list [data-ply="{ply}"]').click()
        expect(page.get_by_role('button',name='Лучший вариант',exact=True)).to_be_disabled()
    assert not errors,errors
    context.close()
    print('PASS missing/invalid variation: disabled without legal first move, bounded legal prefix, no phantom steps')


if __name__=='__main__':
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE','/usr/bin/chromium'),args=['--no-sandbox','--disable-features=LocalNetworkAccessChecks'])
        check_line(browser,390);check_line(browser,1280);check_special(browser);check_missing_and_invalid(browser)
        browser.close()
