import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { ArrowLeft, ArrowRight, CheckCircle2, ExternalLink, Gauge, LoaderCircle, Pause, Play, RefreshCw, Search, X } from 'lucide-react';
import { api } from './api';
import { useBackgroundRefresh } from './useBackgroundRefresh';
import AnalysisReview from './AnalysisReview';
import SoundToggle from './SoundToggle';
import { playSound } from './sound';
import { categories } from './analysisCategories';
import type { GameAnalysisReport, GameAnalysisSource, GameSideSummary, ImportJob } from './api';
import './game-analysis.css';

const errorText = (error: unknown) => error instanceof Error ? error.message : 'Не удалось выполнить запрос. Попробуйте ещё раз.';
const safeLink = (url: string) => /^https:\/\/(?:lichess\.org|(?:www\.)?chess\.com)\//.test(url);
function dateLabel(value: string) {
  const date = new Date(value.replace(/^(\d{4})\.(\d{2})\.(\d{2})$/, '$1-$2-$3'));
  return Number.isNaN(date.getTime()) ? 'Дата не указана' : date.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short', year: 'numeric' });
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

export default function GameAnalysis({ visible, onDataChanged, onFocusChange }: { visible: boolean; onDataChanged?: () => void; onFocusChange?: (focused: boolean) => void }) {
  const [screen, setScreen] = useState<'list' | 'progress' | 'review'>('list');
  const screenRef = useRef(screen), lastReport = useRef<GameAnalysisReport | null>(null);
  const backButton = useRef<HTMLButtonElement>(null);
  const openScreen = useCallback((next: typeof screen) => { screenRef.current = next; setScreen(next); }, []);
  useEffect(() => { onFocusChange?.(screen !== 'list'); }, [screen, onFocusChange]);
  useEffect(() => { if (visible) { window.scrollTo({ top: 0, behavior: 'instant' }); backButton.current?.focus({ preventScroll: true }); } }, [screen, visible]);
  const [username, setUsername] = useState(''), [month, setMonth] = useState(''), [limit, setLimit] = useState(10);
  const [games, setGames] = useState<GameAnalysisSource[]>([]), [selectedGame, setSelectedGame] = useState<number | null>(null);
  const [report, setReport] = useState<GameAnalysisReport | null>(null), [selectedPly, setSelectedPly] = useState<number | null>(null);
  const [loadingGames, setLoadingGames] = useState(false), [loadingReport, setLoadingReport] = useState(false);
  const [actionBusy, setActionBusy] = useState<'import' | 'start' | 'restart' | null>(null), [stepBusy, setStepBusy] = useState(false);
  const [error, setError] = useState(''), [notice, setNotice] = useState('');
  const mounted = useRef(true), selectedRef = useRef<number | null>(null), analysisRef = useRef<number | null>(null);
  const stepFlight = useRef(false), actionFlight = useRef(false);
  const listVersion = useRef(0), selectionVersion = useRef(0), reportRevision = useRef(0), gamesRevision = useRef(0);
  const changedRef = useRef(onDataChanged); changedRef.current = onDataChanged;
  const updateGame = useCallback((result: GameAnalysisReport, gameId: number) => {
    gamesRevision.current++;
    setGames(previous => previous.map(game => game.id === gameId || game.analysis_id === result.id ? { ...game, analysis_id: result.id, analysis_status: result.status, analysis_outdated: result.analysis_outdated, background_requested: result.background_requested } : game));
  }, []);
  const setCurrentReport = useCallback((result: GameAnalysisReport) => {
    if (lastReport.current?.id === result.id && lastReport.current.status !== 'completed' && result.status === 'completed') playSound('complete');
    lastReport.current = result;
    reportRevision.current++; analysisRef.current = result.id; setReport(result);
    if (result.status === 'completed' && screenRef.current === 'progress') openScreen('review');
    setSelectedPly(previous => previous !== null && result.moves.some(move => move.ply === previous) ? previous : null);
  }, [openScreen]);
  async function controlAnalysis(id: number, gameId: number, enabled: boolean) {
    if (stepFlight.current || actionFlight.current) return;
    stepFlight.current = true; setStepBusy(true); setError(''); setNotice('');
    reportRevision.current++; gamesRevision.current++;
    try {
      const result = await api<GameAnalysisReport>(`/game-analysis/${id}/${enabled ? 'resume' : 'pause'}`, {});
      if (!mounted.current) return;
      updateGame(result, gameId);
      if (selectedRef.current === gameId && analysisRef.current === id) setCurrentReport(result);
    } catch (err) { if (mounted.current) setError(errorText(err)); }
    finally { stepFlight.current = false; if (mounted.current) setStepBusy(false); }
  }
  const resume = (id: number, gameId: number) => { void controlAnalysis(id, gameId, true); };
  const pause = () => { if (report && selectedRef.current !== null) void controlAnalysis(report.id, selectedRef.current, false); };
  const chooseGame = useCallback(async (game: GameAnalysisSource) => {
    lastReport.current = null;
    openScreen(game.analysis_status === 'completed' ? 'review' : 'progress');
    selectedRef.current = game.id; analysisRef.current = game.analysis_id; setSelectedGame(game.id); setReport(null); setSelectedPly(null); setError(''); setNotice('');
    const version = ++selectionVersion.current, revision = reportRevision.current; setLoadingReport(game.analysis_id !== null);
    if (game.analysis_id === null) return;
    try {
      let result = await api<GameAnalysisReport>(`/game-analysis/${game.analysis_id}`);
      const stillSelected = () => mounted.current && selectedRef.current === game.id && selectionVersion.current === version && revision === reportRevision.current;
      if (!stillSelected()) return;
      if (result.status === 'completed' && !result.analysis_outdated && result.retry_pending) {
        result = await api<GameAnalysisReport>(`/game-analysis/${result.id}/resume`, {});
        if (!stillSelected()) return;
        if (result.status !== 'completed') openScreen('progress');
      }
      setCurrentReport(result); updateGame(result, game.id);
    }
    catch (err) { if (mounted.current && selectionVersion.current === version) setError(errorText(err)); }
    finally { if (mounted.current && selectionVersion.current === version) setLoadingReport(false); }
  }, [setCurrentReport, updateGame, openScreen]);
  const loadGames = useCallback(async () => {
    const version = ++listVersion.current, revision = gamesRevision.current; setLoadingGames(true);
    try {
      const result = await api<{ games: GameAnalysisSource[] }>('/game-analysis/games');
      if (!mounted.current || version !== listVersion.current) return;
      setGames(previous => revision === gamesRevision.current ? result.games : result.games.map(game => previous.find(item => item.id === game.id && item.analysis_id) || game));
    } catch (err) { if (mounted.current && version === listVersion.current) setError(errorText(err)); }
    finally { if (mounted.current && version === listVersion.current) setLoadingGames(false); }
  }, []);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  useEffect(() => { if (visible) void loadGames(); }, [visible, loadGames]);
  useBackgroundRefresh(async () => {
    if (stepFlight.current || actionFlight.current) return;
    const id = analysisRef.current, gameId = selectedRef.current, revision = reportRevision.current, selection = selectionVersion.current, list = gamesRevision.current;
    try {
      const [result, current] = await Promise.all([
        api<{ games: GameAnalysisSource[] }>('/game-analysis/games'),
        id === null ? Promise.resolve(null) : api<GameAnalysisReport>(`/game-analysis/${id}`),
      ]);
      if (!mounted.current) return;
      if (list === gamesRevision.current) setGames(result.games);
      if (current && selectedRef.current === gameId && analysisRef.current === id && revision === reportRevision.current && selection === selectionVersion.current) {
        setCurrentReport(current); updateGame(current, gameId!);
      } else if (id === null && selectedRef.current === gameId && selection === selectionVersion.current) {
        const game = result.games.find(item => item.id === gameId);
        if (game?.analysis_id != null && screenRef.current !== 'list') void chooseGame(game);
      }
    } catch (err) { if (mounted.current) setError(errorText(err)); }
  }, visible);
  async function importGames(event: FormEvent) {
    event.preventDefault(); if (actionFlight.current || stepFlight.current) return;
    actionFlight.current = true; reportRevision.current++; gamesRevision.current++; setActionBusy('import'); setError(''); setNotice('');
    try { await api<ImportJob>('/imports/chesscom', { username: username.trim(), month: month || null, limit }); if (!mounted.current) return; changedRef.current?.(); await loadGames(); if (mounted.current) setNotice('Партии загружены. Выберите нужную и запустите отдельный анализ ходов.'); }
    catch (err) { if (mounted.current) setError(errorText(err)); }
    finally { actionFlight.current = false; if (mounted.current) setActionBusy(null); }
  }
  async function startAnalysis(restart = false) {
    const gameId = selectedRef.current; if (gameId == null || actionFlight.current || stepFlight.current || (restart && !report)) return;
    openScreen('progress'); setSelectedPly(null);
    actionFlight.current = true; reportRevision.current++; gamesRevision.current++; setActionBusy(restart ? 'restart' : 'start'); setError(''); setNotice(''); const version = ++selectionVersion.current;
    try {
      const result = await api<GameAnalysisReport>(restart ? `/game-analysis/${report!.id}/restart` : '/game-analysis', restart ? { background: true } : { game_id: gameId, background: true });
      if (!mounted.current) return; updateGame(result, gameId);
      if (selectedRef.current === gameId && selectionVersion.current === version) {
        setCurrentReport(result); setLoadingReport(false); actionFlight.current = false;
        if (result.analysis_outdated) setNotice('Доступна обновлённая проверка. Нажмите «Пересчитать партию».');
      }
    } catch (err) { if (mounted.current && selectedRef.current === gameId) setError(errorText(err)); }
    finally { actionFlight.current = false; if (mounted.current) setActionBusy(null); }
  }
  const currentGame = games.find(game => game.id === selectedGame), running = !!report?.background_requested && !report.analysis_outdated && report.status !== 'completed' && report.status !== 'failed', completed = report?.status === 'completed', disabled = actionBusy !== null || stepBusy;
  const percent = actionBusy === 'restart' ? 0 : completed ? 100 : report?.total_plies ? Math.min(99, Math.floor(report.cursor / report.total_plies * 100)) : 0;
  const retrying = !completed && report?.cursor === report?.total_plies && !!report?.retry_pending;
  const progressText = retrying ? `Уточняем оценки ходов: осталось ${report?.retry_pending}. Для них увеличено время анализа.` : `Проверено: ${report?.cursor || 0} из ${report?.total_plies || currentGame?.total_plies || 0} полуходов`;
  const alerts = <>{error && <div className="error-notice" role="alert"><span>{error}</span><button aria-label="Закрыть ошибку анализа" onClick={() => setError('')}><X size={16} /></button></div>}{notice && <div className="analysis-notice" role="status"><CheckCircle2 size={17} /><span>{notice}</span><button className="icon-button" aria-label="Закрыть уведомление анализа" onClick={() => setNotice('')}><X size={15} /></button></div>}</>;
  const controls = <>{!report ? <button className="button primary" disabled={disabled} onClick={() => void startAnalysis()}>{actionBusy === 'start' ? <Spinner /> : <Play size={17} />}Анализировать партию</button> : running ? <button className="button secondary" disabled={disabled} onClick={pause}><Pause size={17} />Пауза</button> : !completed && !report.analysis_outdated ? <button className="button primary" disabled={disabled} onClick={() => resume(report.id, currentGame!.id)}>{stepBusy ? <Spinner /> : <Play size={17} />}Продолжить анализ</button> : null}{report && !running && <button className="button secondary" disabled={disabled} onClick={() => void startAnalysis(true)}>{actionBusy === 'restart' ? <Spinner /> : <RefreshCw size={16} />}Пересчитать партию</button>}</>;
  if (screen === 'list') return <section className="game-analysis-page analysis-list-screen">
    <div className="page-heading"><div><span className="eyebrow">ПОНЯТЬ СВОЮ ИГРУ</span><h1>Анализ партий</h1><p>Выберите партию. Каждый ход — на отдельном экране разбора.</p></div><span className="analysis-heading-icon"><Gauge size={28} /></span></div>
    <details className="panel analysis-import-disclosure" open={!games.length || undefined}><summary>Загрузить партии Chess.com</summary><form className="analysis-import" onSubmit={event => void importGames(event)}><p>Найдите открытые завершённые партии по нику игрока.</p><div className="analysis-import-fields"><label>Ник на Chess.com<input value={username} onChange={event => setUsername(event.target.value)} autoCapitalize="none" autoCorrect="off" spellCheck={false} maxLength={40} required placeholder="Ваш ник" disabled={actionBusy !== null} /></label><label>Месяц <span>необязательно</span><input type="month" value={month} onChange={event => setMonth(event.target.value)} min="2007-01" max={new Date().toISOString().slice(0, 7)} disabled={actionBusy !== null} /></label><label>Количество<input type="number" value={limit} onChange={event => setLimit(Number(event.target.value))} min="1" max="50" required disabled={actionBusy !== null} /></label><button className="button secondary" disabled={disabled || !username.trim()}>{actionBusy === 'import' ? <Spinner /> : <Search size={16} />}{actionBusy === 'import' ? 'Загружаем…' : 'Загрузить партии'}</button></div></form></details>
    {alerts}
    <section className="analysis-game-picker"><div className="panel-title"><h2>Ваши партии <span className="subtle-count">{games.length}</span></h2><button className="icon-button" aria-label="Обновить партии для анализа" disabled={loadingGames} onClick={() => void loadGames()}>{loadingGames ? <Spinner /> : <RefreshCw size={18} />}</button></div>
      <div className="analysis-game-options">{games.map(game => <button key={game.id} onClick={() => void chooseGame(game)} aria-label={`Открыть партию ${game.white} — ${game.black}`}><div className="analysis-game-card-heading"><span className="analysis-game-result">{game.result}</span><ArrowRight size={18} /></div><strong>{game.white}<span>— {game.black}</span></strong><small>{dateLabel(game.source_game_at || game.date)} · {game.total_plies} полуходов</small><span className={`analysis-list-state ${game.analysis_status === 'completed' ? 'ready' : ''}`}>{game.analysis_status === 'completed' ? 'Открыть разбор' : game.analysis_outdated ? 'Доступен пересчёт' : game.analysis_status === 'failed' ? 'Повторить анализ' : game.analysis_id != null ? game.background_requested ? 'Анализируем в фоне' : 'Продолжить анализ' : 'Анализировать'}</span></button>)}</div>
      {!games.length && <div className="panel analysis-placeholder"><Gauge size={32} /><h2>{loadingGames ? 'Загружаем партии…' : 'Пока нет партий'}</h2><p>Загрузите партии Chess.com по нику выше. Уже импортированные партии из раздела «Мои партии» тоже появятся здесь.</p></div>}
    </section>
  </section>;
  return <section className={`game-analysis-page analysis-focused analysis-${screen}-screen`}>
    <div className="analysis-screen-toolbar"><button ref={backButton} className="text-button" onClick={() => openScreen('list')}><ArrowLeft size={18} />К партиям</button><span>{screen === 'review' ? 'Разбор партии' : 'Анализ партии'}</span><SoundToggle /></div>
    {alerts}
    {report?.analysis_outdated && <p className="analysis-notice">Сохранён разбор предыдущей версии. Нажмите «Пересчитать партию», чтобы проверить её Stockfish 19 с увеличенным временем анализа.</p>}
    {loadingReport ? <div className="panel analysis-placeholder" aria-busy="true"><Spinner /><p>Открываем анализ…</p></div> : !currentGame ? <div className="panel analysis-placeholder"><p>Не удалось найти выбранную партию. Вернитесь к списку и обновите его.</p></div> : screen === 'progress' ? <section className="analysis-progress-card panel" aria-label="Ход анализа">
      <span className="eyebrow">{running ? 'STOCKFISH 19 ПРОВЕРЯЕТ ХОДЫ' : report?.status === 'failed' ? 'АНАЛИЗ ПРЕРВАН' : report ? 'ПРОГРЕСС СОХРАНЁН' : 'ГОТОВЫ К РАЗБОРУ'}</span>
      <h1>{currentGame.white}<span>против {currentGame.black}</span></h1><p>{dateLabel(currentGame.source_game_at || currentGame.date)} · {currentGame.result}</p>
      <div className={`analysis-progress-ring ${running ? 'running' : ''}`} role="progressbar" aria-label="Анализ ходов партии" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent} aria-valuetext={`${percent}%, ${progressText}`}>
        <svg viewBox="0 0 140 140" aria-hidden="true"><circle cx="70" cy="70" r="62" /><circle cx="70" cy="70" r="62" pathLength="100" strokeDasharray={`${percent} 100`} /></svg><div><strong>{percent}<small>%</small></strong><span>{running ? 'Анализируем' : report ? 'На паузе' : 'Ожидает запуска'}</span></div>
      </div>
      <p role="status">{progressText}</p>
      <div className="analysis-progress-actions">{controls}{!!report?.moves.length && <button className="button primary" onClick={() => openScreen('review')}>Открыть разбор <ArrowRight size={17} /></button>}</div>
      {report?.error && <div className="error-notice" role="alert">{report.error}</div>}
      <p className="analysis-progress-note">{running ? 'Можно свернуть браузер или вернуться к списку. Анализ продолжится на сервере; результат сохранится автоматически.' : report?.analysis_outdated ? 'Сохранённые позиции доступны для просмотра. Новый расчёт обновит оценки всей партии.' : report ? 'Проверенные ходы сохранены. Вы можете продолжить анализ в любое время.' : 'Stockfish 19 проверит ходы обеих сторон, оценит позицию и найдёт лучшие продолжения. Сложные позиции могут потребовать до 30 секунд на ход.'}</p>
    </section> : report ? <>
      <div className="review-game-heading"><h1>{currentGame.white} <span>— {currentGame.black}</span></h1>{!completed && <button className="text-button" onClick={() => openScreen('progress')}>Ход анализа · {percent}% <ArrowRight size={15} /></button>}</div>
      <AnalysisReview key={report.id} report={report} selectedPly={selectedPly} onSelect={setSelectedPly} visible={visible} />
      <details className="analysis-summary panel"><summary>Отчёт о партии — точность и качество ходов</summary>
        <div className="analysis-accuracy-cards"><AccuracyCard side="white" name={currentGame.white} result={report.summary.white} completed={completed} /><AccuracyCard side="black" name={currentGame.black} result={report.summary.black} completed={completed} /></div>
        <section className="analysis-categories"><div className="panel-title"><h2>Качество ходов</h2><span>{completed ? 'Итог партии' : 'По проверенным ходам'}</span></div><div className="analysis-counts-heading"><span>Категория</span><span>Белые</span><span>Чёрные</span></div>{categories.map(item => <div className="analysis-counts-row" key={item.id}><span><b className={`analysis-category-symbol ${item.id}`}>{item.symbol}</b>{item.name}</span><strong>{report.summary.white.counts[item.id] || 0}</strong><strong>{report.summary.black.counts[item.id] || 0}</strong></div>)}<p>Вынужденные ходы и ходы без надёжной оценки не входят в расчёт точности.</p></section>
        <div className="analysis-report-actions">{controls}{safeLink(currentGame.game_url) && <a className="source-link" href={currentGame.game_url} target="_blank" rel="noreferrer">Исходная партия <ExternalLink size={13} /></a>}</div>
        <p className="analysis-method-note">Оценка Stockfish может отличаться от Chess.com. «Бриллиантовый» — приблизительная оценка корректной жертвы. Анализ не создаёт тренировочные задачи.</p>
      </details>
    </> : <div className="panel analysis-placeholder"><p>Разбор пока не рассчитан.</p><button className="button primary" onClick={() => openScreen('progress')}>Перейти к анализу</button></div>}
  </section>;
}
