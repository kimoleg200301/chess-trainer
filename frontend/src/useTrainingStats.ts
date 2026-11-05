import { useCallback, useLayoutEffect, useRef, useState } from 'react';
import { api, captureSession } from './api';
import type { Stats } from './api';

/** Keep the latest statistics within the lifetime of the authenticated account. */
export function useTrainingStats(user: string | null, onError: (message: string) => void) {
  const [stats, setStats] = useState<Stats | null>(null);
  const [busy, setBusy] = useState(false);
  const requestVersion = useRef(0);
  const active = useRef(false);

  useLayoutEffect(() => {
    active.current = user !== null;
    requestVersion.current++;
    setStats(null);
    setBusy(false);
    return () => { active.current = false; requestVersion.current++; };
  }, [user]);

  const refresh = useCallback(async () => {
    if (!active.current) return;
    const version = ++requestVersion.current;
    const isCurrentSession = captureSession();
    const isCurrent = () => active.current && version === requestVersion.current && isCurrentSession();
    setBusy(true);
    try {
      const result = await api<Stats>('/stats');
      if (isCurrent()) setStats(result);
    } catch (error) {
      if (isCurrent()) onError(error instanceof Error ? error.message : 'Не удалось загрузить статистику. Попробуйте ещё раз.');
    } finally {
      if (isCurrent()) setBusy(false);
    }
  }, [onError]);

  return { stats, busy, refresh };
}
