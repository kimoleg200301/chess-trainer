import { useEffect } from 'react';

/** Keep the visible training screen awake without changing device settings. */
export function useScreenWakeLock(enabled: boolean) {
  useEffect(() => {
    if (!enabled || !('wakeLock' in navigator)) return;

    let disposed = false;
    let suspended = false;
    let pending = false;
    let retryRequested = false;
    let sentinel: WakeLockSentinel | null = null;
    const eligible = () => !disposed && !suspended && document.visibilityState === 'visible';
    const releaseSentinel = (lock: WakeLockSentinel) => {
      void lock.release().catch(() => { /* The browser may have already released it. */ });
    };
    const release = () => {
      const lock = sentinel;
      sentinel = null;
      if (lock && !lock.released) releaseSentinel(lock);
    };

    async function acquire() {
      if (!eligible() || (sentinel && !sentinel.released)) return;
      if (pending) { retryRequested = true; return; }
      pending = true;
      try {
        const lock = await navigator.wakeLock.request('screen');
        // A request can finish after navigation, hiding, or effect cleanup.
        if (!eligible()) { releaseSentinel(lock); return; }
        if (lock.released) return;
        sentinel = lock;
        lock.addEventListener('release', () => {
          if (sentinel === lock) sentinel = null;
        }, { once: true });
      } catch {
        // Unsupported policies or battery saving must not interrupt a puzzle.
        // Retry on the next interaction or return to the visible page.
      } finally {
        pending = false;
        const retry = retryRequested;
        retryRequested = false;
        if (retry && eligible()) void acquire();
      }
    }

    const visibilityChanged = () => {
      if (document.visibilityState === 'visible') void acquire();
      else release();
    };
    const pageHidden = () => { suspended = true; release(); };
    const pageShown = () => { suspended = false; void acquire(); };
    const interacted = () => { void acquire(); };
    document.addEventListener('visibilitychange', visibilityChanged);
    document.addEventListener('pointerdown', interacted, { passive: true });
    document.addEventListener('keydown', interacted);
    window.addEventListener('pagehide', pageHidden);
    window.addEventListener('pageshow', pageShown);
    void acquire();

    return () => {
      disposed = true;
      document.removeEventListener('visibilitychange', visibilityChanged);
      document.removeEventListener('pointerdown', interacted);
      document.removeEventListener('keydown', interacted);
      window.removeEventListener('pagehide', pageHidden);
      window.removeEventListener('pageshow', pageShown);
      release();
    };
  }, [enabled]);
}
