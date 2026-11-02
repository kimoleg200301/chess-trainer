export type Sound = 'move' | 'capture' | 'wrong' | 'complete';
const preferenceKey = 'tactics.sound';
let enabled = true;
try { enabled = localStorage.getItem(preferenceKey) !== 'off'; } catch { /* Private browsing may deny storage. */ }
const listeners = new Set<() => void>();
let context: AudioContext | null = null;
let resuming: Promise<void> | null = null;
let soundGeneration = 0;
const buffers = new Map<Sound, AudioBuffer>();
const playing = new Set<AudioBufferSourceNode>();

export const soundEnabled = () => enabled;
export function subscribeSound(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

function stopSounds() {
  soundGeneration++;
  for (const source of playing) {
    try { source.stop(); } catch { /* Already ended. */ }
    source.disconnect();
  }
  playing.clear();
}

// Called synchronously during a gesture, before any network request. Mobile
// browsers require this even when the eventual sound follows an API response.
function unlockSound() {
  if (!enabled || document.visibilityState !== 'visible') return;
  try {
    if (!context || context.state === 'closed') {
      const Audio = window.AudioContext ?? (window as Window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
      if (!Audio) return;
      context = new Audio();
      buffers.clear();
    }
    if (context.state !== 'running' && !resuming) {
      resuming = context.resume().catch(() => {}).finally(() => { resuming = null; });
    }
  } catch { /* Sound is optional; it must never interrupt a move. */ }
}

function setSoundEnabled(value: boolean) {
  enabled = value;
  if (enabled) unlockSound(); else stopSounds();
  listeners.forEach(listener => listener());
}

export function toggleSound() {
  setSoundEnabled(!enabled);
  try { localStorage.setItem(preferenceKey, enabled ? 'on' : 'off'); } catch { /* Keep the choice for this visit. */ }
}

export function listenForSoundGestures() {
  const gesture = (event: Event) => { if (event.isTrusted) unlockSound(); };
  const visibility = () => { if (document.visibilityState !== 'visible') stopSounds(); };
  const storage = (event: StorageEvent) => {
    if (event.key === preferenceKey || event.key === null) setSoundEnabled(event.newValue !== 'off');
  };
  const events = ['pointerdown', 'pointerup', 'touchend', 'keydown'] as const;
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

// Small, original synthesized sounds: no downloads or third-party recordings.
function makeBuffer(audio: AudioContext, sound: Sound) {
  const duration = sound === 'complete' ? 0.7 : sound === 'wrong' ? 0.24 : 0.13;
  const buffer = audio.createBuffer(1, Math.ceil(audio.sampleRate * duration), audio.sampleRate);
  const samples = buffer.getChannelData(0);
  let seed = 7;
  for (let i = 0; i < samples.length; i++) {
    const t = i / audio.sampleRate;
    if (sound === 'move' || sound === 'capture') {
      const frequency = sound === 'capture' ? 490 : 680;
      seed = (Math.imul(seed, 1664525) + 1013904223) | 0;
      const noise = seed / 2147483648;
      const attack = Math.min(1, t / 0.002);
      samples[i] = attack * (0.24 * Math.sin(2 * Math.PI * frequency * t) * Math.exp(-t * 55)
        + 0.09 * Math.sin(2 * Math.PI * frequency * 1.73 * t) * Math.exp(-t * 85)
        + 0.1 * noise * Math.exp(-t * 150));
    } else {
      const notes = sound === 'complete' ? [523.25, 659.25, 783.99] : [293.66, 246.94];
      for (let note = 0; note < notes.length; note++) {
        const age = t - note * (sound === 'complete' ? 0.12 : 0.09);
        if (age < 0) continue;
        const envelope = Math.min(1, age / 0.008) * Math.exp(-age * (sound === 'complete' ? 11 : 28));
        samples[i] += 0.13 * Math.sin(2 * Math.PI * notes[note] * age) * envelope;
      }
    }
    // Fade the last few milliseconds to avoid a click when the buffer ends.
    samples[i] *= Math.min(1, (duration - t) / 0.015);
  }
  return buffer;
}

export function playSound(sound: Sound, delay = 0) {
  // Drop suspended/hidden sounds instead of queuing stale feedback on return.
  if (!enabled || !context || document.visibilityState !== 'visible') return;
  if (context.state !== 'running') {
    // A click can update the board before the gesture's resume() settles.
    // Allow only this fresh feedback, and cancel it on mute/hide or a slow resume.
    const requestedAt = performance.now(), generation = soundGeneration;
    if (resuming) void resuming.then(() => {
      if (context?.state === 'running' && generation === soundGeneration && performance.now() - requestedAt < 200) playSound(sound, delay);
    });
    return;
  }
  try {
    let buffer = buffers.get(sound);
    if (!buffer) { buffer = makeBuffer(context, sound); buffers.set(sound, buffer); }
    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(context.destination);
    source.onended = () => { playing.delete(source); source.disconnect(); };
    playing.add(source);
    source.start(context.currentTime + delay);
  } catch { /* Unsupported or interrupted audio must not affect gameplay. */ }
}
