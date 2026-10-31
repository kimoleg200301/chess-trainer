import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { ArrowRight, Check, CheckCircle2, ChevronRight, CircleHelp, Download, Flame, GraduationCap, Lightbulb, LoaderCircle, LogOut, Pause, Play, RotateCcw, Settings2, ShieldCheck, Sparkles, Target, TrendingUp, X, Zap } from 'lucide-react';
import ChessBoard from './ChessBoard';
import { Chess } from 'chess.js';
import { api, ApiError } from './api';
import type { Analysis, Attempt, Meta, Reveal, Stats } from './api';

const fmt = new Intl.NumberFormat('ru-RU');
const themeFallback: Record<string, string> = { fork: 'Вилка', pin: 'Связка', skewer: 'Сквозное нападение', discoveredAttack: 'Вскрытое нападение', deflection: 'Отвлечение', attraction: 'Завлечение', sacrifice: 'Жертва', mate: 'Мат', mateIn1: 'Мат в один ход', mateIn2: 'Мат в два хода', backRankMate: 'Мат по последней горизонтали', hangingPiece: 'Незащищённая фигура', crushing: 'Решающее преимущество', advantage: 'Преимущество', middlegame: 'Миттельшпиль', endgame: 'Эндшпиль', opening: 'Дебют', short: 'Короткий вариант', long: 'Длинный вариант', veryLong: 'Длинный вариант', quietMove: 'Тихий ход', defensiveMove: 'Защитный ход', intermezzo: 'Промежуточный ход' };
const errorText = (error: unknown) => error instanceof Error ? error.message : 'Что-то пошло не так. Попробуйте ещё раз.';

function Brand({ subtitle = false }: { subtitle?: boolean }) {
  return <div className="brand"><img src="/favicon.svg" alt="" /><div><span>тактика<span className="brand-dot">.</span></span>{subtitle && <small>Ваш следующий сильный ход</small>}</div></div>;
}
function Spinner() { return <LoaderCircle size={18} className="spinner" aria-hidden="true" />; }
function ErrorNotice({ message, close }: { message: string; close?: () => void }) { return <div className="error-notice" role="alert"><span>{message}</span>{close && <button aria-label="Закрыть сообщение" onClick={close}><X size={16} /></button>}</div>; }
function Empty({ icon, title, children }: { icon: ReactNode; title: string; children: ReactNode }) { return <div className="empty-state"><span className="empty-icon">{icon}</span><h3>{title}</h3><p>{children}</p></div>; }

function Login({ onLogin, initialError }: { onLogin: (name: string) => void; initialError: string }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(initialError);
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    try { const result = await api<{ username: string }>('/auth/login', { username, password }); onLogin(result.username); }
    catch (err) { setError(errorText(err)); } finally { setBusy(false); }
  }
  return <main className="login-page">
    <div className="login-top"><Brand /></div>
    <section className="login-layout">
      <div className="login-story"><span className="eyebrow"><span className="tiny-dot" /> ШАХМАТНАЯ ПРАКТИКА</span><h1>Учитесь видеть<br />сильные ходы.</h1><p>Одна позиция. Одна идея.<br />Больше уверенности за доской.</p>
        <div className="login-board-art" aria-hidden="true"><div className="art-square art-a"><img src="/pieces/wN.svg" alt="" /></div><div className="art-square art-b" /><div className="art-square art-c" /><div className="art-square art-d"><img src="/pieces/bK.svg" alt="" /></div><span className="art-dash" /></div>
        <div className="login-points"><span><CheckCircle2 size={17} /> Мотивы из реальных партий</span><span><RotateCcw size={17} /> Повторение трудных позиций</span></div>
      </div>
      <div className="login-card"><span className="label-small">ЛИЧНЫЙ ТРЕНАЖЁР</span><h2>Вернёмся к практике</h2><p className="muted">Войдите, чтобы сохранять свой прогресс.</p>
        <form onSubmit={submit}><label>Имя пользователя<input autoComplete="username" value={username} onChange={event => setUsername(event.target.value)} placeholder="Ваш логин" required autoFocus /></label><label>Пароль<input type="password" autoComplete="current-password" value={password} onChange={event => setPassword(event.target.value)} placeholder="Введите пароль" required /></label>{error && <ErrorNotice message={error} />}<button className="button primary login-submit" disabled={busy}>{busy ? <Spinner /> : <>Начать практику <ArrowRight size={18} /></>}</button></form>
        <div className="login-foot"><ShieldCheck size={17} /><span>Личный доступ. Ваши задачи и прогресс<br />сохраняются в аккаунте.</span></div>
      </div>
    </section>
    <footer className="login-bottom">Немного практики каждый день — больше знакомых идей в каждой партии.</footer>
  </main>;
}

