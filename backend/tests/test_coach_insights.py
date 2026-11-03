"""Coach diagnostics prove source scope, meaningful denominators and honest trends."""
from copy import deepcopy

import chess
import pytest

from app.coach_insights import SKILLS, build_insights, inspect_move


@pytest.fixture(scope="session", autouse=True)
def database():
    yield


@pytest.fixture(autouse=True)
def clean_training():
    yield


MOVES = 'e2e4 e7e5 g1f3 b8c6 f1c4 g8f6 d2d3 f8c5 e1g1 d7d6 c2c3 e8g8'.split()


def game(game_id=1, cohort='baseline', errors=(0,), result='resigned', status='completed', end_time=None):
    data = {'username': 'player', 'white': 'Player', 'black': 'Opponent', 'player_side': 'white',
            'initial_fen': chess.STARTING_FEN, 'moves': MOVES, 'player_result': result,
            'time_control': '300+5', 'end_time': game_id if end_time is None else end_time,
            'game_url': f'https://www.chess.com/game/live/{game_id}'}
    board, rows = chess.Board(), []
    for ply, uci in enumerate(MOVES):
        move = chess.Move.from_uci(uci)
        if board.turn:
            best = next(candidate for candidate in board.legal_moves if candidate != move) if ply in errors else move
            rows.append({'ply': ply, 'move_number': board.fullmove_number, 'side': 'white',
                         'before_fen': board.fen(), 'played_uci': uci, 'played_san': board.san(move),
                         'best_uci': best.uci(), 'best_san': board.san(best), 'best_line_uci': [best.uci()],
                         'assessed': True, 'category': 'mistake' if ply in errors else 'best',
                         'expected_loss': .12 if ply in errors else 0, 'score_before': 0, 'score_after': -200 if ply in errors else 0,
                         'mate_before': None, 'mate_after': None})
        board.push(move)
    return {'id': game_id, 'cohort': cohort, 'status': status, 'cursor': len(MOVES), 'data': data, 'moves': rows}


def skill(report, key):
    return next(item for item in report['skills'] if item['id'] == key)


def test_own_assessed_nonforced_moves_only_and_boundary_is_strict():
    g = game()
    row = g['moves'][0]
    result = inspect_move(g['data'], row, 'player')
    assert result['eligible'] and 'opening' in result['skill_ids']
    assert inspect_move(g['data'], row, 'opponent')['eligible'] is False
    assert inspect_move(g['data'], row, 'unknown')['eligible'] is False
    for replacement in ({'assessed': False}, {'category': 'forced'}, {'expected_loss': None}, {'expected_loss': float('nan')}):
        assert not inspect_move(g['data'], {**row, **replacement}, 'player')['eligible']
    result = inspect_move(g['data'], {**row, 'expected_loss': .03}, 'player')
    assert result['eligible'] and result['skill_ids'] == []


def test_clock_values_do_not_bridge_missing_data_and_zero_is_valid():
    g = game()
    row = g['moves'][0]
    assert inspect_move(g['data'], row)['time_pressure'] is None
    g['data'].update(clocks=[0], clock_before=[19], move_seconds=[24])
    result = inspect_move(g['data'], row)
    assert (result['clock_before'], result['clock_after'], result['spent_seconds']) == (19, 0, 24)
    assert result['time_pressure'] is True and 'time_management' in result['skill_ids']
    g['data']['clock_before'] = [None]
    assert inspect_move(g['data'], row)['time_pressure'] is None
    g['data']['spent_seconds'] = [0]
    assert inspect_move(g['data'], row)['spent_seconds'] == 0


def test_context_tags_describe_phase_and_starting_advantage():
    g = game()
    row = g['moves'][0]
    assert 'defense' in inspect_move(g['data'], row)['opportunity_ids']
    strong = inspect_move(g['data'], {**row, 'score_before': 300})
    assert 'conversion' in strong['skill_ids'] and 'defense' not in strong['opportunity_ids']
    assert strong['phase'] == 'opening'
    mate = inspect_move(g['data'], {**row, 'mate_before': 3, 'score_before': None})
    assert {'mate', 'conversion'} <= set(mate['skill_ids'])
    mated = inspect_move(g['data'], {**row, 'mate_after': -3})
    assert 'king_safety' in mated['skill_ids']


