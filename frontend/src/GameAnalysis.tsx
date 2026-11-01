import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { Chess } from 'chess.js';
import { ArrowLeft, ArrowRight, CheckCircle2, ChevronLeft, ChevronRight, ExternalLink, FlipVertical2, Gauge, LoaderCircle, Pause, Play, RefreshCw, Search, X } from 'lucide-react';
import { api } from './api';
import AnalysisBoard from './AnalysisBoard';
import type { GameAnalysisReport, GameAnalysisSource, GameMoveCategory, GameSideSummary, ImportJob } from './api';
import './game-analysis.css';

const categories: { id: GameMoveCategory; name: string; symbol: string }[] = [
  { id: 'brilliant', name: 'Бриллиантовый', symbol: '!!' }, { id: 'great', name: 'Сильный', symbol: '!' },
  { id: 'best', name: 'Лучший', symbol: '★' }, { id: 'excellent', name: 'Отличный', symbol: '✓' },
  { id: 'good', name: 'Хороший', symbol: '·' }, { id: 'inaccuracy', name: 'Неточность', symbol: '?!' },
  { id: 'mistake', name: 'Ошибка', symbol: '?' }, { id: 'blunder', name: 'Зевок', symbol: '??' },
  { id: 'forced', name: 'Вынужденный', symbol: '→' }, { id: 'unassessed', name: 'Без оценки', symbol: '—' },
];
const categoryInfo = (id: GameMoveCategory) => categories.find(item => item.id === id) || categories[categories.length - 1];
const errorText = (error: unknown) => error instanceof Error ? error.message : 'Не удалось выполнить запрос. Попробуйте ещё раз.';
const safeLink = (url: string) => /^https:\/\/(?:lichess\.org|(?:www\.)?chess\.com)\//.test(url);
function dateLabel(value: string) {
  const date = new Date(value.replace(/^(\d{4})\.(\d{2})\.(\d{2})$/, '$1-$2-$3'));
  return Number.isNaN(date.getTime()) ? 'Дата не указана' : date.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short', year: 'numeric' });
}
function scoreLabel(score: number | null, mate: number | null) {
  if (mate != null) return mate >= 0 ? `Мат в ${Math.abs(mate)}` : `Мат соперника в ${Math.abs(mate)}`;
  return score == null ? '—' : `${score > 0 ? '+' : ''}${(score / 100).toFixed(2)}`;
}
function Spinner() { return <LoaderCircle size={17} className="spinner" aria-hidden="true" />; }

function AccuracyCard({ side, name, result, completed }: { side: 'white' | 'black'; name: string; result: GameSideSummary; completed: boolean }) {
  return <div className={`analysis-accuracy-card ${side}`}>
    <div className="analysis-player"><span className={`analysis-side-dot ${side}`} /><div><strong>{name}</strong><span>{side === 'white' ? 'Белые' : 'Чёрные'}</span></div></div>
    <div className="analysis-accuracy-value"><strong>{result.accuracy == null ? '—' : result.accuracy.toFixed(1)}{result.accuracy != null && <span>%</span>}</strong><span>Точность ходов</span></div>
    <p>{result.accuracy != null ? `Оценено ходов: ${result.assessed_moves} из ${result.total_moves}` : !result.total_moves ? 'У этой стороны нет ходов.' : result.accuracy_moves === 0 && completed && result.accuracy_complete ? 'Все ходы вынужденные — точность не считается.' : completed ? 'Часть ходов не получила надёжной оценки.' : 'Итог появится после полного анализа.'}</p>
    {result.accuracy == null && result.provisional_accuracy != null && <small className="analysis-provisional">По оценённым ходам: {result.provisional_accuracy.toFixed(1)}% · предварительно</small>}
    <div className="analysis-coverage"><span style={{ width: `${Math.max(0, Math.min(100, result.coverage || 0))}%` }} /></div><small>Проверено: {result.analysed_moves} / {result.total_moves}</small>
  </div>;
}

