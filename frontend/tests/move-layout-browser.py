"""Regression: comments must not move the board; navigation stays at viewport bottom.
Run with Vite on 5174 and Python Playwright / Chromium installed.
"""
import importlib.util
from pathlib import Path
from browser_support import chromium_options, artifact_path
from playwright.sync_api import sync_playwright, expect


def module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


review = module('analysis-browser')
training = module('sound-browser')
LONG = 'Сначала оцените безопасность короля, затем найдите форсированный вариант и проверьте ответ соперника. ' * 35


def frame(page):
    page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')


def board_rect(page, selector):
    frame(page)
    return page.locator(selector).evaluate('(el) => {const r=el.getBoundingClientRect(); return [r.x, r.y + scrollY, r.width, r.height];}')


def same_board(page, selector, expected):
    actual = board_rect(page, selector)
    assert all(abs(a-b) < 1 for a,b in zip(actual, expected)), (actual, expected)


def docked(page):
    dock = page.locator('.move-navigation-dock')
    expect(dock).to_be_visible()
    box = dock.bounding_box()
    assert abs(box['y'] + box['height'] - page.viewport_size['height']) < 1, box
    for button in dock.get_by_role('button').all():
        rect = button.bounding_box()
        assert rect['x'] >= 0 and rect['x'] + rect['width'] <= page.viewport_size['width']
        assert rect['height'] >= 44
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


def check_review(browser, width):
    context,page,errors,posts,games,reports,partial = review.setup(browser,width)
    reports[0]['moves'][0]['reason'] = LONG
    reports[0]['moves'][1]['reason'] = 'Короткий комментарий.'
    page.get_by_role('link',name='Открыть партию Player1 — Opponent1',exact=True).click()
    board = '.analysis-position-board'
    expected = board_rect(page, board)
    page.evaluate('window.scrollTo(0, 0)')
    docked(page)
    page.get_by_role('button',name='Следующий ход',exact=True).click()
    same_board(page, board, expected)
    assert page.evaluate('scrollY') == 0
    comment = page.get_by_label('Комментарий к ходу',exact=True)
    assert comment.evaluate('el => el.scrollHeight > el.clientHeight')
    comment.evaluate('el => el.scrollTop = el.scrollHeight')
    assert comment.evaluate('el => el.scrollTop') > 0
    page.get_by_role('button',name='Следующий ход',exact=True).click()
    same_board(page, board, expected)
    assert comment.evaluate('el => el.scrollTop') == 0
    for name in ['До хода','Лучший вариант','Назад по варианту','Далее по варианту','Сыграно','Последний рассчитанный ход','Начальная позиция']:
        page.get_by_role('button',name=name,exact=True).click()
        same_board(page, board, expected)
        docked(page)
    page.evaluate('window.scrollTo(0, document.documentElement.scrollHeight)')
    frame(page); docked(page)
    summary = page.locator('.analysis-summary summary').bounding_box()
    assert summary['y'] + summary['height'] < page.locator('.move-navigation-dock').bounding_box()['y']
    page.set_viewport_size({'width':width,'height':620})
    frame(page); docked(page)
    page.set_viewport_size({'width':width,'height':844})
    page.evaluate('window.scrollTo(0, 0)')
    page.screenshot(path=artifact_path(f'fixed-review-{width}.png'))
    page.get_by_role('button',name='К партиям',exact=True).click()
    expect(page.locator('.move-navigation-dock')).to_have_count(0)
    assert not errors, errors
    context.close()
    print(f'PASS stable review {width}px: long/short comments, inner scrolling, modes, fixed controls after scrolling/resizing, no overlap, clean exit')


def check_training(browser):
    context,page,errors = training.setup(browser,feedback=LONG)
    page.get_by_role('button',name='Начать тренировку',exact=True).tap()
    board = '.chessboard'
    expected = board_rect(page,board)
    expect(page.locator('.move-navigation-dock')).to_have_count(0)
    training.move(page,'d2','d4')
    same_board(page,board,expected)
    comment = page.get_by_label('Комментарий к позиции',exact=True)
    assert comment.evaluate('el => el.scrollHeight > el.clientHeight')
    training.move(page,'e2','e4')
    same_board(page,board,expected)
    page.get_by_role('button',name='Решение',exact=True).tap()
    same_board(page,board,expected); docked(page)
    page.evaluate('window.scrollTo(0, 0)')
    for name in ['Далее по решению','В конец решения','Назад по решению','В начало']:
        page.get_by_role('button',name=name,exact=True).tap()
        same_board(page,board,expected); docked(page)
        assert page.evaluate('scrollY') == 0
    page.evaluate('window.scrollTo(0, document.documentElement.scrollHeight)')
    frame(page); docked(page)
    last_move = page.locator('.solution-moves button').last.bounding_box()
    assert last_move['y'] + last_move['height'] < page.locator('.move-navigation-dock').bounding_box()['y']
    page.set_viewport_size({'width':800,'height':360})
    frame(page); docked(page)
    page.set_viewport_size({'width':360,'height':800})
    page.evaluate('window.scrollTo(0, 0)')
    page.screenshot(path=artifact_path('fixed-training-360.png'))
    page.get_by_role('button',name='К настройкам',exact=True).tap()
    expect(page.locator('.move-navigation-dock')).to_have_count(0)
    assert not errors, errors
    context.close()
    print('PASS stable training: wrong/correct/reveal, long feedback scrolling, replay controls, landscape, no overlap, clean exit')


if __name__ == '__main__':
    with sync_playwright() as p:
        browser = p.chromium.launch(**chromium_options(),args=['--no-sandbox','--disable-features=LocalNetworkAccessChecks'])
        for width in (360,390,1280): check_review(browser,width)
        check_training(browser)
        browser.close()
