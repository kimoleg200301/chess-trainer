"""Personal practice must ignore hidden settings from other collections.
Run with Vite on 5174 and Python Playwright / Chromium installed.
"""
import os
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect

INITIAL = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'


def check(browser, width):
    context = browser.new_context(viewport={'width':width,'height':844},has_touch=True,is_mobile=width<700)
    page = context.new_page(); errors = []; requests = []; personal_starts = 0
    attempt = {}
    page.on('pageerror',lambda error: errors.append(str(error)))

    def respond(route):
        nonlocal attempt, personal_starts
        parsed=urlparse(route.request.url);path=parsed.path.removeprefix('/api')
        if path=='/auth/me': data={'username':'tester'}
        elif path=='/training/current': data=dict(attempt=None,solution=[],game_url='')
        elif path.endswith('/reveal'):
            attempt['status']='revealed';data=dict(attempt=attempt,solution=[dict(uci='e2e4',san='e4')],game_url='')
        elif path=='/meta':
            data=dict(total_puzzles=2,themes=[dict(id='fork',name='Вилка',count=1)],min_rating=200,max_rating=3500,
                      collections=[dict(id=key,name=name,count=2) for key,name in [('lichess','Lichess'),('personal','Мои задачи'),('public','Открытые партии'),('all','Все доступные')]])
        elif path=='/stats':data=dict(solved=0,total_attempts=0,accuracy=0,streak=0,due_count=1,average_solution_ms=None,timed_solutions=0,by_theme=[],recent=[])
        elif path=='/training/start':
            body=route.request.post_data_json;requests.append(body)
            if body['collection']=='personal':
                personal_starts+=1
                if personal_starts==3:
                    return route.fulfill(status=404,json={'code':'personal_collection_complete','detail':'Старый текст с упоминанием фильтров и режима повторения.'})
            attempt=dict(id=len(requests),puzzle_id=f'personal-{len(requests)}',fen=INITIAL,starting_fen=INITIAL,resumed=False,
                         rating=3500 if len(requests)==1 else 200,themes=['pin'],status='active',turn='white',solved_plies=0,
                         total_plies=3,mistakes=0,hints_used=0,last_move=None,elapsed_ms=0,collection=body['collection'],rating_estimated=True)
            data=attempt
        elif path.endswith('/time'):data=attempt
        else:raise AssertionError(path)
        route.fulfill(json=data)

    def personal_setup():
        page.get_by_label('База задач',exact=True).select_option('personal')
        expect(page.get_by_role('button',name='Начать тренировку' if not requests else 'Начать новую тренировку',exact=True)).to_be_enabled()
        expect(page.locator('.setup-card select')).to_have_count(1)
        expect(page.locator('.setup-card input')).to_have_count(0)
        expect(page.get_by_role('group',name='Режим тренировки',exact=True)).to_have_count(0)
        expect(page.get_by_role('group',name='Уровень сложности',exact=True)).to_have_count(0)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')

    page.route('**/api/**',respond)
    page.goto(os.environ.get('TEST_FRONTEND_URL','http://localhost:5174'))
    page.get_by_label('Тактический мотив',exact=True).select_option('fork')
    page.get_by_role('checkbox',name='Подбирать по моему рейтингу',exact=False).uncheck()
    page.locator('#rating-min').fill('2100')
    page.get_by_label('Верхняя граница рейтинга',exact=True).fill('900')
    page.get_by_role('button',name='Повторение',exact=False).click()
    page.get_by_role('button',name='Начать тренировку',exact=True).click()
    expect(page.get_by_role('alert')).to_contain_text('Нижняя граница рейтинга')
    assert not requests
    personal_setup()
    expect(page.get_by_role('alert')).to_have_count(0)
    page.screenshot(path=f'/workspace/scratch/personal-setup-{width}.png',full_page=True)
    page.get_by_role('button',name='Начать тренировку',exact=True).click()
    expect(page.get_by_role('region',name='Решение позиции')).to_be_visible()
    assert requests[-1]==dict(collection='personal',theme=None,min_rating=0,max_rating=4000,mode='practice',adaptive=True)
    expect(page.locator('.puzzle-caption')).to_contain_text('3500')
    expect(page.get_by_role('button',name='Следующая позиция',exact=True)).to_have_count(0)
    page.get_by_role('button',name='Решение',exact=True).click()
    page.get_by_role('button',name='Следующая позиция',exact=True).click()
    expect(page.locator('.puzzle-caption')).to_contain_text('200')
    assert requests[-1]==dict(collection='personal',theme=None,min_rating=0,max_rating=4000,mode='practice',adaptive=True,exclude_attempt_id=1)
    page.get_by_role('button',name='К настройкам',exact=True).click()
    page.get_by_label('База задач',exact=True).select_option('lichess')
    expect(page.locator('#rating-min')).to_have_value('2100')
    expect(page.get_by_role('button',name='Повторение',exact=False)).to_have_attribute('aria-pressed','true')
    page.locator('#rating-min').fill('1000')
    page.get_by_label('Верхняя граница рейтинга',exact=True).fill('1600')
    page.get_by_label('Тактический мотив',exact=True).select_option('fork')
    page.get_by_role('button',name='Начать новую тренировку',exact=True).click()
    expect(page.get_by_role('region',name='Решение позиции')).to_be_visible()
    assert requests[-1]==dict(collection='lichess',theme='fork',min_rating=1000,max_rating=1600,mode='review',adaptive=False)
    page.get_by_role('button',name='К настройкам',exact=True).click()
    personal_setup()
    page.get_by_role('button',name='Начать новую тренировку',exact=True).click()
    expect(page.get_by_role('region',name='Тренировка завершена')).to_contain_text('Все задачи из ваших партий пройдены')
    expect(page.get_by_role('region',name='Тренировка завершена')).not_to_contain_text('Повторение')
    assert requests[-1]['mode']=='practice' and requests[-1]['theme'] is None
    assert not errors,errors
    context.close()
    print(f'PASS {width}px: no personal filters, invalid stale rating ignored, high/low tasks, next task, other collections retain filters, clear completion')


if __name__=='__main__':
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path='/usr/bin/chromium',args=['--no-sandbox','--disable-features=LocalNetworkAccessChecks'])
        check(browser,360);check(browser,1280)
        browser.close()
