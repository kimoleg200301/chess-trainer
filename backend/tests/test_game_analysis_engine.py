"""Move quality and full-game accuracy, independent of PostgreSQL."""
import math
import os
from contextlib import nullcontext
from pathlib import Path
import shutil
import threading
import chess
import chess.engine
import pytest
from app.game_analysis_engine import (
    CATEGORY_NAMES, _brilliant_evidence, _search_exact, analyse_game_move, expected_result,
    move_accuracy, ordinary_category, summarize_moves,
)
from app.generation import parse_games, position_at

@pytest.fixture(scope="session", autouse=True)
def database():
    yield

@pytest.fixture(autouse=True)
def clean_training():
    yield

OPERA_GAME = '''[White "Player A"]
[Black "Player B"]
[Result "1-0"]

1.e4 e5 2.Nf3 d6 3.d4 Bg4 4.dxe5 Bxf3 5.Qxf3 dxe5 6.Bc4 Nf6
7.Qb3 Qe7 8.Nc3 c6 9.Bg5 b5 10.Nxb5 cxb5 11.Bxb5+ Nbd7
12.O-O-O Rd8 13.Rxd7 Rxd7 14.Rd1 Qe6 15.Bxd7+ Nxd7
16.Qb8+ Nxb8 17.Rd8# 1-0
'''

def info(board, uci, cp=0, mate=None, depth=18, pv=None, **extra):
    return {"pv":[chess.Move.from_uci(m) for m in (pv or [uci])], "depth":depth,
            "score":chess.engine.PovScore(chess.engine.Mate(mate) if mate is not None else chess.engine.Cp(cp),board.turn), **extra}

def game_with(move, fen=chess.STARTING_FEN):
    return {"initial_fen":fen,"moves":[move]}

def test_expected_result_is_symmetric_and_mates_do_not_use_arbitrary_cp():
    assert expected_result(0)==.5
    assert expected_result(300)==pytest.approx(1/(1+math.exp(-1)))
    assert expected_result(800)+expected_result(-800)==pytest.approx(1)
    assert expected_result(0,4)==1 and expected_result(0,-4)==0
    assert expected_result(10000,0)==1 and expected_result(-10000,0)==0
    assert move_accuracy(0)==100
    assert move_accuracy(.1)==pytest.approx(100*math.exp(-.6))
    assert move_accuracy(1)>0
    with pytest.raises(ValueError): expected_result(float('nan'))

@pytest.mark.parametrize('loss,expected',[(0,'excellent'),(.01,'excellent'),(.0101,'good'),
    (.03,'good'),(.0301,'inaccuracy'),(.07,'inaccuracy'),(.0701,'mistake'),(.15,'mistake'),(.1501,'blunder')])
def test_category_boundaries(loss,expected):
    assert ordinary_category(loss)==expected
    assert ordinary_category(0,True)=='best'

def test_mover_pov_for_both_sides_and_no_mistake_filter():
    game={'initial_fen':chess.STARTING_FEN,'moves':['e2e4','e7e5']}
    class SameWhiteScore:
        def analyse(self,board,*_args,**_kwargs):
            first,second=('e2e4','d2d4') if board.turn else ('e7e5','c7c5')
            rows=[info(board,first),info(board,second)]
            for row in rows: row['score']=chess.engine.PovScore(chess.engine.Cp(100),chess.WHITE)
            return rows
    white=analyse_game_move(game,0,SameWhiteScore());black=analyse_game_move(game,1,SameWhiteScore())
    assert white['side']=='white' and black['side']=='black'
    assert white['score_before']==100 and black['score_before']==-100
    assert white['best_expected']==pytest.approx(1-black['best_expected'])
    assert white['accuracy']==black['accuracy']==100
    assert white['category']==black['category']=='best'
    assert chess.Board(black['after_fen']).turn==chess.WHITE

def test_bounded_alternative_gets_separate_search():
    class BoundAlternative:
        def __init__(self):self.played_searches=0
        def analyse(self,board,*args,**kwargs):
            if kwargs.get('root_moves'):
                self.played_searches+=1;return info(board,'e2e4',cp=25)
            return [info(board,'d2d4',cp=30),info(board,'e2e4',cp=25,lowerbound=True)]
    engine=BoundAlternative();result=analyse_game_move(game_with('e2e4'),0,engine)
    assert engine.played_searches==1
    assert result['assessed'] and result['category']=='excellent' and result['accuracy']>97