def test_capture_with_immediate_recapture_is_not_called_hanging():
    data = {'username': 'player', 'white': 'player', 'black': 'opponent'}
    row = {'ply': 0, 'before_fen': '4k3/8/8/8/8/8/rB6/Q3K3 w - - 0 20', 'side': 'white',
           'played_uci': 'e1f1', 'best_uci': 'e1f2', 'best_line_uci': ['e1f2'],
           'assessed': True, 'category': 'mistake', 'expected_loss': .12, 'score_before': 0}
    annotation = inspect_move(data, row)
    assert annotation['eligible']
    assert 'hanging_piece' not in annotation['skill_ids']
    undefended = {**row, 'before_fen': '4k3/8/8/8/8/8/r7/Q3K3 w - - 0 20',
                  'best_uci': 'a1a2', 'best_line_uci': ['a1a2']}
    assert 'hanging_piece' in inspect_move(data, undefended)['skill_ids']


def test_incoherent_or_illegal_best_line_does_not_invent_motifs():
    g = game()
    row = {**g['moves'][0], 'best_line_uci': ['e1e8', 'a8a1']}
    result = inspect_move(g['data'], row)
    assert result['motifs'] == []
    assert 'calculation' in result['skill_ids']
    invalid = inspect_move(g['data'], {**row, 'best_uci': 'e1e8'})
    assert invalid['skill_ids'] == [] and invalid['eligible'] is False


def test_partial_analysis_and_duplicate_positions_do_not_change_rates():
    complete = game()
    complete['moves'].append(deepcopy(complete['moves'][0]))
    partial = game(2, errors=(0, 2, 4, 6, 8, 10), status='running')
    report = build_insights([complete, partial], [])
    opening = skill(report, 'opening')
    assert opening['baseline']['opportunities'] == 6
    assert opening['baseline']['errors'] == 1
    assert opening['baseline']['rate'] == 16.7
    assert report['summary']['pending_games'] == 1
    assert report['summary']['baseline']['assessed_moves'] == 6


def test_baseline_is_fixed_and_followup_uses_newest_40_by_source_time():
    baseline = [game(i) for i in range(1, 41)]
    followup = [game(i, 'followup', errors=() if i > 42 else (0,)) for i in range(41, 83)]
    report = build_insights(baseline + list(reversed(followup)), [])
    opening = skill(report, 'opening')
    assert opening['baseline']['games'] == 40 and opening['baseline']['errors'] == 40
    assert opening['recent']['games'] == 40 and opening['recent']['errors'] == 0
    assert report['summary']['tracked_games'] == 82


def test_improvement_requires_games_opportunities_and_coverage():
    before = [game(i) for i in range(1, 11)]
    after = [game(i, 'followup', errors=()) for i in range(11, 21)]
    report = build_insights(before + after, [])
    opening = skill(report, 'opening')
    assert opening['comparable'] is True and opening['delta'] == -16.7
    assert opening['status'] == 'reinforce'
    short = skill(build_insights(before + after[:9], []), 'opening')
    assert short['delta'] is None and short['comparable'] is False
    incomplete = deepcopy(after)
    for g in incomplete:
        g['moves'][1]['assessed'] = False
    low_coverage = skill(build_insights(before + incomplete, []), 'opening')
    assert low_coverage['comparable'] is False and low_coverage['delta'] is None


def test_time_rate_counts_own_timeouts_and_known_outcomes_not_moves():
    games = [game(1, result='timeout'), game(2, result='resigned'), game(3, result='win'), game(4, result=None)]
    games[2]['data']['black_result'] = 'timeout'
    for g in games:
        g['data'].update(clock_before=[10] * 12, clocks=[9] * 12)
    report = build_insights(games, [])
    time = skill(report, 'time_management')['baseline']
    assert time['opportunities'] == 3 and time['errors'] == 1 and time['rate'] == 33.3
    assert time['pressure_errors'] == 4
    assert 'games' not in report['priorities']


