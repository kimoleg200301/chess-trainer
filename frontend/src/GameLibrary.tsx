import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { ArrowRight, BookOpen, CheckCircle2, ChevronDown, Download, ExternalLink, Layers3, LoaderCircle, Pause, Play, Plus, RefreshCw, Search, Sparkles, X } from 'lucide-react';
import { api } from './api';
import { useBackgroundRefresh } from './useBackgroundRefresh';
import type { CreatedPuzzle, ImportedGame, ImportJob, ImportJobSummary, Improvement } from './api';
import './game-library.css';

const errorText = (error: unknown) => error instanceof Error ? error.message : 'Не удалось выполнить запрос. Попробуйте ещё раз.';
const jobName = (job: ImportJobSummary) => job.source === 'public' ? 'Открытые партии' : `Chess.com · ${job.username}`;
const gameState = { pending: 'Ожидает анализа', analysing: 'Анализируем', completed: 'Проверена', skipped: 'Пропущена' };
const phaseName = { opening: 'Дебют', middlegame: 'Миттельшпиль', endgame: 'Эндшпиль' };
const sourceLink = (url: string) => /^https:\/\/(?:lichess\.org|(?:www\.)?chess\.com)\//.test(url);
function dateLabel(value: string) {
  const parsed = new Date(value.replace(/^(\d{4})\.(\d{2})\.(\d{2})$/, '$1-$2-$3'));
  return Number.isNaN(parsed.getTime()) ? 'Дата не указана' : parsed.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short', year: 'numeric' });
}
function evaluation(score: number | null, mate?: number | null) {
  if (mate != null) return mate >= 0 ? `Мат в ${Math.abs(mate)}` : `Мат соперника в ${Math.abs(mate)}`;
  if (score == null) return '—';
  return `${score > 0 ? '+' : ''}${(score / 100).toFixed(2)}`;
}
function Spinner() { return <LoaderCircle size={17} className="spinner" aria-hidden="true" />; }

function ImprovementCard({ item, source, created, saving, onCreate, onPractice }: {
  item: Improvement; source: ImportJob['source']; created?: CreatedPuzzle; saving: boolean;
  onCreate: () => void; onPractice: (id: string, collection: string) => void;
}) {
  const puzzleId = item.puzzle_id || created?.puzzle_id;
  const collection = created?.collection || (source === 'public' ? 'public' : 'personal');
  return <article className="improvement-card">
    <div className="improvement-heading"><div><span className="improvement-move">{item.move_number}{item.side === 'black' ? '…' : '.'} {item.played_san}</span><span className="improvement-side">{item.side === 'white' ? 'Белые' : 'Чёрные'}</span></div><span className={`improvement-kind ${item.puzzle_available || puzzleId ? 'can-practice' : ''}`}>{item.analysis_pending ? 'Ожидает перепроверки' : puzzleId ? item.puzzle_available ? 'Готова к решению' : 'Сохранена ранее' : item.puzzle_available ? 'Подходит для задачи' : 'Момент для разбора'}</span></div>
    {item.phase && <span className="improvement-phase">{phaseName[item.phase]}</span>}
    <p>{item.analysis_pending ? 'Сохранён предыдущий результат. Этот момент будет проверен заново.' : item.explanation}</p>
    <details className="improvement-analysis"><summary>{item.played_uci === item.best_uci ? 'Посмотреть идею' : 'Посмотреть улучшение'} <ChevronDown size={14} /></summary>
      <div className="improvement-analysis-content"><div className="move-comparison"><span>Сыграно <strong>{item.played_san}</strong></span><ArrowRight size={15} /><span>{item.played_uci === item.best_uci ? 'Точный ход' : 'Лучше'} <strong>{item.best_san}</strong></span></div>
        <div className="evaluation-comparison"><span>После сыгранного: <strong>{evaluation(item.score_after, item.mate_after)}</strong></span><span>Лучший ход: <strong>{evaluation(item.score_before, item.mate_before)}</strong></span></div>
        <small>Оценка с точки зрения {item.side === 'white' ? 'белых' : 'чёрных'}.</small>
        {!!item.best_line_san?.length && <p className="candidate-line">{item.best_line_san.join('  ')}</p>}
      </div>
    </details>
    {!item.analysis_pending && !item.puzzle_available && item.puzzle_reason && puzzleId && <p className="saved-puzzle-reason">{item.puzzle_reason} Ранее сохранённую задачу можно открыть для разбора.</p>}
    <div className="improvement-actions">{puzzleId ? <button className="button secondary" onClick={() => onPractice(puzzleId, collection)}><Play size={15} /> Решить задачу</button> : item.puzzle_available ? <button className="button secondary" disabled={saving || item.analysis_pending} onClick={onCreate}>{saving ? <Spinner /> : <Plus size={15} />} Создать задачу</button> : <span className="puzzle-reason">{item.analysis_pending ? 'Причина появится после повторной проверки.' : item.puzzle_reason || 'Подробная причина пока недоступна. Пересчитайте анализ этой подборки.'}</span>}</div>
  </article>;
}

