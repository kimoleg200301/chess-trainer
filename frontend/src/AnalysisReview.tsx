import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { Chess } from 'chess.js';
import { ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight, FlipVertical2, Pause, Play } from 'lucide-react';
import AnalysisBoard from './AnalysisBoard';
import EvaluationBar, { positionEvaluation } from './EvaluationBar';
import { categoryInfo } from './analysisCategories';
import { useScreenWakeLock } from './useScreenWakeLock';
import type { GameAnalysisReport } from './api';

export default function AnalysisReview({ report, selectedPly, onSelect, visible }: {
  report: GameAnalysisReport; selectedPly: number | null; onSelect: (ply: number | null) => void; visible: boolean;
}) {
  const [flipped, setFlipped] = useState(false), [playing, setPlaying] = useState(false);
  const [view, setView] = useState<{ ply: number | null; mode: 'before' | 'played' | 'line'; lineIndex: number }>({ ply: null, mode: 'played', lineIndex: 0 });
  const moveList = useRef<HTMLDivElement>(null), swipe = useRef<{ x: number; y: number } | null>(null);
  const selectedIndex = report.moves.findIndex(move => move.ply === selectedPly), move = report.moves[selectedIndex];
  const mode = view.ply === selectedPly ? view.mode : 'played';
  const lineIndex = view.ply === selectedPly ? view.lineIndex : 0;
  const playerIsBlack = report.game.username?.toLowerCase() === report.game.black.toLowerCase();
  const orientation = (flipped ? !playerIsBlack : playerIsBlack) ? 'black' : 'white';
  const latest = useRef({ selectedIndex, moves: report.moves, onSelect });
  latest.current = { selectedIndex, moves: report.moves, onSelect };
  useScreenWakeLock(visible);
  const select = (index: number) => {
    setPlaying(false); setView({ ply: null, mode: 'played', lineIndex: 0 });
    onSelect(report.moves[Math.max(-1, Math.min(report.moves.length - 1, index))]?.ply ?? null);
  };
  useEffect(() => { if (!visible) setPlaying(false); }, [visible]);
  useEffect(() => {
    if (!playing || !visible) return;
    const startIndex = latest.current.selectedIndex, startedAt = Date.now();
    const advance = () => {
      const { moves, onSelect } = latest.current;
      const next = Math.min(moves.length - 1, startIndex + Math.max(0, Math.floor((Date.now() - startedAt) / 1000)));
      onSelect(moves[next]?.ply ?? null);
      if (next >= moves.length - 1) setPlaying(false);
    };
    const interval = window.setInterval(advance, 100);
    document.addEventListener('visibilitychange', advance);
    return () => { window.clearInterval(interval); document.removeEventListener('visibilitychange', advance); };
  }, [playing, visible]);
  useEffect(() => {
    if (!visible) return;
    const keyboard = (event: KeyboardEvent) => {
      if ((event.target as HTMLElement).closest('input,textarea,select,[role="dialog"]') || event.altKey || event.ctrlKey || event.metaKey) return;
      const { selectedIndex, moves, onSelect } = latest.current;
      const index = event.key === 'ArrowLeft' ? selectedIndex - 1 : event.key === 'ArrowRight' ? selectedIndex + 1 : event.key === 'Home' ? -1 : event.key === 'End' ? moves.length - 1 : null;
      if (index === null) return;
      event.preventDefault(); setPlaying(false); setView({ ply: null, mode: 'played', lineIndex: 0 });
      onSelect(moves[Math.max(-1, Math.min(moves.length - 1, index))]?.ply ?? null);
    };
    window.addEventListener('keydown', keyboard);
    return () => window.removeEventListener('keydown', keyboard);
  }, [visible]);
  useLayoutEffect(() => {
    const list = moveList.current, current = list?.querySelector<HTMLElement>('[aria-pressed="true"]');
    if (!visible || !list || !current) return;
    const container = list.getBoundingClientRect(), row = current.getBoundingClientRect();
    if (row.top < container.top) list.scrollTop += row.top - container.top;
    else if (row.bottom > container.bottom) list.scrollTop += row.bottom - container.bottom;
  }, [selectedPly, visible]);
  const display = useMemo(() => {
    if (!move) return { fen: report.game.initial_fen, lastMove: null, label: 'Начальная позиция' };
    const label = `${move.move_number}${move.side === 'black' ? '…' : '.'} ${move.played_san}`;
    if (mode === 'played') return { fen: move.after_fen, lastMove: move.played_uci, label: `После ${label}` };
    if (mode === 'line') {
      const board = new Chess(move.before_fen); let lastMove: string | null = null;
      for (const uci of move.best_line_uci.slice(0, lineIndex)) { try { board.move(uci); lastMove = uci; } catch { break; } }
      return { fen: board.fen(), lastMove, label: 'Продолжение движка' };
    }
    return { fen: move.before_fen, lastMove: null, label: `Перед ${label}` };
  }, [move, mode, lineIndex, report.game.initial_fen]);
  const evaluation = positionEvaluation(move || report.moves[0], !move || mode === 'before' || mode === 'line', display.fen, mode === 'line' && lineIndex > 0);
  const selected = move ? categoryInfo(move.category) : null;
  const rows = useMemo(() => {
    const result = new Map<number, { white?: typeof move; black?: typeof move }>();
    report.moves.forEach(item => { const row = result.get(item.move_number) || {}; row[item.side] = item; result.set(item.move_number, row); });
    return [...result.entries()];
  }, [report.moves]);
  const setMode = (next: typeof mode) => { setPlaying(false); setView({ ply: selectedPly, mode: next, lineIndex: 0 }); };
  const setLine = (index: number) => setView({ ply: selectedPly, mode: 'line', lineIndex: Math.max(0, Math.min(move?.best_line_uci.length || 0, index)) });
  const player = (side: 'white' | 'black') => <div className="review-player"><span className={`analysis-side-dot ${side}`} /><strong>{report.game[side]}</strong><span>{side === 'white' ? 'Белые' : 'Чёрные'}</span>{report.summary[side].accuracy != null && <b title="Точность ходов">{report.summary[side].accuracy!.toFixed(1)}%</b>}</div>;
  return <section className="analysis-review" aria-label="Интерактивный разбор партии">
    <div className="review-board-column">
      <div className="review-feedback" aria-live="polite"><span className={`analysis-category-symbol ${move?.category || ''}`}>{selected?.symbol || '♟'}</span><div><strong>{selected?.name || 'Разберём партию'}</strong><p>{move?.reason || 'Листайте ходы стрелками, свайпом по доске или выберите ход в списке.'}</p></div><b title={evaluation.description}>{evaluation.label}</b></div>
      {player(orientation === 'white' ? 'black' : 'white')}
      <div className="review-board-with-evaluation" onTouchStart={event => { swipe.current = event.touches.length === 1 ? { x: event.touches[0].clientX, y: event.touches[0].clientY } : null; }} onTouchCancel={() => { swipe.current = null; }} onTouchEnd={event => {
        const start = swipe.current; swipe.current = null;
        if (!start || !event.changedTouches.length) return;
        const dx = event.changedTouches[0].clientX - start.x, dy = event.changedTouches[0].clientY - start.y;
        if (Math.abs(dx) > 45 && Math.abs(dx) > Math.abs(dy) * 1.5) select(selectedIndex + (dx < 0 ? 1 : -1));
      }}>
        <AnalysisBoard fen={display.fen} side={orientation} lastMove={display.lastMove} identity={report.id} visible={visible} />
        <EvaluationBar evaluation={evaluation} side={orientation} />
      </div>
      {player(orientation)}
      <div className="review-position-caption"><span>{display.label}</span><button className="icon-button" aria-label="Перевернуть доску" onClick={() => setFlipped(value => !value)}><FlipVertical2 size={18} /></button></div>
      <div className="review-navigation" role="group" aria-label="Навигация по партии">
        <button className="icon-button" aria-label="Начальная позиция" disabled={selectedIndex < 0} onClick={() => select(-1)}><ChevronsLeft /></button>
        <button className="icon-button" aria-label="Предыдущий ход" disabled={selectedIndex < 0} onClick={() => select(selectedIndex - 1)}><ChevronLeft /></button>
        <button className="button primary" aria-label={playing ? 'Остановить просмотр' : 'Воспроизвести партию'} disabled={!report.moves.length} onClick={() => { if (selectedIndex >= report.moves.length - 1) onSelect(null); setView({ ply: null, mode: 'played', lineIndex: 0 }); setPlaying(value => !value); }}>{playing ? <Pause size={21} /> : <Play size={21} />}</button>
        <button className="icon-button" aria-label="Следующий ход" disabled={selectedIndex >= report.moves.length - 1} onClick={() => select(selectedIndex + 1)}><ChevronRight /></button>
        <button className="icon-button" aria-label="Последний рассчитанный ход" disabled={selectedIndex >= report.moves.length - 1} onClick={() => select(report.moves.length - 1)}><ChevronsRight /></button>
      </div>
      <div className="analysis-position-tabs" role="group" aria-label="Позиция на доске"><button disabled={!move} aria-pressed={mode === 'before'} onClick={() => setMode('before')}>До хода</button><button disabled={!move} aria-pressed={mode === 'played'} onClick={() => setMode('played')}>Сыграно</button><button disabled={!move?.best_line_uci.length} aria-pressed={mode === 'line'} onClick={() => setMode('line')}>Лучший вариант</button></div>
      {move && mode === 'line' && <div className="analysis-variation"><button className="icon-button" aria-label="Назад по варианту" disabled={lineIndex <= 0} onClick={() => setLine(lineIndex - 1)}><ChevronLeft size={18} /></button><div>{move.best_line_san.map((san, index) => <button key={index} aria-pressed={index + 1 === lineIndex} className={index + 1 === lineIndex ? 'active' : ''} onClick={() => setLine(index + 1)}>{san}</button>)}</div><button className="icon-button" aria-label="Далее по варианту" disabled={lineIndex >= move.best_line_uci.length} onClick={() => setLine(lineIndex + 1)}><ChevronRight size={18} /></button></div>}
      <p className="evaluation-explanation">Оценка в пешках: «+» — преимущество белых, «−» — чёрных. Это оценка позиции, а не число лишних фигур.</p>
    </div>
    <div className="review-moves-panel panel"><div className="panel-title"><h2>Ходы партии</h2><span>{selectedIndex + 1} / {report.total_plies}</span></div>
      <div ref={moveList} className="review-move-list" aria-label="Рассчитанные ходы">{rows.map(([number, row]) => <div className="review-move-row" key={number}><span>{number}.</span>{(['white', 'black'] as const).map(side => { const item = row[side]; if (!item) return <span key={side} />; const info = categoryInfo(item.category); return <button key={side} data-ply={item.ply} data-side={side} className={item.ply === selectedPly ? 'selected' : ''} aria-pressed={item.ply === selectedPly} aria-label={`Ход ${item.move_number} ${side === 'white' ? 'белых' : 'чёрных'}: ${item.played_san}, ${info.name}`} onClick={() => select(report.moves.indexOf(item))}><strong>{item.played_san}</strong><span className={`analysis-category-symbol ${item.category}`} title={info.name}>{info.symbol}</span></button>; })}</div>)}</div>
      <p className="review-result">{report.game.result}{report.status !== 'completed' && <span> · Анализ ещё не завершён</span>}</p>
    </div>
  </section>;
}
