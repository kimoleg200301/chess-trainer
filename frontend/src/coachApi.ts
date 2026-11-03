import { api } from './api';

export type CoachMetric = { games: number; opportunities: number; errors: number; rate: number | null };
export type CoachEvidence = {
  game_id: number; ply: number | null; move_number: number | null; played_san: string | null; best_san: string | null;
  explanation: string; game_url: string; phase: string | null;
};
export type CoachSkill = {
  id: string; title: string; description: string; recommendation: string; practice_tip: string;
  themes: string[]; priority: number; status: 'focus' | 'reinforce' | 'monitor' | 'insufficient';
  baseline: CoachMetric; recent: CoachMetric; delta: number | null; evidence: CoachEvidence[];
  training: { completed: number; clean: number; new_clean: number };
  metric_label: string; comparable: boolean;
};
export type CoachProfile = {
  id: number; username: string; time_control: string; started_at: string; active: boolean;
  status: 'loading' | 'analysing' | 'tracking' | 'paused' | 'error';
  baseline_target: number; baseline_games: number; baseline_completed: number; baseline_ready: boolean;
  new_games: number; new_completed: number; processed_moves: number; total_moves: number;
  progress: number; last_sync_at: string | null; next_sync_at: string | null; error: string | null;
  syncing: boolean; review_due: number;
};
export type CoachGame = {
  id: number; cohort: 'baseline' | 'followup'; white: string; black: string; source_game_at: string;
  result: string; player_result: string; status: string; cursor: number; total_plies: number;
  game_url: string; error: string | null;
};
export type CoachDashboard = {
  profile: CoachProfile | null; skills: CoachSkill[]; priorities: string[];
  summary: Record<string, unknown>; methodology: string; games: CoachGame[];
  active_attempt_id: number | null;
  games_truncated?: boolean;
};
export type CoachAttempt = {
  id: number; skill_id: string; skill_title: string; kind: 'decision' | 'puzzle';
  fen: string; starting_fen: string; learner_white: boolean; status: 'active' | 'completed' | 'revealed';
  mistakes: number; hints_used: number; elapsed_ms: number | null; last_move: string | null;
  ply_index: number; total_plies: number; prompt: string; feedback: string; is_new: boolean;
  source: { game_url: string; move_number: number | null; own_game: boolean };
  solution: null | { initial_fen: string; moves: { uci: string; san: string; fen: string }[]; explanation: string };
  target_seconds: number | null;
};
export type CoachPracticeOptions = { skill_id?: string; game_id?: number; ply?: number; exclude_attempt_id?: number };

export const coachApi = {
  dashboard: () => api<CoachDashboard>('/coach'),
  start: (username: string, timeControl: string) => api<CoachDashboard>('/coach/start', { username, time_control: timeControl }),
  control: (action: 'pause' | 'resume' | 'sync') => api<CoachDashboard>(`/coach/${action}`, {}),
  practice: (options: CoachPracticeOptions) => api<CoachAttempt>('/coach/practice', options),
  attempt: (id: number) => api<CoachAttempt>(`/coach/practice/${id}`),
  move: (id: number, move: string, elapsedMs: number) => api<{ accepted: boolean; attempt: CoachAttempt; feedback: string }>(`/coach/practice/${id}/move`, { move, elapsed_ms: elapsedMs }),
  hint: (id: number, elapsedMs: number) => api<{ attempt: CoachAttempt; message: string; from_square: string | null }>(`/coach/practice/${id}/hint`, { elapsed_ms: elapsedMs }),
  reveal: (id: number, elapsedMs: number) => api<CoachAttempt>(`/coach/practice/${id}/reveal`, { elapsed_ms: elapsedMs }),
  time: (id: number, elapsedMs: number, keepalive = false) => api<CoachAttempt>(`/coach/practice/${id}/time`, { elapsed_ms: elapsedMs }, 'POST', { keepalive }),
};
