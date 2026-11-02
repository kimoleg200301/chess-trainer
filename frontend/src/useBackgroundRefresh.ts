import { useEffect, useRef } from 'react';

/** Poll saved results only. Server work never depends on this subscription. */
export function useBackgroundRefresh(refresh: () => Promise<void>, enabled: boolean) {
  const latest = useRef(refresh);
  latest.current = refresh;
  useEffect(() => {
    if (!enabled) return;
    let disposed = false, pending = false;
    const update = async () => {
      if (disposed || pending || document.visibilityState !== 'visible') return;
      pending = true;
      try { await latest.current(); } finally { pending = false; }
    };
    const returned = () => { void update(); };
    void update();
    const interval = window.setInterval(returned, 2500);
    document.addEventListener('visibilitychange', returned);
    window.addEventListener('pageshow', returned);
    window.addEventListener('online', returned);
    return () => {
      disposed = true;
      window.clearInterval(interval);
      document.removeEventListener('visibilitychange', returned);
      window.removeEventListener('pageshow', returned);
      window.removeEventListener('online', returned);
    };
  }, [enabled]);
}
