import { useCallback, useEffect, useRef, useState } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import { api } from './api';
import type { GameAnalysisSource, GameFavoriteState } from './api';

const keyOf = (game: GameAnalysisSource) => game.favorite_key || String(game.id);

export function useGameFavorites(setGames: Dispatch<SetStateAction<GameAnalysisSource[]>>) {
  const saved = useRef(new Map<string, GameFavoriteState>()), pending = useRef(new Set<string>());
  const mounted = useRef(true);
  const [saving, setSaving] = useState(new Set<string>()), [error, setError] = useState('');
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const mergeFavorites = useCallback((games: GameAnalysisSource[]) => games.map(game => {
    const key = keyOf(game), current = saved.current.get(key);
    const incoming = { favorite_key: key, is_favorite: !!game.is_favorite, favorite_revision: game.favorite_revision || 0 };
    const value = current && current.favorite_revision > incoming.favorite_revision ? current : incoming;
    saved.current.set(key, value);
    return { ...game, ...value };
  }), []);
  const toggleFavorite = useCallback(async (game: GameAnalysisSource) => {
    const key = keyOf(game);
    if (pending.current.has(key)) return;
    pending.current.add(key); setSaving(new Set(pending.current)); setError('');
    try {
      const result = await api<GameFavoriteState>(`/game-analysis/games/${game.id}/favorite`, { is_favorite: !game.is_favorite });
      if (!mounted.current) return;
      const current = saved.current.get(result.favorite_key);
      if (!current || result.favorite_revision >= current.favorite_revision) saved.current.set(result.favorite_key, result);
      setGames(previous => mergeFavorites(previous));
    } catch (reason) {
      if (mounted.current) setError(reason instanceof Error ? reason.message : 'Не удалось сохранить избранное. Попробуйте ещё раз.');
    } finally {
      pending.current.delete(key);
      if (mounted.current) setSaving(new Set(pending.current));
    }
  }, [mergeFavorites, setGames]);
  return { mergeFavorites, toggleFavorite, savingFavorite: (game: GameAnalysisSource) => saving.has(keyOf(game)),
    favoriteError: error, clearFavoriteError: () => setError('') };
}
