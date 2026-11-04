import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { ArrowLeft, ArrowRight, Check, CheckCircle2, ChevronLeft, ChevronRight, Clock3, ExternalLink, GraduationCap, Lightbulb, ListChecks, LoaderCircle, Pause, Play, RefreshCw, RotateCcw, ShieldCheck, SkipForward, Sparkles, Target, TrendingUp, X } from 'lucide-react';
import { Chess } from 'chess.js';
import ChessBoard from './ChessBoard';
import SoundToggle from './SoundToggle';
import { coachApi } from './coachApi';
import type { CoachAttempt, CoachDashboard, CoachEvidence, CoachPracticeOptions, CoachProfile, CoachSkill } from './coachApi';
import type { Attempt } from './api';
import { useBackgroundRefresh } from './useBackgroundRefresh';
import { useAttemptTimer } from './useAttemptTimer';
import { useScreenWakeLock } from './useScreenWakeLock';
import { formatDuration } from './time';
import { playSound } from './sound';
import './coach.css';

const controls = [
  ['180+2', '3 + 2'], ['300', '5 + 0'], ['300+3', '5 + 3'], ['300+5', '5 + 5'],
  ['600', '10 + 0'], ['600+5', '10 + 5'], ['900+10', '15 + 10'], ['1800', '30 + 0'],
];
const labels = { focus: 'В приоритете', reinforce: 'Закрепляем', monitor: 'Наблюдаем', insufficient: 'Мало данных' };
const statusLabels = { loading: 'Собираем партии', analysing: 'Анализируем ходы', tracking: 'Следим за новыми партиями', paused: 'Тренер на паузе', error: 'Нужно продолжить обработку' };
const panelLabels = { plan: 'План', skills: 'Навыки', progress: 'Прогресс', games: 'Партии' };
type Panel = keyof typeof panelLabels;
const safeLink = (url: string) => /^https:\/\/(?:www\.)?(?:chess\.com|lichess\.org)\//.test(url);
const errorText = (error: unknown) => error instanceof Error ? error.message : 'Не удалось выполнить действие. Попробуйте ещё раз.';
const dateLabel = (value: string | null, includeTime = false) => {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('ru-RU', { day: 'numeric', month: 'short', ...(includeTime ? { hour: '2-digit', minute: '2-digit' } as const : {}) });
};
const timeControlLabel = (value: string) => {
  const [base, increment = '0'] = value.split('+');
  return `${Number(base) / 60} + ${increment}`;
};
const rateLabel = (value: number | null) => value == null ? '—' : value.toLocaleString('ru-RU', { maximumFractionDigits: 1 });
function Spinner() { return <LoaderCircle size={17} className="spinner" aria-hidden="true" />; }
function Notice({ message, close }: { message: string; close?: () => void }) {
  return <div className="error-notice" role="alert"><span>{message}</span>{close && <button aria-label="Закрыть сообщение" onClick={close}><X size={16} /></button>}</div>;
}
function PriorityRank({ rank, className = 'coach-skill-rank' }: { rank?: number; className?: string }) {
  return rank == null ? null : <span className={className}><span aria-hidden="true">{String(rank).padStart(2, '0')}</span><span className="coach-visually-hidden">Приоритет {rank}</span></span>;
}

function Setup({ busy, error, onStart, initialUsername = 'kimskiy1114', initialControl = '300+5', onCancel }: { busy: boolean; error: string; onStart: (username: string, control: string) => void; initialUsername?: string; initialControl?: string; onCancel?: () => void }) {
  const [username, setUsername] = useState(initialUsername);
  const [control, setControl] = useState(controls.some(([value]) => value === initialControl) ? initialControl : 'custom');
  const [customMinutes, setCustomMinutes] = useState(String(Number(initialControl.split('+')[0]) / 60));
  const [customIncrement, setCustomIncrement] = useState(initialControl.split('+')[1] || '0');
  function submit(event: FormEvent) {
    event.preventDefault();
    const selected = control === 'custom' ? `${Math.round(Number(customMinutes) * 60)}${Number(customIncrement) ? `+${Number(customIncrement)}` : ''}` : control;
    onStart(username.trim().toLowerCase(), selected);
  }
  return <section className="coach-setup" aria-labelledby="coach-title">
    <div className="coach-intro"><span className="eyebrow"><GraduationCap size={16} /> ЛИЧНЫЙ ТРЕНЕР</span><h1 id="coach-title">Твои партии.<br /><span>Твой план роста.</span></h1><p>Разберём последние 40 партий одного контроля, найдём темы для практики и будем следить за изменениями в новых играх.</p>
      <div className="coach-steps"><div><span>01</span><strong>Найдём отправную точку</strong><p>До 40 последних завершённых партий, включая самую свежую.</p></div><div><span>02</span><strong>Потренируем каждый навык</strong><p>Позиции из твоих ошибок, новые задачи и повторения.</p></div><div><span>03</span><strong>Проверим результат в игре</strong><p>Новые партии обновляют приоритеты и показывают, что пора закрепить.</p></div></div>
    </div>
    <form className="panel coach-start-form" onSubmit={submit}>
      <div className="coach-form-icon"><Target size={28} /></div><h2>С чего начнём?</h2><p>Публичный профиль Chess.com. Пароль от него не нужен.</p>
      <label htmlFor="coach-username">Ник на Chess.com</label><input id="coach-username" autoComplete="off" autoCapitalize="none" autoCorrect="off" spellCheck={false} required maxLength={40} pattern="[A-Za-z0-9_\-]+" value={username} onChange={event => setUsername(event.target.value)} disabled={busy} />
      <label htmlFor="coach-control">Контроль времени</label><select id="coach-control" value={control} disabled={busy} onChange={event => setControl(event.target.value)}>{controls.map(([value, label]) => <option key={value} value={value}>{label}</option>)}<option value="custom">Другой контроль</option></select>
      {control === 'custom' && <div className="coach-custom-control"><label>Минуты<input type="number" min="0.5" max="180" step="0.5" required value={customMinutes} onChange={event => setCustomMinutes(event.target.value)} disabled={busy} /></label><label>Добавление, сек.<input type="number" min="0" max="180" step="1" required value={customIncrement} onChange={event => setCustomIncrement(event.target.value)} disabled={busy} /></label></div>}
      <p className="coach-field-note">Сравниваем только выбранный контроль. Например, 5 + 0 и 5 + 5 учитываются отдельно.</p>
      {error && <Notice message={error} />}<button className="button primary" disabled={busy || !username.trim()}>{busy ? <Spinner /> : <Sparkles size={17} />}{busy ? 'Запускаем тренера…' : 'Создать мой план'}<ArrowRight size={17} /></button>
      {onCancel && <button className="text-button" type="button" onClick={onCancel} disabled={busy}><ArrowLeft size={15} />Вернуться к тренеру</button>}<div className="coach-privacy"><ShieldCheck size={16} /><span>Анализ продолжится на сервере, даже если закрыть браузер. Его можно поставить на паузу.</span></div>
    </form>
  </section>;
}