def test_error_rechecked_and_deeper_search_can_remove_it():
    class CorrectedError:
        options={'Clear Hash':object()}
        def __init__(self):self.roots=0;self.clears=0
        def configure(self,_):self.clears+=1
        def analyse(self,board,*args,**kwargs):
            if kwargs.get('root_moves'):return info(board,'e2e4',cp=-500 if self.roots==1 else 0)
            self.roots+=1;return [info(board,'d2d4',cp=0),info(board,'c2c4',cp=-10)]
    engine=CorrectedError();result=analyse_game_move(game_with('e2e4'),0,engine)
    assert engine.roots==2 and engine.clears==1
    assert result['category']=='excellent' and result['accuracy']==100 and result['expected_loss']==0

def test_disagreement_not_presented_as_perfect_accuracy():
    class Disagreement:
        def analyse(self,board,*args,**kwargs):
            if kwargs.get('root_moves'):return info(board,'e2e4',cp=500)
            return [info(board,'d2d4',cp=0),info(board,'c2c4',cp=-10)]
    result=analyse_game_move(game_with('e2e4'),0,Disagreement())
    assert not result['assessed'] and result['accuracy'] is None
    assert result['category']=='unassessed' and 'расходятся' in result['reason']

def test_forced_and_immediate_mate_need_no_engine_and_have_different_denominators():
    class NoSearch:
        def analyse(self,*args,**kwargs):raise AssertionError('No search needed')
    game=parse_games(OPERA_GAME)[0]
    forced=analyse_game_move(game,31,NoSearch());mate=analyse_game_move(game,32,NoSearch())
    assert forced['category']=='forced' and forced['accuracy'] is None and forced['assessed']
    assert mate['category']=='best' and mate['accuracy']==100
    assert mate['mate_after']==1 and mate['brilliant_evidence'] is None

def test_stalemate_loses_forced_win_instead_of_being_scored_mate():
    game=game_with('g6f5','7k/5K2/6Q1/8/8/8/8/8 w - - 0 1')
    class MateAvailable:
        def analyse(self,board,*args,**kwargs):
            assert not kwargs.get('root_moves')
            return [info(board,'g6g7',mate=1,depth=0),info(board,'g6h6',mate=1,depth=0)]
    result=analyse_game_move(game,0,MateAvailable())
    assert chess.Board(result['after_fen']).is_stalemate()
    assert result['played_expected']==.5 and result['best_expected']==1
    assert result['expected_loss']==.5 and result['category']=='blunder'
    assert result['score_after']==0 and result['mate_after'] is None

def test_insufficient_depth_and_deadline_are_not_rated(monkeypatch):
    class Shallow:
        def analyse(self,board,limit,**kwargs):
            assert limit.time<=2
            return [info(board,'e2e4',depth=2)]
    result=analyse_game_move(game_with('e2e4'),0,Shallow())
    assert result['category']=='unassessed' and result['accuracy'] is None
    import app.game_analysis_engine as module
    ticks=iter([0,10]);monkeypatch.setattr(module.time,'monotonic',lambda:next(ticks))
    result=analyse_game_move(game_with('e2e4'),0,Shallow())
    assert result['category']=='unassessed' and 'предел времени' in result['reason']

@pytest.mark.parametrize('missing_field', ['score', 'depth'])
def test_incomplete_engine_evidence_is_not_rated(missing_field):
    class Incomplete:
        def analyse(self, board, *_args, **_kwargs):
            row = info(board, 'e2e4')
            row.pop(missing_field)
            return [row]

    result = analyse_game_move(game_with('e2e4'), 0, Incomplete())
    assert not result['assessed']
    assert result['accuracy'] is None and result['category'] == 'unassessed'


