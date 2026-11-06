import { useEffect } from 'react';

// Silent media and the playback workaround are from NoSleep.js 0.12.0 (MIT).
// See /wake-lock/NOTICE.txt. A silent, unmuted audio track is intentional: muting
// a detached video can prevent browsers from treating it as sleep-blocking media.
const fallbackSources = [
  ['video/webm', new URL('./assets/wake-lock/keep-awake.webm', import.meta.url).href],
  ['video/mp4', new URL('./assets/wake-lock/keep-awake.mp4', import.meta.url).href],
] as const;

/** Keep the visible training screen awake without changing device settings. */
export function useScreenWakeLock(enabled: boolean) {
  useEffect(() => {
    if (!enabled) return;

    let disposed = false;
    let suspended = false;
    let pending = false;
    let retryRequested = false;
    let releaseRecoveryUsed = false;
    let sentinel: WakeLockSentinel | null = null;
    // Prefer the native API. A native denial is not bypassed with media playback.
    const video = 'wakeLock' in navigator ? null : document.createElement('video');
    const metadataLoaded = () => { if (video) video.loop = video.duration <= 1; };
    const playbackAdvanced = () => {
      // Seeking within the tiny MP4 keeps playback continuous on older Safari.
      if (video && video.duration > 1 && video.currentTime > 0.5) video.currentTime = Math.random();
    };
    if (video) {
      video.dataset.screenWakeLock = 'fallback';
      video.playsInline = true;
      video.setAttribute('webkit-playsinline', '');
      video.controls = false;
      video.disablePictureInPicture = true;
      video.disableRemotePlayback = true;
      video.loop = true;
      for (const [type, src] of fallbackSources) {
        const source = document.createElement('source');
        source.type = type; source.src = src; video.appendChild(source);
      }
      video.addEventListener('loadedmetadata', metadataLoaded);
      video.addEventListener('timeupdate', playbackAdvanced);
    }
    const eligible = () => !disposed && !suspended && document.visibilityState === 'visible';
    const releaseSentinel = (lock: WakeLockSentinel) => {
      void lock.release().catch(() => { /* The browser may have already released it. */ });
    };
    const release = () => {
      const lock = sentinel;
      sentinel = null;
      if (lock && !lock.released) releaseSentinel(lock);
      video?.pause();
    };

    async function acquire() {
      if (!eligible() || (sentinel && !sentinel.released) || (video && !video.paused)) return;
      if (pending) { retryRequested = true; return; }
      pending = true;
      try {
        if (video) {
          // Call play synchronously here so trusted interaction handlers retain
          // their user activation. Autoplay rejection waits for the next gesture.
          await video.play();
          if (!eligible()) video.pause();
          return;
        }
        const lock = await navigator.wakeLock.request('screen');
        // A request can finish after navigation, hiding, or effect cleanup.
        if (!eligible()) { releaseSentinel(lock); return; }
        if (lock.released) return;
        sentinel = lock;
        lock.addEventListener('release', () => {
          if (sentinel !== lock) return;
          sentinel = null;
          // Recover one unexpected release while thinking, without fighting an
          // OS/battery policy that immediately releases every replacement lock.
          if (eligible() && !releaseRecoveryUsed) {
            releaseRecoveryUsed = true;
            void acquire();
          }
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
      if (document.visibilityState === 'visible') { releaseRecoveryUsed = false; void acquire(); }
      else release();
    };
    const pageHidden = () => { suspended = true; release(); };
    const pageShown = () => { suspended = false; releaseRecoveryUsed = false; void acquire(); };
    const interacted = (event: Event) => {
      if (!event.isTrusted) return;
      releaseRecoveryUsed = false;
      void acquire();
    };
    document.addEventListener('visibilitychange', visibilityChanged);
    // Touch activation is granted on pointerup/click, not pointerdown.
    document.addEventListener('pointerup', interacted, { passive: true });
    document.addEventListener('click', interacted, { passive: true });
    document.addEventListener('keydown', interacted);
    window.addEventListener('pagehide', pageHidden);
    window.addEventListener('pageshow', pageShown);
    void acquire();

    return () => {
      disposed = true;
      document.removeEventListener('visibilitychange', visibilityChanged);
      document.removeEventListener('pointerup', interacted);
      document.removeEventListener('click', interacted);
      document.removeEventListener('keydown', interacted);
      window.removeEventListener('pagehide', pageHidden);
      window.removeEventListener('pageshow', pageShown);
      release();
      if (video) {
        video.removeEventListener('loadedmetadata', metadataLoaded);
        video.removeEventListener('timeupdate', playbackAdvanced);
        video.replaceChildren();
        video.load();
      }
    };
  }, [enabled]);
}