function GameCard({ game, source, running, created, creatingId, onCreate, onPractice }: {
  game: ImportedGame; source: ImportJob['source']; created: Record<number, CreatedPuzzle>;
  running: boolean;
  creatingId: number | null; onCreate: (id: number) => void; onPractice: (id: string, collection: string) => void;
}) {
  const terminal = game.status === 'completed' || game.status === 'skipped';
  const progress = terminal ? 100 : Math.min(100, game.total_plies ? game.cursor / game.total_plies * 100 : 0);
  return <article className={`imported-game ${game.candidates.length ? 'has-improvements' : ''}`}>
    <div className="imported-game-heading"><div><h3>{game.white} <span>—</span> {game.black}</h3><p>{dateLabel(game.date)} <span>·</span> {game.result}{game.event && <> <span>·</span> {game.event}</>}</p></div><span className={`game-status ${game.status}`}>{game.status === 'analysing' && running && <Spinner />}{game.status === 'analysing' && !running ? 'На паузе' : gameState[game.status]}</span></div>
    {!terminal && <div className="game-progress"><div role="progressbar" aria-label="Проверка позиций партии" aria-valuemin={0} aria-valuemax={game.total_plies || 1} aria-valuenow={Math.min(game.cursor, game.total_plies || 1)}><span style={{ width: `${progress}%` }} /></div><small>Проверено позиций: {Math.min(game.cursor, game.total_plies)} / {game.total_plies}</small></div>}
    {game.error && <p className="game-note">{game.error}</p>}
    {game.status === 'completed' && !game.candidates.length && <p className="game-note">В проверенных позициях подходящих тактических улучшений не найдено.</p>}
    {!!game.candidates.length && <div className="game-improvements">{game.candidates.map(item => <ImprovementCard key={item.id} item={item} source={source} created={created[item.id]} saving={creatingId !== null} onCreate={() => onCreate(item.id)} onPractice={onPractice} />)}</div>}
    {sourceLink(game.game_url) && <a className="source-link" href={game.game_url} target="_blank" rel="noreferrer">Открыть исходную партию <ExternalLink size={12} /></a>}
  </article>;
}

