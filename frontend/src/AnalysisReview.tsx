import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight, FlipVertical2, Pause, Play } from 'lucide-react';
import AnalysisBoard from './AnalysisBoard';
import BottomMoveControls from './BottomMoveControls';
import EvaluationBar, { positionEvaluation } from './EvaluationBar';
import { categoryInfo } from './analysisCategories';
import { analysisVariation } from './analysisVariation';
import { useScreenWakeLock } from './useScreenWakeLock';
import type { GameAnalysisReport } from './api';

export default function AnalysisReview({ report, selectedPly, onSelect, visible }: {
  report: GameAnalysisReport; selectedPly: number | null; onSelect: (ply: number | null) => void; visible: boolean;
}) {
  const [flipped, setFlipped] = useState(false), [playing, setPlaying] = useState(false);
  const [view, setView] = useState<{ ply: number | null; mode: 'before' | 'played' | 'line'; lineIndex: number }>({ ply: null, mode: 'played', lineIndex: 0 });
  const moveList = useRef<HTMLDivElement>(null), swipe = useRef<{ x: number; y: number } | null>(null);
  const selectedIndex = report.moves.findIndex(move => move.ply === selectedPly), move = report.moves[selectedIndex];
  const variation = useMemo(() => analysisVariation(move), [move]);
  const mode = view.ply === selectedPly && (view.mode !== 'line' || variation.length > 1) ? view.mode : 'played';
  const lineIndex = Math.min(view.ply === selectedPly ? view.lineIndex : 0, Math.max(0, variation.length - 1));
  const playerIsBlack = report.game.username?.toLowerCase() === report.game.black.toLowerCase();
  const orientation = (flipped ? !playerIsBlack : playerIsBlack) ? 'black' : 'white';
  const navigationIndex = mode === 'line' ? lineIndex : selectedIndex;
  const firstIndex = mode === 'line' ? 0 : -1, lastIndex = mode === 'line' ? variation.length - 1 : report.moves.length - 1;
  const latest = useRef({ mode, selectedPly, navigationIndex, firstIndex, lastIndex, moves: report.moves, onSelect });
  latest.current = { mode, selectedPly, navigationIndex, firstIndex, lastIndex, moves: report.moves, onSelect };
  const navigate = useCallback((index: number, stopPlaying = true) => {
    if (stopPlaying) setPlaying(false);
    const current = latest.current;
    const target = Math.max(current.firstIndex, Math.min(current.lastIndex, index));
    if (target === current.navigationIndex && current.mode !== 'before') return;
    if (current.mode === 'line') setView({ ply: current.selectedPly, mode: 'line', lineIndex: target });
    else {
      setView({ ply: null, mode: 'played', lineIndex: 0 });
      current.onSelect(current.moves[target]?.ply ?? null);
    }
  }, []);
  useScreenWakeLock(visible);
  const select = (index: number) => {
    setPlaying(false); setView({ ply: null, mode: 'played', lineIndex: 0 });
    onSelect(report.moves[Math.max(-1, Math.min(report.moves.length - 1, index))]?.ply ?? null);
  };
  useEffect(() => { if (!visible) setPlaying(false); }, [visible]);
  useEffect(() => {
    if (!playing || !visible) return;
    const startIndex = latest.current.navigationIndex, startedAt = Date.now();
    const advance = () => {
      const next = Math.min(latest.current.lastIndex, startIndex + Math.max(0, Math.floor((Date.now() - startedAt) / 1000)));
      navigate(next, false);
      if (next >= latest.current.lastIndex) setPlaying(false);
    };
    const interval = window.setInterval(advance, 100);
    document.addEventListener('visibilitychange', advance);
    return () => { window.clearInterval(interval); document.removeEventListener('visibilitychange', advance); };
  }, [playing, visible, navigate]);
  useEffect(() => {
    if (!visible) return;
    const keyboard = (event: KeyboardEvent) => {
      if ((event.target as HTMLElement).closest('input,textarea,select,[role="dialog"]') || event.altKey || event.ctrlKey || event.metaKey) return;
      const { navigationIndex, firstIndex, lastIndex } = latest.current;
      const index = event.key === 'ArrowLeft' ? navigationIndex - 1 : event.key === 'ArrowRight' ? navigationIndex + 1 : event.key === 'Home' ? firstIndex : event.key === 'End' ? lastIndex : null;
      if (index === null) return;
      event.preventDefault(); navigate(index);
    };
    window.addEventListener('keydown', keyboard);
    return () => window.removeEventListener('keydown', keyboard);
  }, [visible, navigate]);
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
      const position = variation[lineIndex];
      return { ...position, label: `Продолжение движка · ${position.label} · ${lineIndex}/${variation.length - 1}` };
    }
    return { fen: move.before_fen, lastMove: null, label: `Перед ${label}` };
  }, [move, mode, lineIndex, variation, report.game.initial_fen]);
  const evaluation = positionEvaluation(move || report.moves[0], !move || mode === 'before' || mode === 'line', display.fen, mode === 'line' && lineIndex > 0);
  const selected = move ? categoryInfo(move.category) : null;
  const annotation = move && mode === 'played' ? { square: move.played_uci.slice(2, 4), category: move.category }
    : mode === 'line' && lineIndex === 1 && display.lastMove ? { square: display.lastMove.slice(2, 4), category: 'best' as const } : undefined;
  const rows = useMemo(() => {
    const result = new Map<number, { white?: typeof move; black?: typeof move }>();
    report.moves.forEach(item => { const row = result.get(item.move_number) || {}; row[item.side] = item; result.set(item.move_number, row); });
    return [...result.entries()];
  }, [report.moves]);
  const setMode = (next: typeof mode) => { setPlaying(false); setView({ ply: selectedPly, mode: next, lineIndex: next === 'line' ? 1 : 0 }); };
  const player = (side: 'white' | 'black') => <div className="review-player"><span className={`analysis-side-dot ${side}`} /><strong>{report.game[side]}</strong><span>{side === 'white' ? 'Белые' : 'Чёрные'}</span>{report.summary[side].accuracy != null && <b title="Точность ходов">{report.summary[side].accuracy!.toFixed(1)}%</b>}</div>;
  return <section className="analysis-review" aria-label="Интерактивный разбор партии">
    <div className="review-board-column">
      <div className="review-feedback" aria-live="polite"><span className={`analysis-category-symbol ${mode === 'line' ? 'best' : move?.category || ''}`}>{mode === 'line' ? '↳' : selected?.symbol || '♟'}</span><div key={`${selectedPly ?? 'initial'}-${mode}`} className="move-comment-text" tabIndex={0} aria-label="Комментарий к ходу"><strong>{mode === 'line' ? 'Лучший вариант' : selected?.name || 'Разберём партию'}</strong><p>{mode === 'line' ? `Рекомендация движка: ${variation[1].label}. Листайте продолжение стрелками или свайпом. К партии можно вернуться кнопкой «Сыграно».` : move?.reason || 'Листайте ходы стрелками, свайпом по доске или выберите ход в списке.'}</p></div><b title={evaluation.description}>{evaluation.label}</b></div>
      {player(orientation === 'white' ? 'black' : 'white')}
      <div className="review-board-with-evaluation" onTouchStart={event => { swipe.current = event.touches.length === 1 ? { x: event.touches[0].clientX, y: event.touches[0].clientY } : null; }} onTouchCancel={() => { swipe.current = null; }} onTouchEnd={event => {
        const start = swipe.current; swipe.current = null;
        if (!start || !event.changedTouches.length) return;
        const dx = event.changedTouches[0].clientX - start.x, dy = event.changedTouches[0].clientY - start.y;
        if (Math.abs(dx) > 45 && Math.abs(dx) > Math.abs(dy) * 1.5) navigate(navigationIndex + (dx < 0 ? 1 : -1));
      }}>
        <AnalysisBoard fen={display.fen} side={orientation} lastMove={display.lastMove} identity={report.id} visible={visible} annotation={annotation} />
        <EvaluationBar evaluation={evaluation} side={orientation} />
      </div>
      {player(orientation)}
      <div className="review-position-caption"><span>{display.label}</span><button className="icon-button" aria-label="Перевернуть доску" onClick={() => setFlipped(value => !value)}><FlipVertical2 size={18} /></button></div>
      {visible && <BottomMoveControls label={mode === 'line' ? 'Навигация по варианту' : 'Навигация по партии'}>
        <button className="icon-button" aria-label={mode === 'line' ? 'Начало варианта' : 'Начальная позиция'} disabled={navigationIndex <= firstIndex} onClick={() => navigate(firstIndex)}><ChevronsLeft /></button>
        <button className="icon-button" aria-label={mode === 'line' ? 'Предыдущий ход варианта' : 'Предыдущий ход'} disabled={navigationIndex <= firstIndex} onClick={() => navigate(navigationIndex - 1)}><ChevronLeft /></button>
        <button className="button primary" aria-label={playing ? 'Остановить просмотр' : mode === 'line' ? 'Воспроизвести вариант' : 'Воспроизвести партию'} disabled={!report.moves.length} onClick={() => { if (navigationIndex >= lastIndex) navigate(firstIndex, false); else if (mode === 'before') setView({ ply: null, mode: 'played', lineIndex: 0 }); setPlaying(value => !value); }}>{playing ? <Pause size={21} /> : <Play size={21} />}</button>
        <button className="icon-button" aria-label={mode === 'line' ? 'Следующий ход варианта' : 'Следующий ход'} disabled={navigationIndex >= lastIndex} onClick={() => navigate(navigationIndex + 1)}><ChevronRight /></button>
        <button className="icon-button" aria-label={mode === 'line' ? 'Конец варианта' : 'Последний рассчитанный ход'} disabled={navigationIndex >= lastIndex} onClick={() => navigate(lastIndex)}><ChevronsRight /></button>
      </BottomMoveControls>}
      <div className="analysis-position-tabs" role="group" aria-label="Позиция на доске"><button disabled={!move} aria-pressed={mode === 'before'} onClick={() => setMode('before')}>До хода</button><button disabled={!move} aria-pressed={mode === 'played'} onClick={() => setMode('played')}>Сыграно</button><button disabled={variation.length <= 1} aria-pressed={mode === 'line'} onClick={() => setMode('line')}>Лучший вариант</button></div>
      <div className="analysis-variation-slot">{move && mode === 'line' ? <div className="analysis-variation"><button className="icon-button" aria-label="Назад по варианту" disabled={lineIndex <= 0} onClick={() => navigate(lineIndex - 1)}><ChevronLeft size={18} /></button><div>{variation.slice(1).map((position, index) => <button key={index} aria-label={position.label} aria-pressed={index + 1 === lineIndex} className={index + 1 === lineIndex ? 'active' : ''} onClick={() => navigate(index + 1)}>{position.label}</button>)}</div><button className="icon-button" aria-label="Далее по варианту" disabled={lineIndex >= variation.length - 1} onClick={() => navigate(lineIndex + 1)}><ChevronRight size={18} /></button></div> : <p className="variation-placeholder">{variation.length > 1 ? 'Нажмите «Лучший вариант», чтобы посмотреть продолжение движка.' : 'Продолжение движка для этой позиции не показано.'}</p>}</div>
      <p className="evaluation-explanation">Оценка в пешках: «+» — преимущество белых, «−» — чёрных. Это оценка позиции, а не число лишних фигур.</p>
    </div>
    <div className="review-moves-panel panel"><div className="panel-title"><h2>Ходы партии</h2><span>{selectedIndex + 1} / {report.total_plies}</span></div>
      <div ref={moveList} className="review-move-list" aria-label="Рассчитанные ходы">{rows.map(([number, row]) => <div className="review-move-row" key={number}><span>{number}.</span>{(['white', 'black'] as const).map(side => { const item = row[side]; if (!item) return <span key={side} />; const info = categoryInfo(item.category); return <button key={side} data-ply={item.ply} data-side={side} className={item.ply === selectedPly ? 'selected' : ''} aria-pressed={item.ply === selectedPly} aria-label={`Ход ${item.move_number} ${side === 'white' ? 'белых' : 'чёрных'}: ${item.played_san}, ${info.name}`} onClick={() => select(report.moves.indexOf(item))}><strong>{item.played_san}</strong><span className={`analysis-category-symbol ${item.category}`} title={info.name}>{info.symbol}</span></button>; })}</div>)}</div>
      <p className="review-result">{report.game.result}{report.status !== 'completed' && <span> · Анализ ещё не завершён</span>}</p>
    </div>
  </section>;
}