def test_stream_uses_exact_completed_iteration_without_stale_bounds():
    board = chess.Board()
    class Streaming:
        def analysis(self, *_args, **_kwargs):
            return nullcontext(iter([
                info(board, 'e2e4', cp=10, depth=10, multipv=1, lowerbound=True),
                info(board, 'e2e4', cp=20, depth=10, multipv=1),
                info(board, 'd2d4', cp=15, depth=10, multipv=2),
                info(board, 'd2d4', cp=30, depth=11, multipv=1),
                info(board, 'e2e4', cp=25, depth=11, multipv=2, upperbound=True),
                {'score': chess.engine.PovScore(chess.engine.Cp(50), chess.WHITE), 'depth': 12},
            ]))

    rows = _search_exact(Streaming(), board, chess.engine.Limit(time=.8), multipv=2)
    assert [row['depth'] for row in rows] == [10, 10]
    assert [row['score'].pov(chess.WHITE).score() for row in rows] == [20, 15]
    assert not any(row.get('lowerbound') or row.get('upperbound') for row in rows)


def test_stream_can_complete_one_legal_continuation_and_rejects_bound_only_root():
    board = position_at(parse_games(OPERA_GAME)[0], 31)
    move = next(iter(board.legal_moves))
    class Streaming:
        def __init__(self, bounded):
            self.bounded = bounded
        def analysis(self, *_args, **_kwargs):
            return nullcontext(iter([info(board, move.uci(), depth=10, lowerbound=self.bounded)]))

    rows = _search_exact(Streaming(False), board, chess.engine.Limit(time=.8), multipv=2)
    assert len(rows) == 1 and rows[0]['pv'][0] == move
    assert _search_exact(Streaming(True), board, chess.engine.Limit(time=.8), multipv=2) == []


def test_stalled_stream_retains_engine_timeout_watchdog():
    class Stalled:
        timeout = .01
        def __init__(self):
            self.closed = threading.Event()
        def close(self):
            self.closed.set()
        def analysis(self, *_args, **_kwargs):
            def events():
                assert self.closed.wait(1), 'Streaming iteration did not preserve engine timeout'
                raise chess.engine.EngineTerminatedError('Watchdog closed unresponsive engine')
                yield  # Make this a stream without fabricating an engine result.
            return nullcontext(events())

    engine = Stalled()
    with pytest.raises(chess.engine.EngineTerminatedError):
        _search_exact(engine, chess.Board(), chess.engine.Limit(time=.01))
    assert engine.closed.is_set()


@pytest.mark.parametrize('failure_stage', ['root', 'played', 'verified_root', 'verified_played'])
def test_adaptive_retry_gives_unreliable_search_more_time(failure_stage):
    class NeedsRetry:
        def __init__(self):
            self.roots = self.played = 0
            self.retry_times = []
        def analyse(self, board, limit, **kwargs):
            if limit.time > 1:
                self.retry_times.append(limit.time)
            if kwargs.get('root_moves'):
                self.played += 1
                fail = ((failure_stage == 'played' and self.played == 1)
                        or (failure_stage == 'verified_played' and self.played == 2))
                return info(board, 'e2e4', cp=-500, depth=2 if fail else 18)
            self.roots += 1
            fail = ((failure_stage == 'root' and self.roots == 1)
                    or (failure_stage == 'verified_root' and self.roots == 2))
            return [info(board, 'd2d4', cp=0, depth=2 if fail else 18), info(board, 'c2c4', cp=-10)]

    engine = NeedsRetry()
    result = analyse_game_move(game_with('e2e4'), 0, engine)
    assert engine.retry_times == [2]
    assert result['assessed'] and result['category'] == 'blunder'
    assert result['accuracy'] < 20

def test_great_move_requires_critical_gap():
    class Critical:
        def __init__(self,second):self.second=second
        def analyse(self,board,*args,**kwargs):
            return [info(board,'e2e4',cp=0),info(board,'d2d4',cp=self.second)]
    assert analyse_game_move(game_with('e2e4'),0,Critical(-500))['category']=='great'
    assert analyse_game_move(game_with('e2e4'),0,Critical(-20))['category']=='best'

def test_immediate_exchange_recapture_not_brilliant():
    board=chess.Board('8/5b1k/8/3r4/3Q4/8/8/K2R4 w - - 0 1');played=chess.Move.from_uci('d4d5')
    root=info(board,'d4d5',cp=100,pv=['d4d5','f7d5','d1d5'])
    def compensation(position,*args,**kwargs):return [info(position,'d1d5',cp=100),info(position,'d1d4',cp=0)]
    assert _brilliant_evidence(board,played,root,expected_result(100),compensation,200000,.8) is None