function StatsPage({ stats, busy, refresh }: { stats: Stats | null; busy: boolean; refresh: () => void }) {
  const [exportBusy, setExportBusy] = useState(false);
  const [exportError, setExportError] = useState('');
  const [exportReady, setExportReady] = useState(false);
  async function exportProgress() {
    setExportBusy(true); setExportError(''); setExportReady(false);
    try {
      const progress = await api<unknown>('/progress/export');
      const blob = new Blob([JSON.stringify(progress, null, 2)], { type: 'application/json;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url; link.download = `tactics-progress-${new Date().toISOString().slice(0, 10)}.json`;
      document.body.appendChild(link); link.click(); link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      setExportReady(true);
    } catch (err) { setExportError(errorText(err)); } finally { setExportBusy(false); }
  }
  return <section className="stats-page"><div className="page-heading"><div><span className="eyebrow">ВАША ПРАКТИКА</span><h1>Прогресс складывается из ходов</h1><p>Следите за результатами и возвращайтесь к тому, что требует внимания.</p></div><div className="heading-actions"><button className="button secondary" disabled={exportBusy} onClick={exportProgress}>{exportBusy ? <Spinner /> : <Download size={16} />} {exportBusy ? 'Готовим файл…' : 'Скачать прогресс'}</button><button className="button secondary" disabled={busy} onClick={refresh}>{busy ? <Spinner /> : <RotateCcw size={16} />} Обновить</button></div></div>
    {exportError && <ErrorNotice message={exportError} close={() => setExportError('')} />}
    {exportReady && <div className="export-success" role="status"><CheckCircle2 size={16} /><span>Скачивание началось. Сохраните JSON-файл как резервную копию прогресса.</span><button className="icon-button" aria-label="Закрыть сообщение" onClick={() => setExportReady(false)}><X size={14} /></button></div>}
    {!stats ? <Empty icon={<TrendingUp />} title={busy ? 'Загружаем статистику' : 'Статистика пока недоступна'}>Здесь появятся результаты ваших тренировок.</Empty> : <>
      <div className="stat-cards">{[
        { icon: <CheckCircle2 />, value: fmt.format(stats.solved), name: 'Задач решено', detail: 'С найденным продолжением' },
        { icon: <Target />, value: `${Math.round(stats.accuracy)}%`, name: 'Точность', detail: 'Без ошибок и подсказок' },
        { icon: <Flame />, value: stats.streak, name: 'Текущая серия', detail: 'Чистых решений подряд' },
        { icon: <RotateCcw />, value: stats.due_count, name: 'К повторению', detail: 'Закрепите знакомые идеи' },
      ].map(card => <div className="stat-card" key={card.name}><span>{card.icon}</span><strong>{card.value}</strong><h3>{card.name}</h3><p>{card.detail}</p></div>)}</div>
      <div className="stats-columns"><section className="panel"><div className="panel-title"><h2>Тактические мотивы</h2><GraduationCap size={19} /></div>{!stats.by_theme.length ? <Empty icon={<Target />} title="Первый шаг впереди">Решите несколько задач, чтобы увидеть результаты по мотивам.</Empty> : <div className="theme-stats">{stats.by_theme.map(theme => <div className="theme-stat" key={theme.id}><div><span>{theme.name || themeFallback[theme.id] || theme.id}</span><small>{theme.solved} / {theme.attempts}</small></div><div className="progress-track"><span style={{ width: `${theme.attempts ? Math.min(100, theme.solved / theme.attempts * 100) : 0}%` }} /></div></div>)}</div>}</section>
        <section className="panel"><div className="panel-title"><h2>Последние позиции</h2><span className="subtle-count">{fmt.format(stats.total_attempts)} попыток</span></div>{!stats.recent.length ? <Empty icon={<Sparkles />} title="Начните свою историю">Каждая разобранная позиция останется в истории практики.</Empty> : <div className="recent-list">{stats.recent.map((item, index) => <div className="recent-item" key={`${item.puzzle_id}-${index}`}><span className={`recent-icon ${item.status === 'completed' ? 'success' : ''}`}>{item.status === 'completed' ? <Check size={18} /> : item.status === 'revealed' ? <CircleHelp size={18} /> : <Target size={18} />}</span><div><strong>Позиция #{item.puzzle_id}</strong><small>Рейтинг {item.rating} · {new Date(item.created_at).toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' })}</small></div><span className="recent-status">{item.status === 'completed' ? 'Решена' : item.status === 'revealed' ? 'Разобрана' : 'В процессе'}</span></div>)}</div>}</section></div>
      <p className="stats-note">Точность считается по завершённым попыткам. Решение с подсказкой, ошибкой или просмотром ответа не считается чистым.</p>
    </>}
  </section>;
}

function Trainer({ meta, stats, onChanged, visible }: { meta: Meta | null; stats: Stats | null; onChanged: () => void; visible: boolean }) {
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [theme, setTheme] = useState('');
  const [minRating, setMinRating] = useState(800);
  const [maxRating, setMaxRating] = useState(1800);
  const [mode, setMode] = useState<'practice' | 'review'>('practice');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [feedback, setFeedback] = useState('');
  const [feedbackKind, setFeedbackKind] = useState<'neutral' | 'success' | 'wrong'>('neutral');
  const [hintSquare, setHintSquare] = useState<string | null>(null);
  const [reveal, setReveal] = useState<Reveal | null>(null);
  const [initialFen, setInitialFen] = useState<string | null>(null);
  const [replayIndex, setReplayIndex] = useState(0);
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [analysisBusy, setAnalysisBusy] = useState(false);
  const [sessionSolved, setSessionSolved] = useState(0);
  const [showSkip, setShowSkip] = useState(false);
  const [phase, setPhase] = useState<'idle' | 'checking' | 'reply'>('idle');
  const [playback, setPlayback] = useState<{ fen: string; last_move: string } | null>(null);
  const [replayPlaying, setReplayPlaying] = useState(false);
  const [feedbackVersion, setFeedbackVersion] = useState(0);
  const actionPending = useRef(false);
  const mounted = useRef(true);
  const boardColumn = useRef<HTMLDivElement>(null);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => { if (!visible) setReplayPlaying(false); }, [visible]);
  useEffect(() => {
    if (!replayPlaying || !reveal || !visible) return;
    if (replayIndex >= reveal.solution.length) { setReplayPlaying(false); return; }
    const timer = window.setTimeout(() => setReplayIndex(index => index + 1), 950);
    return () => window.clearTimeout(timer);
  }, [replayPlaying, replayIndex, reveal, visible]);
  function seekReplay(index: number) { setReplayPlaying(false); setReplayIndex(index); }
  function toggleReplay() {
    if (reveal && replayIndex >= reveal.solution.length) setReplayIndex(0);
    setReplayPlaying(value => !value);
  }
  const pause = (ms: number) => new Promise<void>(resolve => window.setTimeout(resolve, ms));
  const label = (id: string) => meta?.themes.find(item => item.id === id)?.name || themeFallback[id] || id;
  const done = !!attempt && attempt.status !== 'active';
  const boardAttempt = useMemo(() => {
    if (attempt && playback) return { ...attempt, ...playback };
    if (!attempt || !reveal || !initialFen) return attempt;
    const board = new Chess(initialFen);
    for (const item of reveal.solution.slice(0, replayIndex)) board.move(item.uci);
    return { ...attempt, fen: board.fen(), last_move: replayIndex ? reveal.solution[replayIndex - 1].uci : null };
  }, [attempt, reveal, initialFen, replayIndex, playback]);

  async function start() {
    if (actionPending.current || analysisBusy) return;
    if (minRating > maxRating) { setError('Нижняя граница рейтинга должна быть не выше верхней.'); return; }
    actionPending.current = true;
    setShowSkip(false); setReplayPlaying(false); setBusy(true); setError('');
    try {
      const data = await api<Attempt>('/training/start', { theme: theme || null, min_rating: minRating, max_rating: maxRating, mode });
      if (!mounted.current) return;
      setAttempt(data); setInitialFen(data.fen); setReplayIndex(0); setHintSquare(null); setReveal(null); setAnalysis(null); setFeedback(''); setFeedbackKind('neutral'); setPlayback(null);
      if (window.matchMedia('(max-width: 650px)').matches) boardColumn.current?.scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'start' });
    } catch (err) { if (mounted.current) setError(errorText(err)); }
    finally { actionPending.current = false; if (mounted.current) setBusy(false); }
  }
  async function move(uci: string) {
    if (!attempt || attempt.status !== 'active' || actionPending.current) return;
    actionPending.current = true;
    setBusy(true); setPhase('checking'); setError(''); setHintSquare(null); setShowSkip(false);
    try {
      const result = await api<{ accepted: boolean; attempt: Attempt; reply_move: string | null; feedback: string }>(`/training/${attempt.id}/move`, { move: uci });
      if (!mounted.current) return;
      const animate = !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      // The server has already accepted both moves. Show them separately for readability.
      if (result.accepted && result.reply_move && animate) {
        const intermediate = new Chess(attempt.fen);
        intermediate.move(uci);
        setPlayback({ fen: intermediate.fen(), last_move: uci }); setPhase('reply');
        await pause(550);
        if (!mounted.current) return;
      }
      setAttempt(result.attempt); setPlayback(null); setPhase('idle');
      setFeedback(result.feedback); setFeedbackKind(result.accepted ? 'success' : 'wrong');
      setFeedbackVersion(value => value + 1);
      if (result.accepted && animate) await pause(240);
      if (!mounted.current) return;
      if (result.attempt.status === 'completed') { setSessionSolved(value => value + 1); onChanged(); }
    } catch (err) { if (mounted.current) { setPlayback(null); setError(errorText(err)); } }
    finally { actionPending.current = false; if (mounted.current) { setBusy(false); setPhase('idle'); } }
  }
  async function hint() {
    if (!attempt || attempt.status !== 'active' || actionPending.current) return;
    actionPending.current = true; setBusy(true); setError(''); setShowSkip(false);
    try {
      const result = await api<{ message: string; from_square: string | null; attempt: Attempt }>(`/training/${attempt.id}/hint`, {});
      if (!mounted.current) return;
      setAttempt(result.attempt); setHintSquare(result.from_square); setFeedback(result.message); setFeedbackKind('neutral'); setFeedbackVersion(value => value + 1);
    } catch (err) { if (mounted.current) setError(errorText(err)); }
    finally { actionPending.current = false; if (mounted.current) setBusy(false); }
  }
  async function revealSolution() {
    if (!attempt || attempt.status !== 'active' || actionPending.current) return;
    actionPending.current = true; setBusy(true); setError(''); setShowSkip(false);
    try {
      const result = await api<Reveal>(`/training/${attempt.id}/reveal`, {});
      if (!mounted.current) return;
      setAttempt(result.attempt); setReplayIndex(0); setReplayPlaying(false); setReveal(result); setHintSquare(null); setFeedback('Просмотрите вариант по шагам или включите воспроизведение.'); setFeedbackKind('neutral'); onChanged();
    } catch (err) { if (mounted.current) setError(errorText(err)); }
    finally { actionPending.current = false; if (mounted.current) setBusy(false); }
  }
  async function getAnalysis() {
    if (!attempt || actionPending.current) return;
    actionPending.current = true; setAnalysisBusy(true); setError('');
    try { const result = await api<Analysis>(`/training/${attempt.id}/analysis`); if (mounted.current) setAnalysis(result); }
    catch (err) { if (mounted.current) setError(errorText(err)); }
    finally { actionPending.current = false; if (mounted.current) setAnalysisBusy(false); }
  }
  const statusTitle = phase === 'reply' ? 'Соперник отвечает…' : phase === 'checking' && !done ? 'Проверяем ход…'
    : attempt?.status === 'completed' ? 'Идея найдена!' : reveal ? 'Разбор позиции'
    : feedbackKind === 'wrong' ? 'Попробуйте ещё' : hintSquare ? 'Подсказка на доске'
    : feedbackKind === 'success' ? 'Верный ход. Продолжайте!' : 'Ваш ход';
  const statusDetail = phase === 'reply' ? 'Ход принят. Следите за ответом на доске.' : phase === 'checking' && !done ? 'Сверяем продолжение…'
    : attempt?.status === 'completed' ? attempt.mistakes === 0 && attempt.hints_used === 0 ? 'Чистое решение — без ошибок и подсказок.' : 'Комбинация найдена. Повторение поможет закрепить идею.'
    : feedback || `Играйте за ${attempt?.turn === 'black' ? 'чёрных' : 'белых'}. Выберите фигуру и поле назначения.`;
  const statusKind = phase !== 'idle' && !done ? 'thinking' : attempt?.status === 'completed' ? 'success' : feedbackKind;
  return <section>
    <div className="page-heading"><div><span className="eyebrow">ТРЕНИРУЙТЕ ШАХМАТНОЕ ЗРЕНИЕ</span><h1>У каждой позиции есть идея.</h1><p>Заметьте мотив. Рассчитайте продолжение. Сделайте свой ход.</p></div><div className="session-summary"><span className="session-icon"><Zap size={19} /></span><div><strong>{sessionSolved} решено</strong><small>в этой тренировке</small></div></div></div>
    <div className="training-layout"><div ref={boardColumn} className={`board-column ${attempt?.status === 'completed' ? 'board-solved' : ''}`}>
      <div className="board-topline"><div><span className={`side-indicator ${attempt?.turn === 'black' ? 'black-side' : ''}`} /><strong>{attempt ? `${attempt.turn === 'white' ? 'Белые' : 'Чёрные'} ${done ? '· позиция разобрана' : '· ваша сторона'}` : 'Готовы к следующему ходу?'}</strong></div><span className="puzzle-caption">{attempt ? `№ ${attempt.puzzle_id} · ${attempt.rating}` : 'Выберите параметры справа'}</span></div>
      {attempt && <div className={`play-status ${statusKind}`} role="status" aria-live="polite" aria-atomic="true">
        <span key={`${attempt.id}-${feedbackVersion}-${phase}`} className="play-status-icon">{phase !== 'idle' && !done ? <Spinner /> : attempt.status === 'completed' || feedbackKind === 'success' ? <CheckCircle2 size={23} /> : feedbackKind === 'wrong' ? <RotateCcw size={22} /> : hintSquare || reveal ? <Lightbulb size={22} /> : <Target size={22} />}</span>
        <div><strong>{statusTitle}</strong><p>{statusDetail}</p></div>
      </div>}
      <ChessBoard key={attempt?.id ?? 'initial'} attempt={boardAttempt} busy={busy} hintSquare={hintSquare} onMove={move} />
      {attempt && <div className="board-actions">
        <button className="button primary next-position-button" aria-label="Следующая позиция" disabled={busy || analysisBusy || !meta} onClick={() => attempt.status === 'active' ? setShowSkip(true) : void start()}>
          <span>Следующая позиция</span>{busy ? <Spinner /> : <ArrowRight size={17} />}
        </button>
        {showSkip && <div className="skip-confirm" role="alert"><p>Перейти к новой позиции? Эта попытка останется незавершённой.</p><div><button className="text-button" onClick={() => setShowSkip(false)}>Остаться</button><button className="text-button" onClick={() => void start()}>Перейти <ArrowRight size={14} /></button></div></div>}
      </div>}
      {attempt && !done && <div className="board-tools" aria-label="Помощь в решении">
        <button className="button secondary" disabled={busy} onClick={hint}><Lightbulb size={17} /> Подсказка</button>
        <button className="button secondary" disabled={busy} onClick={revealSolution}><CircleHelp size={17} /> Решение</button>
      </div>}
      {reveal && <div className="board-replay">
        <div className="replay-toolbar"><span className="label-small">РАЗБОР НА ДОСКЕ</span><button className="button secondary replay-play" disabled={busy} onClick={toggleReplay}>{replayPlaying ? <Pause size={16} /> : <Play size={16} />}{replayPlaying ? 'Пауза' : 'Воспроизвести'}</button></div>
        <div className="solution-moves">{reveal.solution.map((item, index) => <button key={`${index}-${item.uci}`} aria-label={`Показать позицию после ${item.san}`} aria-pressed={replayIndex === index + 1} onClick={() => seekReplay(index + 1)} className={`${index % 2 === 0 ? 'learner-move' : ''} ${replayIndex === index + 1 ? 'current-move' : ''}`}>{item.san}</button>)}</div>
        <div className="replay-controls"><button className="text-button" disabled={replayIndex === 0} onClick={() => seekReplay(0)}>В начало</button><div><button className="text-button" disabled={replayIndex === 0} onClick={() => seekReplay(replayIndex - 1)}>← Назад</button><button className="text-button" disabled={replayIndex === reveal.solution.length} onClick={() => seekReplay(replayIndex + 1)}>Далее →</button></div></div>
      </div>}
      {attempt && error && <ErrorNotice message={error} close={() => setError('')} />}
      <div className="board-bottomline"><span><span className="connection-dot" />{reveal ? 'Выбирайте ходы варианта для просмотра на доске' : attempt && !done ? 'Нажмите на фигуру и поле назначения' : done ? 'Возьмите идею с собой в следующую партию' : 'Реальные позиции · спокойная практика'}</span></div>
      <div className="learning-note"><Lightbulb size={20} /><p><strong>Сначала идея, потом ход.</strong> Перед ответом проверьте шахи, взятия и угрозы. Постарайтесь увидеть лучший ответ соперника.</p></div>
    </div>
    <aside className="training-sidebar">
      <section className="panel settings-panel"><div className="panel-title"><h2>Ваша тренировка</h2><Settings2 size={18} /></div>
        <div className="segmented" aria-label="Режим тренировки"><button aria-pressed={mode === 'practice'} className={mode === 'practice' ? 'active' : ''} onClick={() => setMode('practice')}><Target size={15} />Практика</button><button aria-pressed={mode === 'review'} className={mode === 'review' ? 'active' : ''} onClick={() => setMode('review')}><RotateCcw size={15} />Повторение{(stats?.due_count || 0) > 0 && <span className="count-badge">{stats?.due_count}</span>}</button></div>
        <label className="field-label" htmlFor="theme">Тактический мотив</label><select id="theme" value={theme} onChange={event => setTheme(event.target.value)} disabled={!meta}><option value="">Смешанные задачи</option>{meta?.themes.map(item => <option key={item.id} value={item.id}>{item.name} ({item.count})</option>)}</select>
        <div className="difficulty-label"><label className="field-label" htmlFor="rating-min">Рейтинг задач</label><span>{minRating}–{maxRating}</span></div>
        <div className="rating-fields"><label><span>От</span><input id="rating-min" type="number" min="0" max="4000" step="100" value={minRating} onChange={event => setMinRating(Number(event.target.value))} /></label><span className="rating-dash">—</span><label><span>До</span><input aria-label="Верхняя граница рейтинга" type="number" min="0" max="4000" step="100" value={maxRating} onChange={event => setMaxRating(Number(event.target.value))} /></label></div>
        <div className="difficulty-presets">{[{ label: 'Разминка', min: 600, max: 1200 }, { label: 'Практика', min: 1200, max: 1800 }, { label: 'Вызов', min: 1800, max: 2400 }].map(preset => <button key={preset.label} className={minRating === preset.min && maxRating === preset.max ? 'selected-preset' : ''} onClick={() => { setMinRating(preset.min); setMaxRating(preset.max); }}>{preset.label}</button>)}</div>
        {!attempt && error && <ErrorNotice message={error} close={() => setError('')} />}
        {!attempt && <button className="button primary start-button" disabled={busy || analysisBusy || !meta} onClick={() => void start()}>{busy ? <Spinner /> : <><span>Начать тренировку</span><ArrowRight size={17} /></>}</button>}
        <p className="settings-footnote">{mode === 'review' ? 'Позиции, которые пора повторить. Фильтры также применяются.' : meta ? `${fmt.format(meta.total_puzzles)} позиций в библиотеке. Параметры применяются к следующей задаче.` : 'Загружаем библиотеку задач…'}</p>
      </section>
      <section className={`panel position-panel ${attempt?.status === 'completed' ? 'completed-panel' : ''}`}>
        {!attempt ? <Empty icon={<Target size={25} />} title="Точность важнее скорости">Выберите мотив или смешанные задачи. Начните с удобного уровня сложности.</Empty> : <>
          <div className="panel-title"><h2>{attempt.status === 'completed' ? 'Идея найдена!' : attempt.status === 'revealed' ? 'Разбор позиции' : 'Найдите продолжение'}</h2>{attempt.status === 'completed' ? <CheckCircle2 size={21} /> : <Target size={19} />}</div>
          <p className="position-description">{attempt.status === 'completed' ? attempt.mistakes === 0 && attempt.hints_used === 0 ? 'Чистое решение. Ещё один знакомый мотив в вашей коллекции.' : 'Вы нашли продолжение. Повторение поможет закрепить эту идею.' : attempt.status === 'revealed' ? 'Проследите за каждым ответом соперника.' : `Ваш ход за ${attempt.turn === 'white' ? 'белых' : 'чёрных'}. Рассчитайте вариант до понятного результата.`}</p>
          <div className="move-progress" aria-label="Прогресс решения">{Array.from({ length: Math.max(1, Math.ceil(attempt.total_plies / 2)) }, (_, index) => <span key={index} className={index < Math.ceil(attempt.solved_plies / 2) ? 'filled' : ''} />)}</div>
          {(attempt.mistakes > 0 || attempt.hints_used > 0) && <p className="attempt-meta">Ошибок: {attempt.mistakes} · Подсказок: {attempt.hints_used}</p>}
          {done && <div className="theme-tags">{attempt.themes.filter(id => !['short', 'long', 'veryLong', 'oneMove'].includes(id) && label(id) !== id).slice(0, 5).map(id => <span key={id}>{label(id)}</span>)}</div>}
          {reveal && /^https:\/\/lichess\.org\//.test(reveal.game_url) && <a className="source-link" href={reveal.game_url} target="_blank" rel="noreferrer">Посмотреть исходную партию <ChevronRight size={14} /></a>}
          {attempt.status === 'completed' && !analysis && <button className="analysis-button" disabled={busy || analysisBusy} onClick={getAnalysis}>{analysisBusy ? <Spinner /> : <Sparkles size={15} />}{analysisBusy ? 'Анализируем позицию…' : 'Проверить итоговую позицию'}</button>}
          {analysis && <div className="analysis-result"><div><span>{analysis.engine}</span><strong>{analysis.evaluation}</strong></div><p>{analysis.line.length ? analysis.line.join('  ') : 'Продолжение не требуется.'}</p></div>}
        </>}
      </section>
      <div className="sidebar-bottom"><GraduationCap size={18} /><p>Повторяйте мотивы, чтобы узнавать их<br />в собственных партиях.</p></div>
    </aside></div>
  </section>;
}

export default function App() {
  const [user, setUser] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState<'training' | 'stats'>('training');
  const [meta, setMeta] = useState<Meta | null>(null);
  const [stats, setStats] = useState<Stats | null>(null);
  const [error, setError] = useState('');
  const [statsBusy, setStatsBusy] = useState(false);
  const [logoutBusy, setLogoutBusy] = useState(false);
  const [catalogBusy, setCatalogBusy] = useState(false);
  const refreshCatalog = useCallback(async () => { setCatalogBusy(true); setError(''); try { setMeta(await api<Meta>('/meta')); } catch (err) { setError(errorText(err)); } finally { setCatalogBusy(false); } }, []);
  const refreshStats = useCallback(async () => { setStatsBusy(true); try { setStats(await api<Stats>('/stats')); } catch (err) { setError(errorText(err)); } finally { setStatsBusy(false); } }, []);
  useEffect(() => {
    let active = true;
    api<{ username: string }>('/auth/me').then(data => { if (active) setUser(data.username); }).catch(err => { if (active && !(err instanceof ApiError && err.status === 401)) setError(errorText(err)); }).finally(() => { if (active) setLoading(false); });
    const expired = () => { setUser(null); setMeta(null); setStats(null); };
    window.addEventListener('session-expired', expired);
    return () => { active = false; window.removeEventListener('session-expired', expired); };
  }, []);
  useEffect(() => { if (user) { void refreshCatalog(); void refreshStats(); } }, [user, refreshStats, refreshCatalog]);
  async function logout() { setLogoutBusy(true); try { await api('/auth/logout', {}); setUser(null); setMeta(null); setStats(null); setError(''); } catch (err) { setError(errorText(err)); } finally { setLogoutBusy(false); } }
  if (loading) return <div className="app-loading"><Brand subtitle /><Spinner /><p>Готовим доску…</p></div>;
  if (!user) return <Login initialError={error} onLogin={name => { setUser(name); setError(''); }} />;
  return <div className="app-shell"><header className="header"><div className="header-inner"><a href="#" className="brand-link" onClick={event => { event.preventDefault(); setTab('training'); }} aria-label="Тактика — тренировка"><Brand /></a><nav aria-label="Главная навигация"><button className={tab === 'training' ? 'active' : ''} onClick={() => setTab('training')}><Target size={16} />Тренировка</button><button className={tab === 'stats' ? 'active' : ''} onClick={() => { setTab('stats'); void refreshStats(); }}><TrendingUp size={16} />Прогресс</button></nav><div className="account"><span className="avatar">{user.slice(0, 1).toUpperCase()}</span><span className="username">{user}</span><button className="icon-button" title="Выйти" aria-label="Выйти" disabled={logoutBusy} onClick={logout}>{logoutBusy ? <Spinner /> : <LogOut size={17} />}</button></div></div></header>
    <main className="main-content">{error && <ErrorNotice message={error} close={() => setError('')} />}{!meta && <div className="catalog-status"><span>{catalogBusy ? 'Загружаем библиотеку задач…' : 'Библиотека задач не загружена.'}</span><button className="text-button" disabled={catalogBusy} onClick={refreshCatalog}>{catalogBusy ? <Spinner /> : <RotateCcw size={14} />}Повторить загрузку</button></div>}<div hidden={tab !== 'training'}><Trainer meta={meta} stats={stats} onChanged={refreshStats} visible={tab === 'training'} /></div>{tab === 'stats' && <StatsPage stats={stats} busy={statsBusy} refresh={refreshStats} />}</main>
    <footer className="footer"><span>тактика<span className="brand-dot">.</span> <span className="footer-separator">/</span> Практика, которая остаётся с вами.</span><span><a href="https://database.lichess.org/#puzzles" target="_blank" rel="noreferrer">Позиции Lichess · CC0</a><span className="footer-separator">·</span><a href="/pieces/NOTICE.txt" target="_blank" rel="noreferrer">Фигуры</a></span></footer>
  </div>;
}
