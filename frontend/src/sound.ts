export type Sound = 'move' | 'capture' | 'wrong' | 'complete';
const files: Record<Sound, string> = {
  move: new URL('./sounds/move.wav', import.meta.url).href,
  capture: new URL('./sounds/capture.wav', import.meta.url).href,
  wrong: new URL('./sounds/wrong.wav', import.meta.url).href,
  complete: new URL('./sounds/complete.wav', import.meta.url).href,
};
const preferenceKey = 'tactics.sound', volumeKey = 'tactics.volume';
let enabled = true, volume = 0.8, error = '';
try {
  enabled = localStorage.getItem(preferenceKey) !== 'off';
  const saved = localStorage.getItem(volumeKey);
  if (saved !== null && Number.isFinite(Number(saved))) volume = Math.max(0, Math.min(1, Number(saved)));
} catch { /* Storage is optional. */ }
const listeners = new Set<() => void>();
export const soundEnabled = () => enabled;
export const soundVolume = () => volume;
export const soundError = () => error;
export function subscribeSound(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}
const notify = () => listeners.forEach(listener => listener());
type Voice = { audio: HTMLAudioElement; primed: boolean; generation: number };
const voices = new Map<Sound, Voice[]>();
const timers = new Set<number>();
let generation = 0;

function pool(sound: Sound) {
  let result = voices.get(sound);
  if (!result) {
    result = Array.from({ length: 2 }, () => {
      const audio = new Audio(files[sound]);
      audio.preload = 'auto';
      return { audio, primed: false, generation: 0 };
    });
    voices.set(sound, result);
  }
  return result;
}
function stopSounds() {
  generation++;
  timers.forEach(timer => window.clearTimeout(timer)); timers.clear();
  voices.forEach(group => group.forEach(voice => { voice.generation++; voice.audio.pause(); }));
}

// Prime the actual media elements during touchend/click, not only pointerdown:
// on mobile pointerdown may precede the browser's user-activation grant.
function unlockSound() {
  if (!enabled || document.visibilityState !== 'visible') return;
  try {
    for (const sound of Object.keys(files) as Sound[]) for (const voice of pool(sound)) {
      if (voice.primed || !voice.audio.paused) continue;
      const token = ++voice.generation;
      if (voice.audio.error) voice.audio.load();
      voice.audio.muted = true;
      void voice.audio.play().then(() => {
        voice.primed = true;
        if (token === voice.generation) { voice.audio.pause(); voice.audio.currentTime = 0; voice.audio.muted = false; }
      }).catch(() => { /* Retry on the next trusted gesture; never leave a pending lock. */ });
    }
  } catch { /* Audio support must not affect gameplay. */ }
}
export function setSoundVolume(value: number) {
  volume = Math.max(0, Math.min(1, value));
  voices.forEach(group => group.forEach(({ audio }) => { audio.volume = volume; }));
  try { localStorage.setItem(volumeKey, String(volume)); } catch { /* Keep in memory. */ }
  notify();
}
export function toggleSound() {
  enabled = !enabled; error = '';
  if (enabled) unlockSound(); else stopSounds();
  try { localStorage.setItem(preferenceKey, enabled ? 'on' : 'off'); } catch { /* Keep in memory. */ }
  notify();
}
export function listenForSoundGestures() {
  const gesture = (event: Event) => { if (event.isTrusted) unlockSound(); };
  const visibility = () => { if (document.visibilityState !== 'visible') stopSounds(); };
  const storage = (event: StorageEvent) => {
    if (event.key === preferenceKey || event.key === null) enabled = event.newValue !== 'off';
    if (event.key === volumeKey || event.key === null) volume = event.newValue == null ? .8 : Math.max(0, Math.min(1, Number(event.newValue) || 0));
    if (!enabled) stopSounds();
    voices.forEach(group => group.forEach(({ audio }) => { audio.volume = volume; }));
    notify();
  };
  const events = ['pointerup', 'touchend', 'click', 'keydown'] as const;
  events.forEach(type => document.addEventListener(type, gesture, { capture: true, passive: true }));
  document.addEventListener('visibilitychange', visibility);
  window.addEventListener('pagehide', stopSounds);
  window.addEventListener('storage', storage);
  return () => {
    events.forEach(type => document.removeEventListener(type, gesture, true));
    document.removeEventListener('visibilitychange', visibility);
    window.removeEventListener('pagehide', stopSounds);
    window.removeEventListener('storage', storage);
    stopSounds();
  };
}
export function playSound(sound: Sound, delay = 0) {
  if (!enabled || volume === 0 || document.visibilityState !== 'visible') return;
  if (delay > 0) {
    const token = generation, timer = window.setTimeout(() => {
      timers.delete(timer);
      if (token === generation) playSound(sound);
    }, delay * 1000);
    timers.add(timer); return;
  }
  try {
    const group = pool(sound), voice = group.find(item => item.audio.paused) || group[0];
    const token = ++voice.generation;
    if (voice.audio.error) voice.audio.load();
    voice.audio.pause(); voice.audio.currentTime = 0;
    voice.audio.muted = false; voice.audio.volume = volume;
    void voice.audio.play().then(() => {
      voice.primed = true;
      if (error) { error = ''; notify(); }
    }).catch((reason: unknown) => {
      if (token !== voice.generation || (reason instanceof DOMException && reason.name === 'AbortError')) return;
      error = 'Не удалось включить звук. Нажмите «Проверить звук» ещё раз.'; notify();
    });
  } catch { error = 'Звук недоступен в этом браузере.'; notify(); }
}
export function previewSound() {
  if (!enabled) toggleSound();
  if (!volume) setSoundVolume(.8);
  playSound('complete');
}