function AnalysisStatus({ profile }: { profile: CoachProfile }) {
  const pending = !profile.baseline_ready || profile.baseline_completed < profile.baseline_games || profile.new_completed < profile.new_games;
  const progress = Math.min(100, Math.max(0, profile.progress || 0));
  return <div className={`coach-analysis-status ${profile.active ? '' : 'paused'}`} role="status">
    <span className="coach-status-icon">{profile.status === 'error' ? <RefreshCw size={19} /> : profile.active && (pending || profile.syncing) ? <Spinner /> : profile.active ? <CheckCircle2 size={19} /> : <Pause size={19} />}</span>
    <div><strong>{statusLabels[profile.status] || 'Личный тренер'}</strong><p>{!profile.baseline_ready ? `Найдено стартовых партий: ${profile.baseline_games} из ${profile.baseline_target}. Ищем от самой свежей к более ранним.` : pending ? `Разобрано ${profile.baseline_completed} из ${profile.baseline_games} стартовых и ${profile.new_completed} из ${profile.new_games} новых партий.` : `Исходная точка: ${profile.baseline_completed} партий. Новых разобрано: ${profile.new_completed}.`}</p>
      {profile.baseline_ready && pending && <div className="coach-analysis-meter" role="progressbar" aria-label="Разбор партий тренером" aria-valuenow={progress} aria-valuemin={0} aria-valuemax={100}><span style={{ width: `${progress}%` }} /></div>}
    </div>{profile.baseline_ready && pending && <span className="coach-progress-number">{Math.round(progress)}%</span>}
  </div>;
}

function RateComparison({ skill }: { skill: CoachSkill }) {
  const unit = skill.metric_label || (skill.id === 'time_management' ? 'поражений по времени на 100 партий' : 'ошибок на 100 оценённых ходов');
  const baseline = skill.baseline, recent = skill.recent;
  const scale = Math.max(10, baseline.rate || 0, recent.rate || 0);
  return <div className="coach-comparison"><p>{unit}</p><div className="coach-comparison-bars" role="img" aria-label={`До старта: ${rateLabel(baseline.rate)}. После старта: ${rateLabel(recent.rate)}. ${unit}.`}>
    {[{ label: 'До старта', value: baseline, type: 'baseline' }, { label: 'После старта', value: recent, type: 'recent' }].map(item => <div className={`coach-comparison-row ${item.type}`} key={item.type}><span>{item.label}</span><div><i style={{ width: `${item.value.rate == null || item.value.rate === 0 ? 0 : Math.max(2, item.value.rate / scale * 100)}%` }} /></div><strong>{rateLabel(item.value.rate)}</strong></div>)}
    </div><small>{baseline.games} стартовых / {recent.games} новых партий · наблюдений: {baseline.opportunities} / {recent.opportunities}</small>
    {recent.games < 10 ? <p className="coach-small-note">Для устойчивого вывода нужно больше новых партий. Пока наблюдаем за изменениями.</p> : skill.delta != null && skill.comparable && skill.status !== 'insufficient' ? <p className="coach-small-note">{skill.delta < 0 ? 'Ошибок стало меньше. Закрепляй навык на новых позициях.' : skill.delta > 0 ? 'Ошибки повторяются чаще. Этой теме стоит уделить внимание.' : 'Частота ошибок пока не изменилась.'}</p> : <p className="coach-small-note">Данных для сравнения этой темы пока недостаточно.</p>}
  </div>;
}