def test_losing_sacrifice_or_merely_attacked_piece_not_brilliant():
    board=position_at(parse_games(OPERA_GAME)[0],30);played=chess.Move.from_uci('b3b8')
    root=info(board,'b3b8',cp=100,pv=['b3b8','d7b8','d1d8'])
    def refutation(position,*args,**kwargs):return [info(position,'d1d8',cp=-500)]
    assert _brilliant_evidence(board,played,root,expected_result(100),refutation,200000,.8) is None
    root['pv']=[played]
    assert _brilliant_evidence(board,played,root,expected_result(100),refutation,200000,.8) is None

def test_partial_and_unassessed_games_cannot_publish_final_accuracy():
    def row(ply,category,accuracy,assessed=True):return {'ply':ply,'category':category,'accuracy':accuracy,'assessed':assessed}
    rows=[row(0,'best',100),row(1,'forced',None),row(2,'mistake',50)]
    partial=summarize_moves(rows,4,completed=False)
    assert partial['white']['accuracy'] is None and partial['white']['provisional_accuracy']==75
    assert partial['black']['accuracy'] is None and partial['black']['accuracy_moves']==0
    assert partial['black']['coverage']==50
    rows.append(row(3,'unassessed',None,False));final=summarize_moves(rows,4,completed=True)
    assert final['complete'] and final['white']['accuracy']==75
    assert final['black']['accuracy'] is None and not final['black']['accuracy_complete']
    assert final['black']['counts']['forced']==1 and final['black']['counts']['unassessed']==1
    rows[-1]=row(3,'good',90);final=summarize_moves(rows,4,completed=True)
    assert final['black']['accuracy']==90 and final['black']['accuracy_moves']==1

def test_black_start_summary_and_recomputed_row_deduplication():
    rows=[{'ply':0,'category':'mistake','accuracy':50,'assessed':True},
          {'ply':1,'category':'forced','accuracy':None,'assessed':True},
          {'ply':0,'category':'best','accuracy':100,'assessed':True},
          {'ply':2,'category':'good','accuracy':80,'assessed':True}]
    result=summarize_moves(rows,3,starting_side='black',completed=True)
    assert result['black']['total_moves']==2 and result['white']['total_moves']==1
    assert result['black']['accuracy']==90 and result['black']['counts']['mistake']==0
    assert result['white']['accuracy'] is None
    assert result['white']['accuracy_complete'] and result['white']['coverage']==100
    assert sum(result['black']['counts'].values())==2 and set(result['white']['counts'])==set(CATEGORY_NAMES)

@pytest.fixture
def stockfish():
    executable=os.environ.get('TEST_STOCKFISH_PATH') or shutil.which('stockfish')
    if not executable and Path('/usr/games/stockfish').is_file():executable='/usr/games/stockfish'
    if not executable:pytest.skip('Install Stockfish or set TEST_STOCKFISH_PATH')
    with chess.engine.SimpleEngine.popen_uci(executable) as engine:
        engine.configure({'Threads':1,'Hash':32});yield engine

def test_real_sound_queen_sacrifice_for_both_colors(stockfish):
    game=parse_games(OPERA_GAME)[0];result=analyse_game_move(game,30,stockfish,max_time=1.2)
    assert result['category']=='brilliant' and result['accuracy']==100
    assert result['brilliant_evidence']['material_sacrificed_cp']==900
    assert result['best_line_san'][:3]==['Qb8+','Nxb8','Rd8#']
    board=position_at(game,30).mirror();opposite=analyse_game_move(game_with('b6b1',board.fen()),0,stockfish,max_time=1.2)
    assert opposite['side']=='black' and opposite['category']=='brilliant'
    assert opposite['mate_before']==2 and opposite['accuracy']==100

def test_real_ordinary_opening_both_sides_are_assessed(stockfish):
    game={'initial_fen':chess.STARTING_FEN,'moves':['e2e4','e7e5']}
    results=[analyse_game_move(game,ply,stockfish,max_time=1.2) for ply in range(2)]
    assert all(row['assessed'] for row in results)
    assert all(row['category'] in {'best','excellent','good'} for row in results)
    report=summarize_moves(results,2,completed=True)
    assert report['white']['accuracy'] is not None and report['black']['accuracy'] is not None
    assert report['white']['coverage']==report['black']['coverage']==100
