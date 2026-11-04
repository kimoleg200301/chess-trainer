"""Verify source accuracy stays separate from local review results on the real UI."""
import importlib.util
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

spec = importlib.util.spec_from_file_location('review', Path(__file__).with_name('analysis-browser.py'))
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


def open_report(page):
    page.get_by_role('link', name='Открыть партию Player1 — Opponent1', exact=True).click()
    page.locator('.analysis-summary > summary').click()
    expect(page.locator('.analysis-accuracy-card.white')).to_be_visible()


def check_source(browser, width):
    context, page, errors, posts, games, reports, partial = review.setup(browser, width)
    reports[0]['game']['chesscom_accuracies'] = {'white': 58.96, 'black': 68.87}
    open_report(page)
    for side, expected in [('white', '58.96%'), ('black', '68.87%')]:
        card = page.locator(f'.analysis-accuracy-card.{side}')
        expect(card.locator('.analysis-reference-accuracy > strong')).to_have_text(expected)
        expect(card.locator('.analysis-reference-accuracy')).to_contain_text('Точность Chess.com')
        expect(card.locator('.analysis-local-accuracy > strong')).to_have_text('90.0%')
        expect(card.locator('.analysis-local-accuracy')).to_contain_text('Оценка тренажёра')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.locator('.analysis-summary').screenshot(path=f'/workspace/scratch/accuracy-source-{width}.png')
    assert not errors and not posts, (errors, posts)
    context.close()
    print(f'PASS {width}px: original two-decimal accuracy and independent local result')


def check_missing_and_partial(browser):
    context, page, errors, posts, games, reports, partial = review.setup(browser)
    reports[0]['game']['chesscom_accuracies'] = {'white': 0}
    reports[0]['summary']['white'].update(accuracy=None, provisional_accuracy=None,
                                         accuracy_moves=0, accuracy_complete=True, estimated_chesscom_accuracy=80)
    reports[0]['summary']['black'].update(accuracy=None, provisional_accuracy=72.4,
                                         accuracy_complete=False, coverage=50, estimated_chesscom_accuracy=75)
    open_report(page)
    white, black = [page.locator(f'.analysis-accuracy-card.{side}') for side in ('white', 'black')]
    expect(white.locator('.analysis-reference-accuracy > strong')).to_have_text('0.00%')
    expect(white.locator('.analysis-local-accuracy > strong')).to_have_text('—')
    expect(white).to_contain_text('Все ходы вынужденные — точность не считается.')
    expect(black.locator('.analysis-reference-accuracy')).to_have_count(0)
    expect(black).to_contain_text('Точность Chess.com недоступна.')
    expect(black.locator('.analysis-local-accuracy > strong')).to_have_text('—')
    expect(black).to_contain_text('72.4% · предварительно')
    expect(page.locator('.analysis-approximation-sign')).to_have_count(0)
    expect(page.locator('.analysis-method-note')).not_to_contain_text('Приближение настроено')
    assert not errors and not posts, (errors, posts)
    context.close()
    print('PASS zero source score, absent other side, forced-only and incomplete local result')


def check_legacy(browser):
    context, page, errors, posts, games, reports, partial = review.setup(browser)
    open_report(page)
    expect(page.locator('.analysis-reference-accuracy')).to_have_count(0)
    expect(page.locator('.analysis-reference-unavailable')).to_have_count(2)
    expect(page.locator('.analysis-local-accuracy').first).to_contain_text('90.0%')
    assert not errors and not posts, (errors, posts)
    context.close()
    print('PASS older report without reference values remains usable')


def check_calibrated(browser, width):
    context, page, errors, posts, games, reports, partial = review.setup(browser, width)
    reports[0]['game']['chesscom_accuracies'] = {'white': 68.87}
    reports[0]['summary']['white']['estimated_chesscom_accuracy'] = 70.2
    reports[0]['summary']['black']['estimated_chesscom_accuracy'] = 100
    open_report(page)
    white, black = [page.locator(f'.analysis-accuracy-card.{side}') for side in ('white', 'black')]
    expect(white.locator('.analysis-reference-accuracy > strong')).to_have_text('68.87%')
    expect(white.locator('.analysis-local-accuracy > strong')).to_have_text('≈70.2%')
    expect(white.locator('.analysis-local-accuracy')).to_contain_text('Приближение к Chess.com')
    expect(white.locator('.analysis-local-accuracy')).to_have_class('analysis-accuracy-value analysis-local-accuracy comparison')
    expect(black.locator('.analysis-reference-accuracy')).to_have_count(0)
    expect(black.locator('.analysis-local-accuracy > strong')).to_have_text('≈100.0%')
    expect(black.locator('.analysis-local-accuracy')).to_contain_text('Приближение к Chess.com')
    expect(black.locator('.analysis-local-accuracy')).to_have_class('analysis-accuracy-value analysis-local-accuracy')
    expect(page.locator('.analysis-method-note')).to_contain_text('Приближение настроено по партиям этого игрока с контролем 5+5')
    expect(page.locator('.analysis-accuracy-cards')).not_to_contain_text('90.0%')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.locator('.analysis-summary').screenshot(path=f'/workspace/scratch/accuracy-calibrated-{width}.png')
    assert not errors and not posts, (errors, posts)
    context.close()
    print(f'PASS {width}px: labelled calibration with/without reference; raw score not duplicated')


if __name__ == '__main__':
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE', '/usr/bin/chromium'),
                                    args=['--no-sandbox', '--disable-features=LocalNetworkAccessChecks'])
        check_source(browser, 390)
        check_source(browser, 1280)
        check_missing_and_partial(browser)
        check_legacy(browser)
        check_calibrated(browser, 390)
        check_calibrated(browser, 1280)
        browser.close()