function EvidenceCard({ evidence, index, total, busy, onChange, onPractice }: { evidence: CoachEvidence; index: number; total: number; busy: boolean; onChange: (index: number) => void; onPractice: () => void }) {
  return <div className="coach-evidence"><div className="coach-section-heading"><span><Target size={15} /> Пример из твоей партии</span><div><button className="icon-button" aria-label="Предыдущий пример" disabled={index === 0} onClick={() => onChange(index - 1)}><ChevronLeft size={17} /></button><small>{index + 1} / {total}</small><button className="icon-button" aria-label="Следующий пример" disabled={index + 1 >= total} onClick={() => onChange(index + 1)}><ChevronRight size={17} /></button></div></div>
    <p className="coach-evidence-move">{evidence.ply == null ? 'Поражение по времени' : <>Ход {evidence.move_number} · сыграно <strong>{evidence.played_san}</strong></>}</p><p className="coach-evidence-explanation" tabIndex={0}>{evidence.explanation}</p>
    <div className="coach-evidence-actions"><button className="button secondary" onClick={onPractice} disabled={busy}><Play size={15} />{evidence.ply == null ? 'Тренировать навык' : 'Решить этот момент'}</button>{safeLink(evidence.game_url) && <a href={evidence.game_url} target="_blank" rel="noreferrer" aria-label="Открыть исходную партию"><ExternalLink size={16} /></a>}</div>
  </div>;
}

