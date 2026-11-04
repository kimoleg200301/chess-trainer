import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from './api';
import type { ChessComSyncState } from './api';

const rank = { idle: 0, ready: 1, running: 2, completed: 3, failed: 3 };
const AUTO_INTERVAL = 5 * 60 * 1000;

export function useChessComSync(enabled: boolean, onDataChanged?: () => void) {
  const [username, setInput] = useState(''), [state, setState] = useState<ChessComSyncState | null>(null);
  const [sending, setSending] = useState(false), [error, setError] = useState('');
  const [clock, setClock] = useState(Date.now());
  const saved = useRef(state), field = useRef(username), dirty = useRef(false), edit = useRef(0);
  const inFlight = useRef(false), mounted = useRef(true), changed = useRef(onDataChanged);
  changed.current = onDataChanged;
  const setUsername = (value: string) => { field.current = value; dirty.current = true; edit.current++; setInput(value); };
  const confirmUsername = (value: string) => {
    if (field.current.trim().toLowerCase() === value.toLowerCase()) {
      dirty.current = false; field.current = value.toLowerCase(); setInput(field.current);
    }
  };
  const acceptSync = useCallback((value: ChessComSyncState | null | undefined) => {
    if (!mounted.current || !value) return;
    const previous = saved.current;
    if (previous && (value.revision < previous.revision ||
        (value.revision === previous.revision && rank[value.status] < rank[previous.status]))) return;
    saved.current = value; setState(value);
    if (!dirty.current) { field.current = value.username || ''; setInput(field.current); }
    if (value.status === 'completed' && value.added_games > 0 &&
        (previous?.revision !== value.revision || previous.status !== 'completed')) changed.current?.();
  }, []);
  const synchronize = useCallback(async (automatic = false) => {
    const target = automatic ? saved.current?.username : field.current.trim();
    if (!target || inFlight.current || (automatic && dirty.current)) return;
    const version = edit.current;
    inFlight.current = true; setSending(true); setError('');
    try {
      const result = await api<ChessComSyncState>('/game-analysis/sync', { username: target, automatic });
      if (!mounted.current) return;
      if (version === edit.current && (!saved.current || result.revision >= saved.current.revision)) {
        dirty.current = false; field.current = result.username || ''; setInput(field.current);
      }
      acceptSync(result);
    } catch (reason) {
      if (mounted.current) setError(reason instanceof Error ? reason.message : 'Не удалось запустить синхронизацию. Повторите позже.');
    } finally { inFlight.current = false; if (mounted.current) setSending(false); }
  }, [acceptSync]);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    if (!enabled) return;
    const check = () => {
      const current = saved.current;
      if (document.visibilityState !== 'visible' || !current?.username || current.background_requested || dirty.current) return;
      if (current.requested_at && Date.now() - Date.parse(current.requested_at) < AUTO_INTERVAL) return;
      void synchronize(true);
    };
    check();
    const timer = window.setInterval(check, AUTO_INTERVAL);
    const clockTimer = window.setInterval(() => setClock(Date.now()), 1000);
    document.addEventListener('visibilitychange', check);
    window.addEventListener('online', check);
    window.addEventListener('pageshow', check);
    return () => {
      window.clearInterval(timer); window.clearInterval(clockTimer);
      document.removeEventListener('visibilitychange', check);
      window.removeEventListener('online', check); window.removeEventListener('pageshow', check);
    };
  }, [enabled, state?.username, synchronize]);
  const sameUsername = !!state?.username && username.trim().toLowerCase() === state.username;
  const waiting = !!state?.background_requested && sameUsername;
  const cooldown = sameUsername && state?.next_sync_at ? Math.max(0, Math.ceil((Date.parse(state.next_sync_at) - clock) / 1000)) : 0;
  return { username, setUsername, confirmUsername, state, sending, error, acceptSync, synchronize, waiting, cooldown };
}