def test_training_counts_do_not_claim_mastery_or_create_game_evidence():
    attempts = [
        {'skill_id': 'fork', 'status': 'completed', 'mistakes': 0, 'hints_used': 0, 'is_new': True},
        {'skill_id': 'fork', 'status': 'completed', 'mistakes': 0, 'hints_used': 0, 'is_new': False},
        {'skill_id': 'fork', 'status': 'completed', 'mistakes': 1, 'hints_used': 0, 'is_new': True},
        {'skill_id': 'fork', 'status': 'completed', 'mistakes': 0, 'hints_used': 1, 'is_new': True},
        {'skill_id': 'fork', 'status': 'revealed', 'mistakes': 0, 'hints_used': 0, 'is_new': True},
    ]
    report = build_insights([], attempts)
    fork = skill(report, 'fork')
    assert fork['training'] == {'completed': 4, 'clean': 2, 'new_clean': 1}
    assert fork['status'] == 'insufficient' and fork['evidence'] == []
    assert fork['delta'] is None and fork['priority'] == 0
    assert len(report['skills']) == len(SKILLS)
    assert all(s['baseline']['rate'] is None for s in report['skills'])


def test_different_controls_cannot_produce_comparable_trend():
    before = [game(i) for i in range(1, 11)]
    after = [game(i, 'followup', errors=()) for i in range(11, 21)]
    after[-1]['data']['time_control'] = '180+2'
    report = build_insights(before + after, [])
    assert report['summary']['comparable_control'] is False
    assert all(s['delta'] is None for s in report['skills'])


def test_duplicate_game_cannot_enter_baseline_and_recent_twice():
    first = game()
    duplicate = {**deepcopy(first), 'id': 2, 'cohort': 'followup'}
    report = build_insights([duplicate, first], [])
    assert report['summary']['tracked_games'] == 1
    assert report['summary']['baseline_games'] == 1
    assert report['summary']['recent_games'] == 0


def test_new_evidence_is_not_hidden_by_many_baseline_errors():
    games = [game(i) for i in range(1, 21)] + [game(21, 'followup')]
    opening = skill(build_insights(games, []), 'opening')
    assert opening['evidence'][0]['cohort'] == 'followup'
    assert opening['evidence'][0]['game_id'] == 21


def test_opponent_rows_cannot_inflate_own_coverage_or_skills():
    g = game()
    row = deepcopy(g['moves'][0])
    board = chess.Board()
    board.push_uci('e2e4')
    row.update(ply=1, side='black', before_fen=board.fen(), played_uci='e7e5', best_uci='a7a6', best_line_uci=['a7a6'])
    g['moves'].append(row)
    report = build_insights([g], [])
    assert report['summary']['baseline']['assessed_moves'] == 6
    assert skill(report, 'opening')['baseline']['opportunities'] == 6


def test_zero_recent_errors_first_requires_reinforcement_then_monitoring():
    before = [game(i) for i in range(1, 11)]
    first_ten = [game(i, 'followup', errors=()) for i in range(11, 21)]
    initial = skill(build_insights(before + first_ten, []), 'opening')
    assert initial['status'] == 'reinforce' and initial['priority'] > 0
    twenty = first_ten + [game(i, 'followup', errors=()) for i in range(21, 31)]
    assert skill(build_insights(before + twenty, []), 'opening')['status'] == 'monitor'


def test_unmatched_player_and_missing_side_cannot_inflate_coverage():
    g = game()
    g['data']['username'] = 'not-in-this-game'
    report = build_insights([g], [])
    assert report['summary']['baseline']['assessed_moves'] == 0
    assert report['summary']['baseline']['own_moves'] == 0
    g = game()
    opponent = deepcopy(g['moves'][0])
    opponent.pop('side')
    board = chess.Board()
    board.push_uci('e2e4')
    opponent.update(ply=1, before_fen=board.fen(), played_uci='e7e5', best_uci='a7a6', best_line_uci=['a7a6'])
    g['moves'].append(opponent)
    assert build_insights([g], [])['summary']['baseline']['assessed_moves'] == 6