export default function PersonalCoach({ visible, onFocusChange }: { visible: boolean; onFocusChange: (focused: boolean) => void }) {
  const [dashboard, setDashboard] = useState<CoachDashboard | null>(null);
  const [loading, setLoading] = useState(false);
  const [editingSetup, setEditingSetup] = useState(false);
  const [error, setError] = useState('');
  const [fetchError, setFetchError] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [screen, setScreen] = useState<'dashboard' | 'practice'>('dashboard');
  const [panel, setPanel] = useState<Panel>('plan');
  const [selectedSkill, setSelectedSkill] = useState('');
  const [evidenceIndex, setEvidenceIndex] = useState(0);
  const [gameCohort, setGameCohort] = useState<'baseline' | 'followup'>('followup');
  const [gamePage, setGamePage] = useState(0);
  const [attempt, setAttempt] = useState<CoachAttempt | null>(null);
  const [hintSquare, setHintSquare] = useState<string | null>(null);
  const [feedback, setFeedback] = useState('');
  const [feedbackKind, setFeedbackKind] = useState<'neutral' | 'success' | 'wrong'>('neutral');
  const [replayIndex, setReplayIndex] = useState<number | null>(null);
  const [replayPlaying, setReplayPlaying] = useState(false);
  const [sessionCount, setSessionCount] = useState(0);
  const [sessionClean, setSessionClean] = useState(0);
  const [sessionSummary, setSessionSummary] = useState(false);
  const [timeError, setTimeError] = useState(false);
  const actionPending = useRef(false);
  const timeSaves = useRef(new Set<Promise<void>>());
  const mounted = useRef(true);
  const fetchVersion = useRef(0);
  const dashboardRef = useRef(dashboard); dashboardRef.current = dashboard;
  const currentAttempt = useRef(attempt); currentAttempt.current = attempt;
  const finished = useRef(new Set<number>());
  const sessionBack = useRef<HTMLButtonElement>(null);
  const refresh = useCallback(async () => {
    if (actionPending.current) return;
    const version = ++fetchVersion.current;
    if (!dashboardRef.current) setLoading(true);
    try { const data = await coachApi.dashboard(); if (mounted.current && version === fetchVersion.current) { setDashboard(data); setFetchError(''); } }
    catch (err) { if (mounted.current && version === fetchVersion.current) setFetchError(errorText(err)); }
    finally { if (mounted.current && version === fetchVersion.current) setLoading(false); }
  }, []);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => { if (visible) void refresh(); }, [visible, refresh]);
  const activelyAnalysing = !!dashboard?.profile?.active && (['loading', 'analysing'].includes(dashboard.profile.status) || dashboard.profile.syncing);
  useBackgroundRefresh(refresh, visible && screen === 'dashboard' && activelyAnalysing);
  useEffect(() => {
    if (!visible || screen !== 'dashboard' || !dashboard?.profile?.active || activelyAnalysing) return;
    const update = () => { if (document.visibilityState === 'visible') void refresh(); };
    const interval = window.setInterval(update, 20_000);
    document.addEventListener('visibilitychange', update); window.addEventListener('pageshow', update); window.addEventListener('online', update);
    return () => { window.clearInterval(interval); document.removeEventListener('visibilitychange', update); window.removeEventListener('pageshow', update); window.removeEventListener('online', update); };
  }, [visible, screen, dashboard?.profile?.active, activelyAnalysing, refresh]);
  useEffect(() => { onFocusChange(screen === 'practice'); return () => onFocusChange(false); }, [screen, onFocusChange]);
  useEffect(() => { if (!visible || screen !== 'practice') setReplayPlaying(false); }, [visible, screen]);
  useEffect(() => { if (visible && screen === 'practice') { window.scrollTo({ top: 0, behavior: 'instant' }); sessionBack.current?.focus({ preventScroll: true }); } }, [visible, screen]);
  useEffect(() => { setEvidenceIndex(0); }, [selectedSkill]);
  const persistTime = useCallback((id: number, elapsed: number, keepalive: boolean) => {
    // Actions carry elapsed_ms themselves and must not compete with autosave for the user lock.
    if (actionPending.current) return;
    const request: Promise<void> = coachApi.time(id, elapsed, keepalive).then(result => {
      if (!mounted.current || currentAttempt.current?.id !== id) return;
      setAttempt(previous => previous?.id === id && previous.status === 'active' && (previous.elapsed_ms ?? -1) < (result.elapsed_ms ?? -1) ? { ...previous, elapsed_ms: result.elapsed_ms } : previous);
      setTimeError(false);
    }).catch(() => { if (mounted.current && !keepalive && currentAttempt.current?.id === id && currentAttempt.current.status === 'active') setTimeError(true); })
      .finally(() => { timeSaves.current.delete(request); });
    timeSaves.current.add(request);
  }, []);
  const timer = useAttemptTimer({ attemptId: attempt?.id ?? null, elapsedMs: attempt?.elapsed_ms ?? null, running: visible && screen === 'practice' && attempt?.status === 'active' && !busy, onPersist: persistTime });
  useScreenWakeLock(visible && screen === 'practice' && !!attempt);
  const done = !!attempt && attempt.status !== 'active';
  const solution = done ? attempt.solution : null;
  const totalReplay = solution?.moves.length || 0;
  const shownReplay = replayIndex ?? 0;
  const boardAttempt = useMemo<Attempt | null>(() => {
    if (!attempt) return null;
    let fen = attempt.fen, lastMove = attempt.last_move;
    if (attempt.status !== 'active' && attempt.solution) {
      const index = replayIndex ?? 0;
      const board = new Chess(attempt.solution.initial_fen);
      for (const move of attempt.solution.moves.slice(0, index)) board.move(move.uci);
      fen = board.fen(); lastMove = index ? attempt.solution.moves[index - 1].uci : null;
    }
    return { id: attempt.id, puzzle_id: `coach-${attempt.id}`, fen, rating: 0, themes: [], status: attempt.status,
      turn: attempt.learner_white ? 'white' : 'black', solved_plies: attempt.ply_index, total_plies: attempt.total_plies,
      mistakes: attempt.mistakes, hints_used: attempt.hints_used, last_move: lastMove, elapsed_ms: attempt.elapsed_ms,
      collection: attempt.source.own_game ? 'personal' : 'lichess', rating_estimated: false };
  }, [attempt, replayIndex]);
  useEffect(() => {
    if (!replayPlaying || !solution || !visible || screen !== 'practice') return;
    const startedAt = Date.now(), startIndex = replayIndex ?? 0;
    const step = () => { const index = Math.min(solution.moves.length, startIndex + Math.floor((Date.now() - startedAt) / 1000)); setReplayIndex(index); if (index >= solution.moves.length) setReplayPlaying(false); };
    const interval = window.setInterval(step, 100);
    return () => window.clearInterval(interval);
  }, [replayPlaying, solution, visible, screen]);
  function seek(index: number) { setReplayPlaying(false); setReplayIndex(Math.min(totalReplay, Math.max(0, index))); }
  function toggleReplay() { if (replayPlaying) setReplayPlaying(false); else { if (replayIndex == null || shownReplay >= totalReplay) setReplayIndex(0); setReplayPlaying(true); } }
  function acceptAttempt(value: CoachAttempt) {
    setAttempt(value);
    if (value.status !== 'active' && value.solution) {
      const matched = value.solution.moves.findIndex(item => item.fen === value.fen);
      setReplayIndex(matched >= 0 ? matched + 1 : 0);
    }
    if (value.status !== 'active' && !finished.current.has(value.id)) {
      finished.current.add(value.id); setSessionCount(count => count + 1);
      if (value.status === 'completed' && !value.mistakes && !value.hints_used) setSessionClean(count => count + 1);
    }
  }
  async function action(name: string, operation: () => Promise<void>) {
    if (actionPending.current) return;
    actionPending.current = true; ++fetchVersion.current; setBusy(name); setLoading(false); setError('');
    try {
      // timer.pause() may just have sent a save; settle every earlier save before mutating.
      await Promise.allSettled([...timeSaves.current]);
      if (mounted.current) await operation();
    } catch (err) { if (mounted.current) setError(errorText(err)); }
    finally { actionPending.current = false; if (mounted.current) setBusy(null); }
  }
  async function startCoach(username: string, control: string) { await action('start', async () => { setDashboard(await coachApi.start(username, control)); setEditingSetup(false); }); }
  async function controlCoach(operation: 'pause' | 'resume' | 'sync') { await action(operation, async () => setDashboard(await coachApi.control(operation))); }
  async function practice(options: CoachPracticeOptions = {}, newSession = true) {
    timer.pause();
    await action('practice', async () => {
      const result = await coachApi.practice(options);
      if (newSession) { finished.current.clear(); setSessionCount(0); setSessionClean(0); }
      setSessionSummary(false); setAttempt(result); setFeedback(result.feedback || ''); setFeedbackKind('neutral');
      setHintSquare(null); setReplayIndex(null); setReplayPlaying(false); setTimeError(false); setScreen('practice');
    });
  }
  async function resume() {
    const id = attempt?.status === 'active' ? attempt.id : dashboard?.active_attempt_id;
    if (!id) return;
    await action('resume-practice', async () => { const result = await coachApi.attempt(id); setAttempt(result); setFeedback(result.feedback || ''); setHintSquare(null); setFeedbackKind('neutral'); setReplayIndex(null); setTimeError(false); setScreen('practice'); });
  }
  async function move(uci: string) {
    if (!attempt || attempt.status !== 'active' || actionPending.current) return;
    const id = attempt.id, elapsed = timer.pause(), previousFen = attempt.fen;
    await action('move', async () => {
      const result = await coachApi.move(id, uci, elapsed);
      setTimeError(false);
      acceptAttempt(result.attempt); setFeedback(result.feedback); setFeedbackKind(result.accepted ? 'success' : 'wrong'); setHintSquare(null);
      if (result.attempt.status === 'completed') playSound('complete');
      else if (result.accepted) { const board = new Chess(previousFen); playSound(board.move(uci)?.captured ? 'capture' : 'move'); }
      else playSound('wrong');
    });
  }
  async function hint() {
    if (!attempt || attempt.status !== 'active' || actionPending.current) return;
    const id = attempt.id, elapsed = timer.pause();
    await action('hint', async () => { const result = await coachApi.hint(id, elapsed); setTimeError(false); setAttempt(result.attempt); setHintSquare(result.from_square); setFeedback(result.message); setFeedbackKind('neutral'); });
  }
  async function reveal() {
    if (!attempt || attempt.status !== 'active' || actionPending.current) return;
    const id = attempt.id, elapsed = timer.pause();
    await action('reveal', async () => { const result = await coachApi.reveal(id, elapsed); setTimeError(false); acceptAttempt(result); setHintSquare(null); setFeedback(result.feedback || 'Разбери продолжение и вернись к позиции в повторении.'); setFeedbackKind('neutral'); setReplayIndex(0); });
  }
  function leavePractice() { timer.pause(); setReplayPlaying(false); setScreen('dashboard'); setSessionSummary(sessionCount > 0); void refresh(); }
  const profile = dashboard?.profile;
  const skills = dashboard?.skills || [];
  const prioritySkills = (dashboard?.priorities || []).map(id => skills.find(skill => skill.id === id)).filter((skill): skill is CoachSkill => !!skill).slice(0, 3);
  // Keep the server's order, but do not rank preliminary or monitoring topics as confirmed problems.
  const rankedSkills = skills.filter(skill => skill.priority > 0 && (skill.status === 'focus' || skill.status === 'reinforce'));
  const priorityRanks = new Map(rankedSkills.map((skill, index) => [skill.id, index + 1]));
  const chosen = skills.find(skill => skill.id === selectedSkill) || prioritySkills[0] || skills[0];
  const hasResume = attempt?.status === 'active' || !!dashboard?.active_attempt_id;
  const canPractice = !!profile && profile.baseline_games + profile.new_games > 0;
  const visibleGames = (dashboard?.games || []).filter(game => game.cohort === gameCohort);
  const maxGamePage = Math.max(0, Math.ceil(visibleGames.length / 8) - 1);
  const actualGamePage = Math.min(gamePage, maxGamePage);
  const currentEvidence = chosen?.evidence[Math.min(evidenceIndex, Math.max(0, chosen.evidence.length - 1))];

  if (screen === 'practice' && attempt) return <section className={`coach-session ${done ? 'coach-session-review' : ''}`} aria-label="Занятие с личным тренером">
    <header className="coach-session-toolbar"><button ref={sessionBack} className="text-button" onClick={leavePractice} disabled={!!busy}><ArrowLeft size={18} /><span>К плану</span></button><span className="coach-session-topic">{attempt.skill_title}</span><SoundToggle /></header>
    <div className="coach-session-layout"><div className="coach-session-board-column"><div className="coach-board-meta"><span><i className={`side-indicator ${attempt.learner_white ? '' : 'black-side'}`} />{attempt.learner_white ? 'Ход белых' : 'Ход чёрных'}</span><span className={`attempt-timer ${done ? 'finished' : ''}`} aria-label={`Время решения ${formatDuration(timer.elapsedMs)}`}><Clock3 size={14} />{formatDuration(timer.elapsedMs)}</span><span>{attempt.source.own_game ? 'Из твоей партии' : 'Учебная позиция'}</span></div>
      <div className={`coach-board-wrap ${done ? 'coach-board-done' : ''}`}><ChessBoard attempt={boardAttempt} busy={!!busy || done} hintSquare={hintSquare} onMove={uci => void move(uci)} /></div>
      <div className="coach-session-underboard"><span>{attempt.is_new ? 'Первое решение' : 'Повторение'}{attempt.source.move_number != null && ` · ход ${attempt.source.move_number}`}</span><span>Разобрано: {sessionCount} / 10</span></div>
    </div>
    <aside className={`coach-session-instruction ${feedbackKind}`} aria-label="Задание и обратная связь">
      <div className="coach-lesson-heading"><span>{done ? <CheckCircle2 size={20} /> : <Target size={20} />}</span><div><small>{done ? attempt.status === 'completed' ? 'РЕШЕНИЕ НАЙДЕНО' : 'РАЗБОР ПОЗИЦИИ' : 'ТВОЯ ЗАДАЧА'}</small><h1>{done ? attempt.status === 'completed' ? 'Сохрани эту идею' : 'Пойми продолжение' : attempt.kind === 'decision' ? 'Выбери сильный ход' : 'Найди продолжение'}</h1></div></div>
      <div className="coach-lesson-text" tabIndex={0} aria-label="Пояснение к позиции" aria-live="polite">{error ? <Notice message={error} close={() => setError('')} /> : <><p>{feedback || attempt.prompt}</p>{done && solution?.explanation && solution.explanation !== feedback && <p>{solution.explanation}</p>}</>}{timeError && <p className="coach-time-warning">Время пока не сохранено. Повторим отправку при восстановлении связи.</p>}</div>
      {!done && attempt.target_seconds != null && <p className="coach-time-target"><Clock3 size={14} />Ориентир: {attempt.target_seconds} сек. Можно думать дольше.</p>}
      {done && solution && <div className="coach-solution"><div className="coach-solution-label"><span>Продолжение</span><span>{shownReplay} / {totalReplay}</span></div><div className="coach-solution-moves" tabIndex={0} aria-label="Ходы решения">{solution.moves.map((item, index) => <button key={`${index}-${item.uci}`} className={shownReplay === index + 1 ? 'current' : ''} onClick={() => seek(index + 1)} aria-label={`Показать ход ${item.san}`}>{item.san}</button>)}</div></div>}
      <div className="coach-session-detail"><span>{attempt.mistakes ? `Ошибок: ${attempt.mistakes}` : 'Без ошибок'} · {attempt.hints_used ? 'С подсказкой' : 'Без подсказки'}</span>{done && safeLink(attempt.source.game_url) && <a href={attempt.source.game_url} target="_blank" rel="noreferrer" aria-label="Открыть партию"><ExternalLink size={15} /></a>}</div>
    </aside></div>
    <footer className="coach-session-actions" role="group" aria-label="Действия в занятии"><div className="coach-session-actions-inner">{done ? <>
      <div className="coach-replay-buttons" role="group" aria-label="Разбор решения"><button className="button secondary" aria-label="Предыдущий ход" disabled={shownReplay <= 0 || !solution} onClick={() => seek(shownReplay - 1)}><ChevronLeft size={19} /></button><button className="button secondary" aria-label={replayPlaying ? 'Остановить воспроизведение' : 'Воспроизвести решение'} disabled={!totalReplay} onClick={toggleReplay}>{replayPlaying ? <Pause size={17} /> : <Play size={17} />}</button><button className="button secondary" aria-label="Следующий ход" disabled={shownReplay >= totalReplay || !solution} onClick={() => seek(shownReplay + 1)}><ChevronRight size={19} /></button></div>
      {sessionCount >= 10 ? <button className="button primary coach-next" onClick={leavePractice}><Check size={18} />Завершить</button> : <button className="button primary coach-next" disabled={!!busy} onClick={() => void practice({ skill_id: attempt.skill_id, exclude_attempt_id: attempt.id }, false)}>{busy === 'practice' ? <Spinner /> : <ArrowRight size={18} />}Дальше</button>}
    </> : <><button className="button secondary" disabled={!!busy} onClick={() => void hint()}>{busy === 'hint' ? <Spinner /> : <Lightbulb size={17} />}<span>Подсказка</span></button><button className="button secondary" disabled={!!busy} onClick={() => void reveal()}>{busy === 'reveal' ? <Spinner /> : <ListChecks size={17} />}<span>Разобрать</span></button><button className="button secondary coach-skip" aria-label="Отложить позицию" title="Отложить позицию" disabled={!!busy} onClick={() => void practice({ skill_id: attempt.skill_id, exclude_attempt_id: attempt.id }, false)}>{busy === 'practice' ? <Spinner /> : <SkipForward size={18} />}</button></>}</div></footer>
  </section>;

  if (!dashboard) return <section className="coach-placeholder panel" aria-busy={loading}><GraduationCap size={32} /><h1>{loading ? 'Открываем личного тренера…' : 'Личный тренер'}</h1>{(error || fetchError) && <Notice message={error || fetchError} />}<button className="button secondary" onClick={() => void refresh()} disabled={loading}>{loading ? <Spinner /> : <RefreshCw size={16} />}Обновить</button></section>;
  if (!profile || editingSetup) return <Setup busy={!!busy} error={error || fetchError} initialUsername={profile?.username} initialControl={profile?.time_control} onCancel={profile ? () => setEditingSetup(false) : undefined} onStart={(username, control) => void startCoach(username, control)} />;

  return <section className="coach-dashboard" aria-labelledby="coach-dashboard-title">
    <div className="coach-dashboard-heading"><div><span className="eyebrow"><GraduationCap size={15} /> ЛИЧНЫЙ ТРЕНЕР</span><h1 id="coach-dashboard-title">Игра становится практикой</h1><p><strong>{profile.username}</strong><span>·</span>{timeControlLabel(profile.time_control)}<span>·</span>с {dateLabel(profile.started_at)}</p></div><div className="coach-dashboard-controls"><button className="button secondary" disabled={!!busy || profile.syncing || !profile.active} onClick={() => void controlCoach('sync')}>{busy === 'sync' || (profile.active && profile.syncing) ? <Spinner /> : <RefreshCw size={15} />}<span>Проверить партии</span></button><button className="button secondary" disabled={!!busy} onClick={() => void controlCoach(profile.active && profile.status !== 'error' ? 'pause' : 'resume')}>{busy === 'pause' || busy === 'resume' ? <Spinner /> : profile.active && profile.status !== 'error' ? <Pause size={15} /> : <Play size={15} />}{profile.status === 'error' ? 'Повторить анализ' : profile.active ? 'Пауза' : 'Продолжить'}</button></div></div>
    {error && <Notice message={error} close={() => setError('')} />}{fetchError && <Notice message={fetchError} close={() => setFetchError('')} />}{profile.error && <Notice message={profile.error} />}{profile.baseline_games === 0 && profile.new_games === 0 && (profile.error || profile.baseline_ready) && !dashboard.active_attempt_id && <button className="button secondary coach-fix-setup" disabled={!!busy} onClick={() => { setEditingSetup(true); setError(''); }}><RefreshCw size={15} />Исправить настройки</button>}
    <AnalysisStatus profile={profile} />
    {sessionSummary && <div className="coach-session-summary" role="status"><CheckCircle2 size={19} /><p>За занятие разобрано: <strong>{sessionCount}</strong>. Чистых решений: <strong>{sessionClean}</strong>. Проверим применение навыков в следующих партиях.</p><button className="icon-button" aria-label="Закрыть итоги занятия" onClick={() => setSessionSummary(false)}><X size={16} /></button></div>}
    <div className="coach-tabs" role="tablist" aria-label="Разделы личного тренера">{(Object.keys(panelLabels) as Panel[]).map((name, index, names) => <button key={name} id={`coach-tab-${name}`} role="tab" aria-selected={panel === name} aria-controls={`coach-panel-${name}`} tabIndex={panel === name ? 0 : -1} onClick={() => setPanel(name)} onKeyDown={event => { if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return; event.preventDefault(); const next = names[(index + (event.key === 'ArrowRight' ? 1 : names.length - 1)) % names.length]; setPanel(next); document.getElementById(`coach-tab-${next}`)?.focus(); }}>{panelLabels[name]}{name === 'games' && profile.new_games > 0 && <span>{profile.new_games}</span>}</button>)}</div>
    <div id={`coach-panel-${panel}`} role="tabpanel" aria-labelledby={`coach-tab-${panel}`} tabIndex={-1}>
      {panel === 'plan' && <div className="coach-plan-grid"><div className="coach-daily-card"><span className="coach-daily-eyebrow"><Sparkles size={17} /> ПРАКТИКА НА СЕГОДНЯ</span><h2>{prioritySkills.length ? <>Одна тема.<br />Десять решений.</> : <>Начни с основ,<br />пока идёт разбор.</>}</h2><p>{prioritySkills[0]?.recommendation || 'Тренер готовит рекомендации по твоим партиям. Пока можно потренировать проверку угроз и расчёт коротких вариантов.'}</p><div className="coach-daily-meta"><span><Clock3 size={15} />15–20 минут</span><span><RotateCcw size={15} />{profile.review_due} к повторению</span></div><button className="button coach-daily-start" disabled={!!busy || (!canPractice && !hasResume)} onClick={() => hasResume ? void resume() : void practice({ skill_id: prioritySkills[0]?.id })}>{busy === 'practice' || busy === 'resume-practice' ? <Spinner /> : <Play size={17} />}{hasResume ? 'Продолжить занятие' : 'Начать занятие'}<ArrowRight size={18} /></button><small>{canPractice || hasResume ? 'Свои ошибки · новые задачи · повторение' : 'Занятия откроются после загрузки первой партии.'}</small></div>
        <div className="coach-priorities"><div className="coach-section-heading"><h2>Сейчас в фокусе</h2><span>{prioritySkills.length ? `${prioritySkills.length} темы` : 'Идёт диагностика'}</span></div>{prioritySkills.length ? prioritySkills.map(skill => <button className="coach-priority-card" key={skill.id} onClick={() => { setSelectedSkill(skill.id); setPanel('skills'); }}><PriorityRank rank={priorityRanks.get(skill.id)} className="coach-priority-number" /><div><div><h3>{skill.title}</h3><span className={`coach-skill-status ${skill.status}`}>{labels[skill.status]}</span></div><p>{skill.description}</p><small>{skill.evidence.length ? `Примеры из партий: ${skill.evidence.length}` : 'Начни с учебных позиций'}</small></div><ChevronRight size={19} /></button>) : <div className="coach-awaiting panel"><Target size={29} /><h3>Ищем повторяющиеся моменты</h3><p>Первые выводы появятся по мере разбора. Если в теме мало данных, тренер так и скажет.</p></div>}<button className="text-button coach-all-skills" onClick={() => setPanel('skills')}>Все направления тренировки<ArrowRight size={15} /></button></div>
        <div className="coach-plan-note"><ShieldCheck size={20} /><p><strong>Прогресс проверяем в новых партиях.</strong> Исходные партии остаются точкой сравнения. После старта сравниваем с последними 40 новыми партиями того же контроля.</p></div>
      </div>}
      {(panel === 'skills' || panel === 'progress') && <><div className="coach-panel-purpose"><p>{panel === 'skills' ? 'Что тренировать: рекомендации, примеры из твоих партий и упражнения по каждому навыку.' : 'Что меняется: частота ошибок в новых партиях по сравнению со стартом и результаты упражнений.'}</p>{rankedSkills.length > 0 && <span>Номер — порядок тренировки: начни с №1.</span>}</div><div className="coach-skills-layout"><div className="coach-skill-picker" aria-label="Выбор навыка">{skills.map(skill => <button key={skill.id} className={chosen?.id === skill.id ? 'selected' : ''} aria-pressed={chosen?.id === skill.id} onClick={() => setSelectedSkill(skill.id)}><span className="coach-skill-name"><PriorityRank rank={priorityRanks.get(skill.id)} /><span className="coach-skill-title">{skill.title}</span></span><small className={`coach-skill-status ${skill.status}`}>{labels[skill.status]}</small><ChevronRight size={16} /></button>)}</div>{chosen ? <article className="coach-skill-detail panel"><div className="coach-section-heading"><div className="coach-skill-meta">{priorityRanks.has(chosen.id) && <span className="coach-detail-priority">Приоритет №{priorityRanks.get(chosen.id)}</span>}<span className={`coach-skill-status ${chosen.status}`}>{labels[chosen.status]}</span></div>{panel === 'progress' ? <TrendingUp size={21} /> : <Target size={21} />}</div><h2>{chosen.title}</h2><p className="coach-skill-description">{chosen.description}</p><div className="coach-recommendation"><Lightbulb size={19} /><p>{chosen.recommendation}</p></div>
        {panel === 'progress' ? <><RateComparison skill={chosen} /><div className="coach-training-counts"><div><strong>{chosen.training.completed}</strong><span>позиций решено</span></div><div><strong>{chosen.training.clean}</strong><span>чистых решений</span></div><div><strong>{chosen.training.new_clean}</strong><span>новых без подсказок</span></div></div><p className="coach-small-note">Результаты упражнений и результаты партий учитываются отдельно. Повторное решение знакомой позиции не означает, что навык уже закреплён в игре.</p></> : <><p className="coach-practice-tip">{chosen.practice_tip}</p>{currentEvidence && <EvidenceCard evidence={currentEvidence} index={Math.min(evidenceIndex, chosen.evidence.length - 1)} total={chosen.evidence.length} busy={!!busy} onChange={setEvidenceIndex} onPractice={() => void practice(currentEvidence.ply == null ? { skill_id: chosen.id } : { skill_id: chosen.id, game_id: currentEvidence.game_id, ply: currentEvidence.ply })} />}{!currentEvidence && <p className="coach-small-note">Подтверждённых примеров из твоих партий пока нет. Можно начать с упражнений по этой теме.</p>}</>}
        <button className="button primary coach-skill-start" disabled={!!busy || !canPractice} onClick={() => void practice({ skill_id: chosen.id })}>{busy === 'practice' ? <Spinner /> : <Play size={17} />}Тренировать этот навык<ArrowRight size={16} /></button>
      </article> : <div className="panel coach-awaiting"><Spinner /><p>Направления появятся после загрузки партий.</p></div>}</div></>}
      {panel === 'games' && <div className="coach-games panel"><div className="coach-games-toolbar"><div className="coach-cohort-switch" role="group" aria-label="Группа партий"><button className={gameCohort === 'followup' ? 'selected' : ''} aria-pressed={gameCohort === 'followup'} onClick={() => { setGameCohort('followup'); setGamePage(0); }}>После старта<span>{profile.new_games}</span></button><button className={gameCohort === 'baseline' ? 'selected' : ''} aria-pressed={gameCohort === 'baseline'} onClick={() => { setGameCohort('baseline'); setGamePage(0); }}>Исходные<span>{profile.baseline_games}</span></button></div><p>{gameCohort === 'followup' ? 'Новые игры показывают перенос навыков в практику.' : 'Эта группа сохраняется для сравнения с новыми играми.'}</p></div>
        {gameCohort === 'followup' && dashboard.games_truncated && <p className="coach-small-note">В списке показаны последние 200 новых партий. Общая история сохранена.</p>}{visibleGames.length ? <><div className="coach-game-list">{visibleGames.slice(actualGamePage * 8, actualGamePage * 8 + 8).map(game => <div className="coach-game-row" key={game.id}><span className={`coach-game-result ${game.player_result === 'win' ? 'win' : ['resigned', 'checkmated', 'timeout', 'abandoned', 'lose'].includes(game.player_result) ? 'loss' : 'draw'}`}>{game.player_result === 'win' ? '1' : ['resigned', 'checkmated', 'timeout', 'abandoned', 'lose'].includes(game.player_result) ? '0' : '½'}</span><div><strong>{game.white}<span> — </span>{game.black}</strong><small>{dateLabel(game.source_game_at, true)}{game.player_result === 'timeout' ? ' · проигрыш по времени' : ''}</small>{game.error && <p className="coach-game-error">{game.error}</p>}</div><span className="coach-game-status">{game.status === 'completed' ? <><CheckCircle2 size={15} /><span>Разобрана</span></> : game.status === 'failed' ? 'Нужен повтор' : <><Clock3 size={15} /><span>{game.cursor ? `${Math.round(game.cursor / Math.max(1, game.total_plies) * 100)}%` : 'В очереди'}</span></>}</span>{safeLink(game.game_url) && <a href={game.game_url} target="_blank" rel="noreferrer" aria-label={`Открыть партию ${game.white} — ${game.black}`}><ExternalLink size={16} /></a>}</div>)}</div><div className="coach-pagination"><button className="button secondary" disabled={actualGamePage === 0} onClick={() => setGamePage(actualGamePage - 1)}><ChevronLeft size={16} />Назад</button><span>{actualGamePage + 1} / {maxGamePage + 1}</span><button className="button secondary" disabled={actualGamePage >= maxGamePage} onClick={() => setGamePage(actualGamePage + 1)}>Далее<ChevronRight size={16} /></button></div></> : <div className="coach-awaiting"><Clock3 size={29} /><h3>{gameCohort === 'followup' ? 'Ждём твою следующую партию' : 'Собираем исходные партии'}</h3><p>{gameCohort === 'followup' ? `Сыграй партию ${timeControlLabel(profile.time_control)} на Chess.com. Тренер автоматически добавит её после публикации в архиве.` : 'Загружаем самые свежие партии выбранного контроля.'}</p></div>}
      </div>}
    </div>
    <div className="coach-sync-note"><span className={`tiny-dot ${profile.active ? '' : 'inactive'}`} /><span>{profile.active ? 'Новые партии проверяются каждые 5 минут, пока работает сервер.' : 'Автоматический анализ и поиск новых партий приостановлены.'}{profile.last_sync_at && ` Последняя проверка: ${dateLabel(profile.last_sync_at, true)}.`}</span></div>
  </section>;
}