function MoveInspector({ report, selectedPly, onSelect, visible }: { report: GameAnalysisReport; selectedPly: number | null; onSelect: (ply: number) => void; visible: boolean }) {
  const [flipped, setFlipped] = useState<{ id: number; side: 'white' | 'black' } | null>(null);
  const [view, setView] = useState<{ key: string; mode: 'before' | 'played' | 'line'; lineIndex: number }>({ key: '', mode: 'played', lineIndex: 1 });
  const moveList = useRef<HTMLDivElement>(null);
  const moves = report.moves, index = moves.findIndex(item => item.ply === selectedPly), selectedIndex = Math.max(0, index);
  const move = index >= 0 ? moves[index] : moves[0];
  const selectionKey = `${report.id}:${move?.ply ?? 'initial'}`;
  // A newly selected move renders its own resulting position immediately,
  // without briefly borrowing the previous selection's variation or mode.
  const mode = view.key === selectionKey ? view.mode : 'played';
  const lineIndex = view.key === selectionKey ? view.lineIndex : 1;
  const orientation = flipped?.id === report.id ? flipped.side : report.game.username?.toLowerCase() === report.game.black.toLowerCase() ? 'black' : 'white';
  const setMode = (next: 'before' | 'played' | 'line') => setView({ key: selectionKey, mode: next, lineIndex: 1 });
  const setLineIndex = (next: number) => setView({ key: selectionKey, mode: 'line', lineIndex: Math.max(0, Math.min(move?.best_line_uci.length || 0, next)) });
  function selectMove(ply: number) {
    setView({ key: `${report.id}:${ply}`, mode: 'played', lineIndex: 1 });
    onSelect(ply);
  }
  useLayoutEffect(() => {
    const list = moveList.current, current = list?.querySelector<HTMLElement>('[aria-pressed="true"]');
    if (!visible || !list || !current) return;
    const container = list.getBoundingClientRect(), row = current.getBoundingClientRect();
    // Follow the selected move inside the list without scrolling the page or
    // moving the board out of view on a phone.
    if (row.top < container.top) list.scrollTop += row.top - container.top;
    else if (row.bottom > container.bottom) list.scrollTop += row.bottom - container.bottom;
  }, [selectionKey, visible]);
  const display = useMemo(() => {
    if (!move) return { fen: report.game.initial_fen, lastMove: null, label: 'Начальная позиция' };
    const moveLabel = `${move.move_number}${move.side === 'black' ? '…' : '.'} ${move.played_san}`;
    if (mode === 'played') return { fen: move.after_fen, lastMove: move.played_uci, label: `После ${moveLabel}` };
    if (mode === 'line') {
      const board = new Chess(move.before_fen); let lastMove: string | null = null;
      for (const uci of move.best_line_uci.slice(0, lineIndex)) { try { board.move(uci); lastMove = uci; } catch { break; } }
      return { fen: board.fen(), lastMove, label: 'Продолжение движка' };
    }
    return { fen: move.before_fen, lastMove: null, label: `Перед ${moveLabel}` };
  }, [move, mode, lineIndex, report.game.initial_fen]);
  const selected = move ? categoryInfo(move.category) : null;
  return <section className="analysis-inspector panel" aria-label="Просмотр ходов партии">
    <div className="analysis-board-column"><div className="analysis-board-heading"><span>{display.label}</span><button className="icon-button" aria-label="Перевернуть доску" onClick={() => setFlipped({ id: report.id, side: orientation === 'white' ? 'black' : 'white' })}><FlipVertical2 size={17} /></button></div>
      <AnalysisBoard fen={display.fen} side={orientation} lastMove={display.lastMove} identity={report.id} visible={visible} />
      <div className="analysis-position-tabs" role="group" aria-label="Позиция на доске"><button disabled={!move} aria-pressed={mode === 'before'} onClick={() => setMode('before')}>До хода</button><button disabled={!move} aria-pressed={mode === 'played'} onClick={() => setMode('played')}>Сыграно</button><button disabled={!move?.best_line_uci.length} aria-pressed={mode === 'line'} onClick={() => setMode('line')}>Лучший вариант</button></div>
      {move && mode === 'line' && <div className="analysis-variation"><button className="icon-button" aria-label="Назад по варианту" disabled={lineIndex <= 0} onClick={() => setLineIndex(lineIndex - 1)}><ChevronLeft size={16} /></button><div>{move.best_line_san.map((san, position) => <button key={position} className={position + 1 === lineIndex ? 'active' : ''} aria-pressed={position + 1 === lineIndex} onClick={() => setLineIndex(position + 1)}>{san}</button>)}</div><button className="icon-button" aria-label="Далее по варианту" disabled={lineIndex >= move.best_line_uci.length} onClick={() => setLineIndex(lineIndex + 1)}><ChevronRight size={16} /></button></div>}
    </div>
    <div className="analysis-moves-column"><div className="analysis-selected-move">{move && selected ? <><div><strong>{move.move_number}{move.side === 'black' ? '…' : '.'} {move.played_san}</strong><span className={`analysis-category ${move.category}`}><b>{selected.symbol}</b> {selected.name}</span></div><p>{move.reason}</p><div className="analysis-move-evaluations"><span>Сыграно <strong>{scoreLabel(move.score_after, move.mate_after)}</strong></span><span>Лучшее <strong>{move.best_san || '—'} · {scoreLabel(move.score_before, move.mate_before)}</strong></span></div><small>Оценка со стороны {move.side === 'white' ? 'белых' : 'чёрных'}.{move.accuracy != null && <> Точность хода: {move.accuracy.toFixed(1)}%.</>}</small></> : <><h3>Ходы появятся после начала анализа</h3><p>Выберите рассчитанный ход, чтобы посмотреть позицию и продолжение движка.</p></>}</div>
      <div className="analysis-move-navigation"><button className="button secondary" disabled={!moves.length || selectedIndex === 0} onClick={() => selectMove(moves[selectedIndex - 1].ply)}><ArrowLeft size={15} /> Назад</button><span>{moves.length ? `${selectedIndex + 1} / ${moves.length}` : 'Нет рассчитанных ходов'}</span><button className="button secondary" disabled={!moves.length || selectedIndex >= moves.length - 1} onClick={() => selectMove(moves[selectedIndex + 1].ply)}>Далее <ArrowRight size={15} /></button></div>
      <div ref={moveList} className="analysis-move-list" aria-label="Рассчитанные ходы">{moves.map(item => { const info = categoryInfo(item.category); return <button key={item.ply} data-ply={item.ply} data-side={item.side} className={`${item.category} ${move?.ply === item.ply ? 'selected' : ''}`} aria-pressed={move?.ply === item.ply} aria-label={`Ход ${item.move_number} ${item.side === 'white' ? 'белых' : 'чёрных'}: ${item.played_san}, ${info.name}`} onClick={() => selectMove(item.ply)}><span className="analysis-move-number">{item.move_number}{item.side === 'black' ? '…' : '.'}</span><strong>{item.played_san}</strong><span className={`analysis-category-symbol ${item.category}`} title={info.name}>{info.symbol}</span></button>; })}</div>
    </div>
  </section>;
}

