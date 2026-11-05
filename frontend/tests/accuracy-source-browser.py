"""Verify source accuracy stays separate from local review results on the real UI."""
import importlib.util
from pathlib import Path

from browser_support import chromium_options, artifact_path
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
    page.locator('.analysis-summary').screenshot(path=artifact_path(f'accuracy-source-{width}.png'))
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
    expect(page.locator('.analysis-method-note')).not_to_contain_text('Приближение проверено')
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


def check_calibrated(browser, width, reference_side):
    context, page, errors, posts, games, reports, partial = review.setup(browser, width)
    reference_scores = {'white': 68.87, 'black': 73.9}
    reports[0]['game']['chesscom_accuracies'] = {reference_side: reference_scores[reference_side]}
    reports[0]['summary']['white']['estimated_chesscom_accuracy'] = 70.2
    reports[0]['summary']['black']['estimated_chesscom_accuracy'] = 100
    open_report(page)
    white, black = [page.locator(f'.analysis-accuracy-card.{side}') for side in ('white', 'black')]
    expect(white.locator('.analysis-local-accuracy > strong')).to_have_text('≈70.2%')
    expect(black.locator('.analysis-local-accuracy > strong')).to_have_text('≈100.0%')
    for side, card in [('white', white), ('black', black)]:
        expect(card.locator('.analysis-local-accuracy')).to_contain_text('Приближение к Chess.com')
        if side == reference_side:
            expect(card.locator('.analysis-reference-accuracy > strong')).to_have_text(f'{reference_scores[side]:.2f}%')
            expect(card.locator('.analysis-local-accuracy')).to_have_class('analysis-accuracy-value analysis-local-accuracy comparison')
        else:
            expect(card.locator('.analysis-reference-accuracy')).to_have_count(0)
            expect(card.locator('.analysis-local-accuracy')).to_have_class('analysis-accuracy-value analysis-local-accuracy')
    expect(page.locator('.analysis-method-note')).to_contain_text('Приближение проверено для обоих игроков на партиях с контролем 5+5')
    expect(page.locator('.analysis-accuracy-cards')).not_to_contain_text('90.0%')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.locator('.analysis-summary').screenshot(path=artifact_path(f'accuracy-calibrated-{width}.png'))
    assert not errors and not posts, (errors, posts)
    context.close()
    print(f'PASS {width}px: both players calibrated, independent {reference_side} reference, raw score not duplicated')


def check_one_side_incomplete(browser, incomplete_side):
    context, page, errors, posts, games, reports, partial = review.setup(browser)
    reports[0]['game']['chesscom_accuracies'] = {'white': 68.87, 'black': 73.9}
    for side in ('white', 'black'):
        reports[0]['summary'][side]['estimated_chesscom_accuracy'] = 70.2
    reports[0]['summary'][incomplete_side].update(accuracy=None, provisional_accuracy=72.4,
                                                accuracy_complete=False, coverage=50)
    complete_side = 'black' if incomplete_side == 'white' else 'white'
    open_report(page)
    incomplete = page.locator(f'.analysis-accuracy-card.{incomplete_side}')
    complete = page.locator(f'.analysis-accuracy-card.{complete_side}')
    expect(incomplete.locator('.analysis-local-accuracy > strong')).to_have_text('—')
    expect(incomplete.locator('.analysis-local-accuracy')).to_contain_text('Оценка тренажёра')
    expect(incomplete).to_contain_text('72.4% · предварительно')
    expect(complete.locator('.analysis-local-accuracy > strong')).to_have_text('≈70.2%')
    expect(complete.locator('.analysis-local-accuracy')).to_contain_text('Приближение к Chess.com')
    expect(page.locator('.analysis-approximation-sign')).to_have_count(1)
    for side, reference in [('white', '68.87%'), ('black', '73.90%')]:
        expect(page.locator(f'.analysis-accuracy-card.{side} .analysis-reference-accuracy > strong')).to_have_text(reference)
    expect(page.locator('.analysis-method-note')).to_contain_text('Приближение проверено для обоих игроков')
    assert not errors and not posts, (errors, posts)
    context.close()
    print(f'PASS incomplete {incomplete_side} does not hide {complete_side} calibration or either source reference')


if __name__ == '__main__':
    with sync_playwright() as p:
        browser = p.chromium.launch(**chromium_options(),
                                    args=['--no-sandbox', '--disable-features=LocalNetworkAccessChecks'])
        check_source(browser, 390)
        check_source(browser, 1280)
        check_missing_and_partial(browser)
        check_legacy(browser)
        check_calibrated(browser, 390, 'white')
        check_calibrated(browser, 1280, 'black')
        check_one_side_incomplete(browser, 'white')
        check_one_side_incomplete(browser, 'black')
        browser.close()
