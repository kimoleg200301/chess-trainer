export type Theme = { id: string; name: string; count: number };
export type PuzzleCollection = { id: string; name: string; count: number };
export type Meta = { total_puzzles: number; themes: Theme[]; min_rating: number; max_rating: number; collections: PuzzleCollection[] };
export type RatingChange = { before: number; after: number; delta: number; puzzle_before: number; puzzle_after: number; clean: boolean };
export type TrainingOptions = { collection: string; mode: 'practice' | 'review'; adaptive: boolean; theme: string | null; min_rating: number; max_rating: number };
export type Attempt = {
  id: number; puzzle_id: string; fen: string; rating: number; themes: string[];
  status: 'active' | 'completed' | 'revealed'; turn: 'white' | 'black';
  solved_plies: number; total_plies: number; mistakes: number; hints_used: number;
  last_move: string | null; elapsed_ms: number | null;
  collection: string; rating_estimated: boolean;
  starting_fen?: string; resumed?: boolean;
  rated?: boolean; rating_change?: RatingChange | null; training_options?: TrainingOptions;
};
export type Stats = {
  solved: number; total_attempts: number; accuracy: number; streak: number; due_count: number;
  average_solution_ms: number | null; timed_solutions: number;
  puzzle_rating: number; rated_attempts: number;
  by_theme: { id: string; name: string; attempts: number; solved: number }[];
  recent: { puzzle_id: string; rating: number; status: string; created_at: string; elapsed_ms: number | null; collection: string; rating_estimated: boolean }[];
};
export type Analysis = { evaluation: string; line: string[]; engine: string };
export type Reveal = { attempt: Attempt; solution: { uci: string; san: string }[]; game_url: string };
export type SavedTraining = Omit<Reveal, 'attempt'> & { attempt: Attempt | null };

export type ImportJobSummary = {
  background_requested: boolean;
  id: number; source: 'public' | 'chesscom'; username: string | null; month: string | null;
  status: 'ready' | 'running' | 'completed' | 'failed'; total_games: number; completed_games: number;
  candidates_count: number; available_puzzles_count: number; puzzles_count: number;
  analysis_version: number; analysis_outdated: boolean; error: string | null; created_at: string;
};
export type Improvement = {
  id: number; ply: number; move_number: number; side: 'white' | 'black';
  played_uci: string; played_san: string; best_uci: string; best_san: string;
  loss_cp: number; score_before: number | null; score_after: number | null;
  mate_before?: number | null; mate_after?: number | null; best_line_san?: string[];
  phase?: 'opening' | 'middlegame' | 'endgame';
  explanation: string; puzzle_available: boolean; puzzle_id: string | null;
  puzzle_reason_code: string | null; puzzle_reason: string | null;
  analysis_depth: number | null; analysis_pending: boolean;
};
export type ImportedGame = {
  id: number; white: string; black: string; result: string; date: string; event: string;
  game_url: string; status: 'pending' | 'analysing' | 'completed' | 'skipped';
  cursor: number; total_plies: number; error: string | null; candidates: Improvement[];
  source_game_at: string | null; analysis_version: number;
};
export type ImportJob = ImportJobSummary & { games: ImportedGame[] };
export type CreatedPuzzle = { puzzle_id: string; collection: 'public' | 'personal'; created: boolean };

export class ApiError extends Error {
  constructor(message: string, public status: number, public code?: string) { super(message); }
}

export async function api<T>(path: string, body?: unknown, method?: string, options: { keepalive?: boolean } = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      method: method || (body === undefined ? 'GET' : 'POST'), credentials: 'same-origin', keepalive: options.keepalive,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch { throw new ApiError('Не удалось связаться с сервером. Проверьте подключение и попробуйте ещё раз.', 0); }
  if (!response.ok) {
    let message = response.status === 401 ? 'Войдите в аккаунт, чтобы продолжить.' : `Ошибка сервера (${response.status}). Попробуйте ещё раз.`;
    let code: string | undefined;
    try { const data = await response.json(); if (typeof data.detail === 'string') message = data.detail; if (typeof data.code === 'string') code = data.code; } catch { /* Keep readable fallback. */ }
    if (response.status === 401 && path !== '/auth/login') window.dispatchEvent(new Event('session-expired'));
    throw new ApiError(message, response.status, code);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}


export type GameMoveCategory = 'brilliant' | 'great' | 'best' | 'excellent' | 'good' | 'inaccuracy' | 'mistake' | 'blunder' | 'forced' | 'unassessed';
export type GameAnalysisSource = {
  background_requested: boolean;
  id: number; white: string; black: string; result: string; date: string;
  game_url: string; source_game_at: string | null; username: string | null; total_plies: number;
  analysis_id: number | null; analysis_status: 'ready' | 'running' | 'completed' | 'failed' | null;
  analysis_outdated: boolean;
};
export type AnalysedGameMove = {
  ply: number; move_number: number; side: 'white' | 'black'; before_fen: string; after_fen: string;
  played_uci: string; played_san: string; best_uci: string | null; best_san: string | null;
  category: GameMoveCategory; accuracy: number | null; expected_loss: number | null;
  best_expected: number | null; played_expected: number | null;
  score_before: number | null; score_after: number | null; mate_before: number | null; mate_after: number | null;
  best_line_uci: string[]; best_line_san: string[]; depth: number; assessed: boolean; reason: string;
  brilliant_evidence: Record<string, unknown> | null;
};
export type GameSideSummary = {
  accuracy: number | null; provisional_accuracy: number | null; counts: Record<GameMoveCategory, number>;
  total_moves: number; analysed_moves: number; assessed_moves: number; accuracy_moves: number;
  coverage: number; complete: boolean; accuracy_complete: boolean;
};
export type GameAnalysisReport = {
  background_requested: boolean;
  id: number; game_id: number; game: GameAnalysisSource & { initial_fen: string };
  status: 'ready' | 'running' | 'completed' | 'failed'; cursor: number; total_plies: number;
  analysis_version: number; analysis_outdated: boolean; error: string | null; created_at: string; updated_at: string;
  summary: { white: GameSideSummary; black: GameSideSummary; complete: boolean };
  moves: AnalysedGameMove[]; method?: Record<string, unknown>; category_names?: Record<string, string>;
};