export default function GameLibrary({ visible, onChanged, onPractice }: {
  visible: boolean; onChanged: () => void; onPractice: (puzzleId: string, collection: string) => void;
}) {
  const [username, setUsername] = useState('');
  const [month, setMonth] = useState('');
  const [limit, setLimit] = useState(10);
  const [jobs, setJobs] = useState<ImportJobSummary[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [detail, setDetail] = useState<ImportJob | null>(null);
  const [listBusy, setListBusy] = useState(false);
  const [detailBusy, setDetailBusy] = useState(false);
  const [importBusy, setImportBusy] = useState<'public' | 'chesscom' | null>(null);
  const [stepBusy, setStepBusy] = useState(false);
  const [reanalyzingId, setReanalyzingId] = useState<number | null>(null);
  const [creatingId, setCreatingId] = useState<number | null>(null);
  const [created, setCreated] = useState<Record<number, CreatedPuzzle>>({});
  const [onlyImprovements, setOnlyImprovements] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const mounted = useRef(true);
  const selectedRef = useRef<number | null>(null);
  const stepFlight = useRef(false);
  const importFlight = useRef(false);
  const creatingFlight = useRef(false);
  const reanalysisFlight = useRef(false);
  const detailVersion = useRef(0);
  const detailRevision = useRef(0);
  const listVersion = useRef(0);
  const jobsRevision = useRef(0);
  const changedRef = useRef(onChanged);
  const puzzleCounts = useRef(new Map<number, number>());
  changedRef.current = onChanged;
  const upsertJob = useCallback((job: ImportJobSummary) => {
    jobsRevision.current++;
    puzzleCounts.current.set(job.id, job.puzzles_count);
    setJobs(previous => [job, ...previous.filter(item => item.id !== job.id)].sort((a, b) => b.id - a.id));
  }, []);
  const acceptJob = useCallback((result: ImportJob) => {
    const puzzlesChanged = puzzleCounts.current.get(result.id) !== result.puzzles_count;
    upsertJob(result);
    if (selectedRef.current === result.id) { detailRevision.current++; setDetail(result); }
    if (puzzlesChanged) changedRef.current();
  }, [upsertJob]);

  async function controlAnalysis(id: number, enabled: boolean) {
    if (stepFlight.current || reanalysisFlight.current) return;
    stepFlight.current = true; setStepBusy(true); setError(''); setNotice('');
    detailRevision.current++; jobsRevision.current++;
    try {
      const result = await api<ImportJob>(`/imports/${id}/${enabled ? 'resume' : 'pause'}`, {});
      if (mounted.current) acceptJob(result);
    } catch (err) { if (mounted.current) setError(errorText(err)); }
    finally { stepFlight.current = false; if (mounted.current) setStepBusy(false); }
  }
  const resumeAnalysis = (id: number) => { void controlAnalysis(id, true); };
  const pauseAnalysis = () => { if (selectedRef.current !== null) void controlAnalysis(selectedRef.current, false); };

  const selectJob = useCallback(async (id: number) => {
    selectedRef.current = id; setSelectedId(id); setDetail(null); setDetailBusy(true); setError(''); setNotice(''); setOnlyImprovements(false);
    const version = ++detailVersion.current;
    const revision = detailRevision.current;
    try {
      const result = await api<ImportJob>(`/imports/${id}`);
      if (mounted.current && version === detailVersion.current && selectedRef.current === id && revision === detailRevision.current) { detailRevision.current++; setDetail(result); upsertJob(result); }
    } catch (err) { if (mounted.current && version === detailVersion.current) setError(errorText(err)); }
    finally { if (mounted.current && version === detailVersion.current) setDetailBusy(false); }
  }, [upsertJob]);

  const loadJobs = useCallback(async () => {
    const version = ++listVersion.current;
    const revision = jobsRevision.current;
    setListBusy(true);
    try {
      const result = await api<{ jobs: ImportJobSummary[] }>('/imports');
      if (!mounted.current || version !== listVersion.current) return;
      setJobs(previous => revision === jobsRevision.current ? result.jobs : [...previous, ...result.jobs.filter(job => !previous.some(existing => existing.id === job.id))].sort((a, b) => b.id - a.id));
      if (selectedRef.current === null && result.jobs.length) void selectJob(result.jobs[0].id);
    } catch (err) { if (mounted.current && version === listVersion.current) setError(errorText(err)); }
    finally { if (mounted.current && version === listVersion.current) setListBusy(false); }
  }, [selectJob]);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  useEffect(() => { if (visible) void loadJobs(); }, [visible, loadJobs]);
  useBackgroundRefresh(async () => {
    if (stepFlight.current || importFlight.current || reanalysisFlight.current) return;
    const id = selectedRef.current, revision = detailRevision.current, selection = detailVersion.current, list = jobsRevision.current;
    try {
      const [result, current] = await Promise.all([
        api<{ jobs: ImportJobSummary[] }>('/imports'),
        id === null ? Promise.resolve(null) : api<ImportJob>(`/imports/${id}`),
      ]);
      if (!mounted.current) return;
      if (list === jobsRevision.current) setJobs(result.jobs);
      if (current && selectedRef.current === id && revision === detailRevision.current && selection === detailVersion.current) acceptJob(current);
    } catch (err) { if (mounted.current) setError(errorText(err)); }
  }, visible);

  async function beginImport(source: 'public' | 'chesscom', event?: FormEvent) {
    event?.preventDefault();
    if (importFlight.current || stepFlight.current || reanalysisFlight.current) return;
    importFlight.current = true; detailRevision.current++; jobsRevision.current++; setImportBusy(source); setError(''); setNotice('');
    try {
      const job = await api<ImportJob>(`/imports/${source}`, source === 'chesscom' ? { username: username.trim(), month: month || null, limit, background: true } : { background: true });
      if (!mounted.current) return;
      detailVersion.current++; detailRevision.current++; selectedRef.current = job.id; setSelectedId(job.id); setDetail(job); setDetailBusy(false); setOnlyImprovements(false); upsertJob(job); changedRef.current();
      if (job.analysis_outdated) setNotice('Для этой подборки доступна улучшенная проверка. Нажмите «Пересчитать анализ».');
      else if (job.status === 'completed') setNotice('Эта подборка уже обработана. Сохранённые задачи доступны в выбранной базе.');
    } catch (err) { if (mounted.current) setError(errorText(err)); }
    finally { importFlight.current = false; if (mounted.current) setImportBusy(null); }
  }
  async function createPuzzle(candidateId: number) {
    if (creatingFlight.current || reanalysisFlight.current) return;
    creatingFlight.current = true; setCreatingId(candidateId); setError('');
    try {
      const result = await api<CreatedPuzzle>(`/candidates/${candidateId}/puzzle`, {});
      if (!mounted.current) return;
      setCreated(previous => ({ ...previous, [candidateId]: result }));
      setNotice(result.created ? 'Задача добавлена в вашу базу. Её можно решить здесь или выбрать на главной.' : 'Эта задача уже есть в базе.');
      changedRef.current();
      if (selectedRef.current !== null) {
        const id = selectedRef.current;
        const version = detailVersion.current;
        const revision = detailRevision.current;
        const refreshed = await api<ImportJob>(`/imports/${id}`);
        if (mounted.current && selectedRef.current === id && version === detailVersion.current && revision === detailRevision.current) { detailRevision.current++; setDetail(refreshed); upsertJob(refreshed); }
      }
    } catch (err) { if (mounted.current) setError(errorText(err)); }
    finally { creatingFlight.current = false; if (mounted.current) setCreatingId(null); }
  }
  async function reanalyzeJob(id: number) {
    if (reanalysisFlight.current || stepFlight.current || importFlight.current || creatingFlight.current) return;
    reanalysisFlight.current = true;
    detailRevision.current++; jobsRevision.current++; setReanalyzingId(id); setError(''); setNotice('');
    const version = ++detailVersion.current;
    try {
      const job = await api<ImportJob>(`/imports/${id}/reanalyze`, { background: true });
      if (!mounted.current) return;
      upsertJob(job); setCreated({}); changedRef.current();
      if (selectedRef.current === id && version === detailVersion.current) {
        detailRevision.current++; setDetail(job); setOnlyImprovements(false);
        reanalysisFlight.current = false;
      }
    } catch (err) { if (mounted.current) setError(errorText(err)); }
    finally { reanalysisFlight.current = false; if (mounted.current) setReanalyzingId(null); }
  }
  function practice(id: string, collection: string) { onPractice(id, collection); }

  const running = !!detail?.background_requested && detail.status !== 'completed' && detail.status !== 'failed';
  const displayedGames = detail?.games.filter(game => !onlyImprovements || game.candidates.length > 0) || [];
  const readyMoments = detail?.available_puzzles_count ?? detail?.games.reduce((sum, game) => sum + game.candidates.filter(item => item.puzzle_available && !item.analysis_pending).length, 0) ?? 0;
  const positions = detail?.games.reduce((sum, game) => sum + game.total_plies, 0) || 0;
  const checked = detail?.games.reduce((sum, game) => sum + (game.status === 'completed' || game.status === 'skipped' ? game.total_plies : Math.min(game.cursor, game.total_plies)), 0) || 0;

  return <section className="game-library">
    <div className="page-heading"><div><span className="eyebrow">ИЗ ПАРТИИ В ПРАКТИКУ</span><h1>Найдите свои тактические возможности</h1><p>Разбирайте реальные партии. Подходящие задачи сохраняются автоматически.</p></div><span className="library-heading-icon"><BookOpen size={27} /></span></div>
    <div className="library-import-panels">
      <form className="panel chesscom-import" onSubmit={event => void beginImport('chesscom', event)}><div className="panel-title"><h2>Партии Chess.com</h2><Download size={19} /></div><p className="library-panel-copy">Загрузите публичные завершённые партии по нику. Пароль не нужен.</p>
        <label htmlFor="chesscom-username">Ник на Chess.com</label><input id="chesscom-username" value={username} onChange={event => setUsername(event.target.value)} placeholder="Ваш ник" autoCapitalize="none" autoCorrect="off" spellCheck={false} maxLength={40} required disabled={importBusy !== null || reanalyzingId !== null} />
        <div className="import-options"><label htmlFor="chesscom-month">Месяц <span>необязательно</span><input id="chesscom-month" type="month" max={new Date().toISOString().slice(0, 7)} value={month} onChange={event => setMonth(event.target.value)} disabled={importBusy !== null || reanalyzingId !== null} /></label><label htmlFor="chesscom-limit">Партий за раз<input id="chesscom-limit" type="number" min={1} max={50} step={1} value={limit} onChange={event => setLimit(Number(event.target.value))} required disabled={importBusy !== null || reanalyzingId !== null} /></label></div>
        <p className="import-small-note">Без месяца загрузим последний доступный архив. Анализируем ваши ходы и автоматически сохраняем подходящие задачи.</p>
        <button className="button primary" disabled={importBusy !== null || stepBusy || reanalyzingId !== null || !username.trim()}>{importBusy === 'chesscom' ? <Spinner /> : <Search size={16} />}{importBusy === 'chesscom' ? 'Загружаем партии…' : 'Загрузить и найти улучшения'}</button>
        <p className="chesscom-attribution">Независимый тренажёр, не связан с Chess.com.</p>
      </form>
      <section className="panel public-import"><span className="public-import-icon"><Layers3 size={25} /></span><h2>Своя база из открытых партий</h2><p>Stockfish проверит подборку реальных партий и отберёт тактические моменты для отдельной базы.</p><div className="public-import-steps"><span><CheckCircle2 size={15} /> Позиции из реальных игр</span><span><CheckCircle2 size={15} /> Проверка продолжений движком</span><span><CheckCircle2 size={15} /> Готовые задачи в базе «Открытые партии»</span></div><button className="button secondary" disabled={importBusy !== null || stepBusy || reanalyzingId !== null} onClick={() => void beginImport('public')}>{importBusy === 'public' ? <Spinner /> : <Sparkles size={16} />}{importBusy === 'public' ? 'Открываем подборку…' : 'Создать задачи из открытых партий'}</button><p className="import-small-note">Найденные подходящие задачи добавляются автоматически. Повторная обработка не создаёт дубликаты.</p></section>
    </div>

    {error && <div className="error-notice" role="alert"><span>{error}</span><button aria-label="Закрыть ошибку" onClick={() => setError('')}><X size={16} /></button></div>}
    {notice && <div className="library-notice" role="status"><CheckCircle2 size={17} /><span>{notice}</span><button className="icon-button" aria-label="Закрыть уведомление" onClick={() => setNotice('')}><X size={15} /></button></div>}

    <div className="library-workspace"><aside className="panel import-history"><div className="panel-title"><h2>Ваши подборки</h2><button className="icon-button" aria-label="Обновить список подборок" disabled={listBusy} onClick={() => void loadJobs()}>{listBusy ? <Spinner /> : <RefreshCw size={16} />}</button></div>
      {!jobs.length ? <p className="library-empty">{listBusy ? 'Загружаем подборки…' : 'Здесь появятся загруженные партии и результаты их анализа.'}</p> : <div className="import-history-list">{jobs.map(job => <button key={job.id} className={selectedId === job.id ? 'selected' : ''} aria-pressed={selectedId === job.id} onClick={() => void selectJob(job.id)}><strong>{jobName(job)}</strong><small>{job.month || dateLabel(job.created_at)} · {job.total_games} партий</small><span>{job.status === 'completed' ? 'Готово' : job.status === 'failed' ? 'Требует внимания' : job.background_requested ? 'Анализируем' : 'Можно продолжить'} <span>·</span> {job.candidates_count} моментов</span></button>)}</div>}
    </aside>
    <div className="library-results" aria-busy={detailBusy}>
      {detailBusy ? <div className="panel library-placeholder"><Spinner /><p>Открываем результаты…</p></div> : !detail ? <div className="panel library-placeholder"><BookOpen size={29} /><h2>Начните с нескольких партий</h2><p>В списке появятся улучшения и готовые задачи для тренировки.</p></div> : <>
        <section className="panel import-progress-panel"><div className="import-detail-heading"><div><h2>{jobName(detail)}</h2><p>{detail.source === 'public' ? 'Тактические возможности за обе стороны.' : `Улучшения в ходах ${detail.username}.`}</p></div>{detail.analysis_outdated ? <span className="import-complete">Доступен пересчёт</span> : detail.status === 'completed' ? <span className="import-complete"><CheckCircle2 size={17} /> Готово</span> : running ? <button className="button secondary" disabled={stepBusy} onClick={pauseAnalysis}><Pause size={15} /> Пауза</button> : <button className="button secondary" disabled={stepBusy || importBusy !== null || reanalyzingId !== null} onClick={() => resumeAnalysis(detail.id)}>{stepBusy ? <Spinner /> : <Play size={15} />}{stepBusy ? 'Завершаем проверку…' : detail.status === 'failed' ? 'Повторить анализ' : 'Продолжить анализ'}</button>}</div>
          <div className="import-progress-track" role="progressbar" aria-label="Анализ подборки" aria-valuemin={0} aria-valuemax={positions || 1} aria-valuenow={Math.min(checked, positions || 1)}><span style={{ width: `${positions ? Math.min(100, checked / positions * 100) : detail.status === 'completed' ? 100 : 0}%` }} /></div>
          <div className="import-totals"><span><strong>{detail.completed_games} / {detail.total_games}</strong> партий обработано</span><span><strong>{detail.candidates_count}</strong> моментов найдено</span><span><strong>{readyMoments}</strong> подходящих моментов</span><span><strong>{detail.puzzles_count}</strong> задач из этих партий</span></div>
          <p className="import-small-note auto-save-note"><CheckCircle2 size={13} /> Подходящие задачи сохраняются автоматически и доступны для решения сразу.</p>
          {detail.analysis_outdated && <p className="analysis-update-note">Для этой подборки доступна улучшенная проверка. Пересчитайте анализ, чтобы обновить выводы и найти подходящие задачи.</p>}
          <div className="reanalyze-actions"><button className="button secondary" disabled={running || stepBusy || importBusy !== null || reanalyzingId !== null || creatingId !== null} onClick={() => void reanalyzeJob(detail.id)}>{reanalyzingId === detail.id ? <Spinner /> : <RefreshCw size={14} />}{reanalyzingId === detail.id ? 'Готовим пересчёт…' : 'Пересчитать анализ'}</button><span>Задачи и прогресс тренировок сохранятся.</span></div>
          {detail.status !== 'completed' && !detail.analysis_outdated && <p className="import-small-note">{running ? 'Анализ продолжается в фоне. Можно свернуть браузер или перейти к тренировке.' : stepBusy ? 'Текущая проверка завершится и сохранится. Новые позиции пока не анализируем.' : 'Прогресс сохранён. Продолжите анализ, когда будет удобно.'}</p>}
          {detail.error && <p className="game-note">{detail.error}</p>}
          <label className="improvement-filter"><input type="checkbox" checked={onlyImprovements} onChange={event => setOnlyImprovements(event.target.checked)} /> Только партии с найденными моментами</label>
        </section>
        {!displayedGames.length ? <div className="panel library-empty">{onlyImprovements ? 'В этой подборке пока нет партий с найденными моментами.' : 'Подходящих партий в подборке пока нет.'}</div> : <div className="imported-games">{displayedGames.map(game => <GameCard key={game.id} game={game} source={detail.source} running={running || stepBusy} created={created} creatingId={reanalyzingId !== null ? -1 : creatingId} onCreate={id => void createPuzzle(id)} onPractice={practice} />)}</div>}
      </>}
    </div></div>
    <p className="library-method-note">Не каждое улучшение становится задачей. У каждого момента показан результат проверки; сложность созданных задач приблизительная.</p>
  </section>;
}