export default function GameAnalysis({ visible, onDataChanged }: { visible: boolean; onDataChanged?: () => void }) {
  const [username, setUsername] = useState(''), [month, setMonth] = useState(''), [limit, setLimit] = useState(10);
  const [games, setGames] = useState<GameAnalysisSource[]>([]), [selectedGame, setSelectedGame] = useState<number | null>(null);
  const [report, setReport] = useState<GameAnalysisReport | null>(null), [selectedPly, setSelectedPly] = useState<number | null>(null);
  const [loadingGames, setLoadingGames] = useState(false), [loadingReport, setLoadingReport] = useState(false);
  const [actionBusy, setActionBusy] = useState<'import' | 'start' | 'restart' | null>(null), [stepBusy, setStepBusy] = useState(false), [runningId, setRunningId] = useState<number | null>(null);
  const [error, setError] = useState(''), [notice, setNotice] = useState('');
  const mounted = useRef(true), visibleRef = useRef(visible), selectedRef = useRef<number | null>(null), analysisRef = useRef<number | null>(null);
  const activeRun = useRef<{ id: number; gameId: number } | null>(null), stepFlight = useRef(false), actionFlight = useRef(false);
  const nextStep = useRef<number | undefined>(undefined), listVersion = useRef(0), selectionVersion = useRef(0), reportRevision = useRef(0), gamesRevision = useRef(0);
  const changedRef = useRef(onDataChanged); visibleRef.current = visible; changedRef.current = onDataChanged;
  const pause = useCallback(() => { activeRun.current = null; setRunningId(null); window.clearTimeout(nextStep.current); }, []);
  const updateGame = useCallback((result: GameAnalysisReport, gameId: number) => {
    gamesRevision.current++;
    setGames(previous => previous.map(game => game.id === gameId || game.analysis_id === result.id ? { ...game, analysis_id: result.id, analysis_status: result.status, analysis_outdated: result.analysis_outdated } : game));
  }, []);
  const setCurrentReport = useCallback((result: GameAnalysisReport) => {
    reportRevision.current++; analysisRef.current = result.id; setReport(result);
    setSelectedPly(previous => previous !== null && result.moves.some(move => move.ply === previous) ? previous : result.moves[0]?.ply ?? null);
  }, []);
  const processStep = useCallback(async () => {
    const run = activeRun.current;
    if (!run || stepFlight.current || actionFlight.current || !mounted.current || !visibleRef.current || document.visibilityState !== 'visible') return;
    stepFlight.current = true; setStepBusy(true);
    try {
      const result = await api<GameAnalysisReport>(`/game-analysis/${run.id}/step`, {});
      if (!mounted.current) return;
      updateGame(result, run.gameId);
      if (selectedRef.current === run.gameId && analysisRef.current === run.id) setCurrentReport(result);
      if ((result.status === 'completed' || result.status === 'failed') && activeRun.current?.id === run.id) {
        activeRun.current = null; setRunningId(null);
        if (result.status === 'failed') setError(result.error || 'Анализ остановлен. Можно продолжить позже.');
        else setNotice('Анализ партии завершён. Выберите ход, чтобы рассмотреть его подробнее.');
      }
    } catch (err) { if (mounted.current && activeRun.current?.id === run.id) { pause(); setError(errorText(err)); } }
    finally { stepFlight.current = false; if (mounted.current) { setStepBusy(false); if (activeRun.current && visibleRef.current && document.visibilityState === 'visible') nextStep.current = window.setTimeout(() => { void processStep(); }, 180); } }
  }, [pause, setCurrentReport, updateGame]);
  const resume = useCallback((id: number, gameId: number) => {
    if (stepFlight.current || actionFlight.current || !visibleRef.current || document.visibilityState !== 'visible') return;
    setError(''); setNotice(''); activeRun.current = { id, gameId }; setRunningId(id); void processStep();
  }, [processStep]);
  const chooseGame = useCallback(async (game: GameAnalysisSource) => {
    pause(); selectedRef.current = game.id; analysisRef.current = game.analysis_id; setSelectedGame(game.id); setReport(null); setSelectedPly(null); setError(''); setNotice('');
    const version = ++selectionVersion.current, revision = reportRevision.current; setLoadingReport(game.analysis_id !== null);
    if (game.analysis_id === null) return;
    try { const result = await api<GameAnalysisReport>(`/game-analysis/${game.analysis_id}`); if (mounted.current && selectedRef.current === game.id && selectionVersion.current === version && revision === reportRevision.current) { setCurrentReport(result); updateGame(result, game.id); } }
    catch (err) { if (mounted.current && selectionVersion.current === version) setError(errorText(err)); }
    finally { if (mounted.current && selectionVersion.current === version) setLoadingReport(false); }
  }, [pause, setCurrentReport, updateGame]);
  const loadGames = useCallback(async (preferredGameId?: number) => {
    const version = ++listVersion.current, revision = gamesRevision.current; setLoadingGames(true);
    try {
      const result = await api<{ games: GameAnalysisSource[] }>('/game-analysis/games');
      if (!mounted.current || version !== listVersion.current) return;
      setGames(previous => revision === gamesRevision.current ? result.games : result.games.map(game => previous.find(item => item.id === game.id && item.analysis_id) || game));
      const preferred = preferredGameId == null ? null : result.games.find(game => game.id === preferredGameId);
      if (preferred) void chooseGame(preferred); else if (selectedRef.current === null && result.games.length) void chooseGame(result.games[0]);
    } catch (err) { if (mounted.current && version === listVersion.current) setError(errorText(err)); }
    finally { if (mounted.current && version === listVersion.current) setLoadingGames(false); }
  }, [chooseGame]);
  useEffect(() => {
    mounted.current = true; const hide = () => { if (document.visibilityState !== 'visible') pause(); };
    document.addEventListener('visibilitychange', hide); window.addEventListener('pagehide', pause);
    return () => { mounted.current = false; activeRun.current = null; window.clearTimeout(nextStep.current); document.removeEventListener('visibilitychange', hide); window.removeEventListener('pagehide', pause); };
  }, [pause]);
  useEffect(() => { if (visible) void loadGames(); else pause(); }, [visible, loadGames, pause]);
  async function importGames(event: FormEvent) {
    event.preventDefault(); if (actionFlight.current || stepFlight.current) return;
    actionFlight.current = true; pause(); setActionBusy('import'); setError(''); setNotice('');
    try { const result = await api<ImportJob>('/imports/chesscom', { username: username.trim(), month: month || null, limit }); if (!mounted.current) return; changedRef.current?.(); await loadGames(result.games[0]?.id); if (mounted.current) setNotice('Партии загружены. Выберите нужную и запустите отдельный анализ ходов.'); }
    catch (err) { if (mounted.current) setError(errorText(err)); }
    finally { actionFlight.current = false; if (mounted.current) setActionBusy(null); }
  }
  async function startAnalysis(restart = false) {
    const gameId = selectedRef.current; if (gameId == null || actionFlight.current || stepFlight.current || (restart && !report)) return;
    actionFlight.current = true; pause(); setActionBusy(restart ? 'restart' : 'start'); setError(''); setNotice(''); const version = ++selectionVersion.current;
    try {
      const result = await api<GameAnalysisReport>(restart ? `/game-analysis/${report!.id}/restart` : '/game-analysis', restart ? {} : { game_id: gameId });
      if (!mounted.current) return; updateGame(result, gameId);
      if (selectedRef.current === gameId && selectionVersion.current === version) {
        setCurrentReport(result); setLoadingReport(false); actionFlight.current = false;
        if (result.status !== 'completed' && !result.analysis_outdated && visibleRef.current && document.visibilityState === 'visible') resume(result.id, gameId);
        else if (result.analysis_outdated) setNotice('Доступна обновлённая проверка. Нажмите «Пересчитать партию».');
      }
    } catch (err) { if (mounted.current && selectedRef.current === gameId) setError(errorText(err)); }
    finally { actionFlight.current = false; if (mounted.current) setActionBusy(null); }
  }
  const currentGame = games.find(game => game.id === selectedGame), running = report != null && runningId === report.id, completed = report?.status === 'completed', disabled = actionBusy !== null || stepBusy;
  return <section className="game-analysis-page">
    <div className="page-heading"><div><span className="eyebrow">ПОНЯТЬ СВОЮ ИГРУ</span><h1>Анализ партий</h1><p>Точность, сильные решения и моменты для улучшения — ход за ходом.</p></div><span className="analysis-heading-icon"><Gauge size={28} /></span></div>
    <form className="panel analysis-import" onSubmit={event => void importGames(event)}><div><h2>Загрузить партии Chess.com</h2><p>Можно выбрать уже загруженную партию ниже или найти новые по нику.</p></div><div className="analysis-import-fields"><label>Ник на Chess.com<input value={username} onChange={event => setUsername(event.target.value)} autoCapitalize="none" autoCorrect="off" spellCheck={false} maxLength={40} required placeholder="Ваш ник" disabled={actionBusy !== null} /></label><label>Месяц <span>необязательно</span><input type="month" value={month} onChange={event => setMonth(event.target.value)} min="2007-01" max={new Date().toISOString().slice(0, 7)} disabled={actionBusy !== null} /></label><label>Количество<input type="number" value={limit} onChange={event => setLimit(Number(event.target.value))} min="1" max="50" required disabled={actionBusy !== null} /></label><button className="button secondary" disabled={disabled || !username.trim()}>{actionBusy === 'import' ? <Spinner /> : <Search size={16} />}{actionBusy === 'import' ? 'Загружаем…' : 'Загрузить партии'}</button></div></form>
    {error && <div className="error-notice" role="alert"><span>{error}</span><button aria-label="Закрыть ошибку анализа" onClick={() => setError('')}><X size={16} /></button></div>}
    {notice && <div className="analysis-notice" role="status"><CheckCircle2 size={17} /><span>{notice}</span><button className="icon-button" aria-label="Закрыть уведомление анализа" onClick={() => setNotice('')}><X size={15} /></button></div>}
    <div className="analysis-workspace"><aside className="panel analysis-game-picker"><div className="panel-title"><h2>Партии</h2><button className="icon-button" aria-label="Обновить партии для анализа" disabled={loadingGames} onClick={() => void loadGames()}>{loadingGames ? <Spinner /> : <RefreshCw size={16} />}</button></div><div className="analysis-game-options">{games.map(game => <button key={game.id} className={selectedGame === game.id ? 'selected' : ''} aria-pressed={selectedGame === game.id} onClick={() => void chooseGame(game)}><strong>{game.white}<span>— {game.black}</span></strong><small>{dateLabel(game.source_game_at || game.date)} · {game.result}</small><span className="analysis-list-state">{game.analysis_outdated ? 'Доступен пересчёт' : game.analysis_status === 'completed' ? 'Анализ готов' : game.analysis_status === 'failed' ? 'Можно повторить' : game.analysis_id != null ? runningId === game.analysis_id ? 'Анализируем' : 'Можно продолжить' : 'Ещё не анализировали'}</span></button>)}</div>{!games.length && <p className="analysis-empty">{loadingGames ? 'Загружаем список…' : 'Здесь появятся ваши загруженные партии.'}</p>}</aside>
      <div className="game-analysis-result" aria-busy={loadingReport}>
        {loadingReport ? <div className="panel analysis-placeholder"><Spinner /><p>Открываем анализ…</p></div> : !currentGame ? <div className="panel analysis-placeholder"><Gauge size={32} /><h2>Выберите партию для разбора</h2><p>Движок проверит ходы обеих сторон. Результат сохранится, и к нему можно будет вернуться.</p></div> : <>
          <section className="panel analysis-report-header"><div><h2>{currentGame.white} — {currentGame.black}</h2><p>{dateLabel(currentGame.source_game_at || currentGame.date)} · {currentGame.result}</p></div><div className="analysis-report-actions">{!report ? <button className="button primary" disabled={disabled} onClick={() => void startAnalysis()}>{actionBusy === 'start' ? <Spinner /> : <Play size={16} />}Анализировать партию</button> : running ? <button className="button secondary" onClick={pause}><Pause size={16} />Пауза</button> : !completed && !report.analysis_outdated ? <button className="button primary" disabled={disabled} onClick={() => resume(report.id, currentGame.id)}>{stepBusy ? <Spinner /> : <Play size={16} />}{stepBusy ? 'Сохраняем текущий ход…' : 'Продолжить анализ'}</button> : completed ? <span className="analysis-done"><CheckCircle2 size={17} />Анализ готов</span> : null}{report && <button className="button secondary" disabled={disabled} onClick={() => void startAnalysis(true)}>{actionBusy === 'restart' ? <Spinner /> : <RefreshCw size={15} />}Пересчитать партию</button>}</div>
            {report && <div className="analysis-report-progress"><div role="progressbar" aria-label="Анализ ходов партии" aria-valuemin={0} aria-valuemax={report.total_plies || 1} aria-valuenow={Math.min(report.cursor, report.total_plies || 1)}><span style={{ width: `${report.total_plies ? Math.min(100, report.cursor / report.total_plies * 100) : completed ? 100 : 0}%` }} /></div><span>Проверено ходов: {report.cursor} / {report.total_plies}</span></div>}
            {report?.analysis_outdated && <p className="analysis-status-note">Для этой партии доступна обновлённая проверка. Пересчитайте её, чтобы получить новые оценки.</p>}
            {report && !completed && <p className="analysis-status-note">{running ? 'Анализ идёт, пока этот раздел открыт. При переходе или скрытии вкладки он остановится на паузу.' : stepBusy ? 'Текущий ход будет сохранён. Новые ходы пока не проверяем.' : 'Прогресс сохранён. Анализ можно продолжить позже.'}</p>}
            {report?.error && <p className="analysis-status-note">{report.error}</p>}{safeLink(currentGame.game_url) && <a className="source-link" href={currentGame.game_url} target="_blank" rel="noreferrer">Исходная партия <ExternalLink size={12} /></a>}
          </section>
          {report && <><div className="analysis-accuracy-cards"><AccuracyCard side="white" name={currentGame.white} result={report.summary.white} completed={completed} /><AccuracyCard side="black" name={currentGame.black} result={report.summary.black} completed={completed} /></div>
            <section className="panel analysis-categories"><div className="panel-title"><h2>Качество ходов</h2><span>{completed ? 'Итог партии' : 'По проверенным ходам'}</span></div><div className="analysis-counts-heading"><span>Категория</span><span>Белые</span><span>Чёрные</span></div>{categories.map(item => <div className="analysis-counts-row" key={item.id}><span><b className={`analysis-category-symbol ${item.id}`}>{item.symbol}</b>{item.name}</span><strong>{report.summary.white.counts[item.id] || 0}</strong><strong>{report.summary.black.counts[item.id] || 0}</strong></div>)}<p>Вынужденные ходы и ходы без надёжной оценки не входят в расчёт точности.</p></section>
            <MoveInspector report={report} selectedPly={selectedPly} onSelect={setSelectedPly} visible={visible} />
          </>}
        </>}
      </div>
    </div>
    <p className="analysis-method-note">Оценка тренажёра на основе Stockfish; она может отличаться от Chess.com. Метка «Бриллиантовый» — приблизительная оценка корректной жертвы материала. Анализ партии не создаёт тренировочные задачи.</p>
  </section>;
}
