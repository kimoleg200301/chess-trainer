export type Theme = { id: string; name: string; count: number };
export type Meta = { total_puzzles: number; themes: Theme[]; min_rating: number; max_rating: number };
export type Attempt = {
  id: number; puzzle_id: string; fen: string; rating: number; themes: string[];
  status: 'active' | 'completed' | 'revealed'; turn: 'white' | 'black';
  solved_plies: number; total_plies: number; mistakes: number; hints_used: number;
  last_move: string | null;
};
export type Stats = {
  solved: number; total_attempts: number; accuracy: number; streak: number; due_count: number;
  by_theme: { id: string; name: string; attempts: number; solved: number }[];
  recent: { puzzle_id: string; rating: number; status: string; created_at: string }[];
};
export type Analysis = { evaluation: string; line: string[]; engine: string };
export type Reveal = { attempt: Attempt; solution: { uci: string; san: string }[]; game_url: string };

export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}

export async function api<T>(path: string, body?: unknown, method?: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      method: method || (body === undefined ? 'GET' : 'POST'), credentials: 'same-origin',
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch { throw new ApiError('Не удалось связаться с сервером. Проверьте подключение и попробуйте ещё раз.', 0); }
  if (!response.ok) {
    let message = response.status === 401 ? 'Войдите в аккаунт, чтобы продолжить.' : `Ошибка сервера (${response.status}). Попробуйте ещё раз.`;
    try { const data = await response.json(); if (typeof data.detail === 'string') message = data.detail; } catch { /* Keep readable fallback. */ }
    if (response.status === 401 && path !== '/auth/login') window.dispatchEvent(new Event('session-expired'));
    throw new ApiError(message, response.status);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}
