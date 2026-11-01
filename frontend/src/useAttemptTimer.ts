import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';

type TimerOptions = {
  attemptId: number | null;
  elapsedMs: number | null;
  running: boolean;
  onPersist: (attemptId: number, elapsedMs: number, keepalive: boolean) => void;
};

type Clock = {
  id: number | null;
  accumulated: number;
  startedAt: number | null;
  tracked: boolean;
  acknowledgedMs: number;
  lastSentMs: number;
  lastSentAt: number;
  lastKeepalive: boolean;
};

const SAVE_INTERVAL = 15_000;
const baseline = (value: number | null) => value !== null && Number.isFinite(value) ? Math.max(0, value) : 0;
const read = (clock: Clock, now: number) => clock.accumulated + (clock.startedAt === null ? 0 : Math.max(0, now - clock.startedAt));

/** Counts only visible, enabled solving time; requests and replay can pause it. */
export function useAttemptTimer({ attemptId, elapsedMs, running, onPersist }: TimerOptions) {
  const clock = useRef<Clock>({ id: attemptId, accumulated: baseline(elapsedMs), startedAt: null, tracked: false, acknowledgedMs: elapsedMs ?? -1, lastSentMs: -1, lastSentAt: 0, lastKeepalive: false });
  const persist = useRef(onPersist);
  const enabled = useRef(running);
  const mounted = useRef(false);
  const pageSuspended = useRef(false);
  const [display, setDisplay] = useState(baseline(elapsedMs));

  const publish = useCallback((value: number) => {
    if (mounted.current) setDisplay(previous => previous === value ? previous : value);
  }, []);

  const save = useCallback((keepalive: boolean, now: number, force = false) => {
    const current = clock.current;
    if (current.id === null || !current.tracked) return;
    const value = Math.floor(read(current, now));
    if (value <= current.acknowledgedMs) return;
    if (!force && value === current.lastSentMs && now - current.lastSentAt < SAVE_INTERVAL && (!keepalive || current.lastKeepalive)) return;
    current.lastSentMs = value;
    current.lastSentAt = now;
    current.lastKeepalive = keepalive;
    persist.current(current.id, value, keepalive);
  }, []);

  const stop = useCallback((keepalive: boolean, force = false) => {
    const now = performance.now();
    const current = clock.current;
    current.accumulated = read(current, now);
    current.startedAt = null;
    const value = Math.floor(current.accumulated);
    publish(value);
    save(keepalive, now, force);
    return value;
  }, [publish, save]);

  const resume = useCallback(() => {
    const current = clock.current;
    if (current.id === null || !enabled.current || pageSuspended.current || document.visibilityState !== 'visible' || current.startedAt !== null) return;
    const now = performance.now();
    current.startedAt = now;
    current.lastSentAt = now;
    current.tracked = true;
  }, []);

  useLayoutEffect(() => {
    mounted.current = true;
    persist.current = onPersist;
    enabled.current = running;
  });

  useLayoutEffect(() => {
    const now = performance.now();
    if (clock.current.id !== attemptId) {
      stop(false, true);
      clock.current = { id: attemptId, accumulated: baseline(elapsedMs), startedAt: null, tracked: false, acknowledgedMs: elapsedMs ?? -1, lastSentMs: -1, lastSentAt: now, lastKeepalive: false };
    } else {
      // A heartbeat response may contain an earlier snapshot. Never add that
      // snapshot to local time, and never move the displayed clock backwards.
      clock.current.accumulated = Math.max(read(clock.current, now), baseline(elapsedMs));
      clock.current.acknowledgedMs = Math.max(clock.current.acknowledgedMs, elapsedMs ?? -1);
      if (clock.current.startedAt !== null) clock.current.startedAt = now;
    }
    if (!running) stop(false);
    else resume();
    publish(Math.floor(read(clock.current, now)));
  }, [attemptId, elapsedMs, running, publish, resume, stop]);

  useEffect(() => {
    const visibilityChanged = () => {
      if (document.visibilityState === 'visible') resume();
      else stop(true);
    };
    const pageHidden = () => { pageSuspended.current = true; stop(true); };
    const pageShown = () => { pageSuspended.current = false; resume(); };
    document.addEventListener('visibilitychange', visibilityChanged);
    window.addEventListener('pagehide', pageHidden);
    window.addEventListener('pageshow', pageShown);
    const tick = window.setInterval(() => {
      const now = performance.now();
      const current = clock.current;
      if (current.startedAt === null) {
        // The callback has no acknowledgement return value. A newer server
        // baseline acknowledges a save; until then retry even during a pause.
        if (!pageSuspended.current && now - current.lastSentAt >= SAVE_INTERVAL) save(document.visibilityState !== 'visible', now);
        return;
      }
      // Reconcile visibility even if a browser delayed its visibility event.
      if (document.visibilityState !== 'visible') { stop(true); return; }
      publish(Math.floor(read(current, now)));
      if (now - current.lastSentAt >= SAVE_INTERVAL) save(false, now);
    }, 250);
    return () => {
      mounted.current = false;
      window.clearInterval(tick);
      document.removeEventListener('visibilitychange', visibilityChanged);
      window.removeEventListener('pagehide', pageHidden);
      window.removeEventListener('pageshow', pageShown);
      stop(true, true);
    };
  }, [publish, resume, save, stop]);

  const snapshot = useCallback(() => Math.floor(read(clock.current, performance.now())), []);
  const pause = useCallback(() => stop(false, true), [stop]);
  return { elapsedMs: display, snapshot, pause };
}
