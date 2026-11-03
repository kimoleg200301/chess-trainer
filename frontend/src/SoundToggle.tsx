import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { Volume2, VolumeX, X } from 'lucide-react';
import { previewSound, soundEnabled, soundError, soundVolume, subscribeSound, setSoundVolume, toggleSound } from './sound';
import './sound.css';

export default function SoundToggle() {
  const enabled = useSyncExternalStore(subscribeSound, soundEnabled);
  const volume = useSyncExternalStore(subscribeSound, soundVolume);
  const error = useSyncExternalStore(subscribeSound, soundError);
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null), trigger = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') { setOpen(false); trigger.current?.focus(); } };
    document.addEventListener('pointerdown', outside); document.addEventListener('keydown', escape);
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', escape); };
  }, [open]);
  return <div className="sound-controls" ref={root}>
    <button ref={trigger} type="button" className={`icon-button sound-toggle ${error ? 'sound-attention' : ''}`} aria-label="Настройки звука" title={enabled && volume ? 'Звук включён' : 'Звук выключен'} aria-expanded={open} onClick={() => setOpen(value => !value)}>
      {enabled && volume ? <Volume2 size={19} aria-hidden="true" /> : <VolumeX size={19} aria-hidden="true" />}
    </button>
    {open && <div className="sound-popover" role="dialog" aria-label="Звук">
      <div><strong>Звук</strong><button className="icon-button" aria-label="Закрыть настройки звука" onClick={() => { setOpen(false); trigger.current?.focus(); }}><X size={16} /></button></div>
      <button className="button secondary" role="switch" aria-checked={enabled} onClick={toggleSound}>{enabled ? 'Звук включён' : 'Звук выключен'}</button>
      <label>Громкость <span>{Math.round(volume * 100)}%</span><input type="range" min="0" max="100" step="5" value={Math.round(volume * 100)} onChange={event => setSoundVolume(Number(event.target.value) / 100)} /></label>
      <button className="button primary" onClick={previewSound}><Volume2 size={16} />Проверить звук</button>
      <p role={error ? 'alert' : undefined}>{error || 'Если мелодию не слышно, проверьте громкость мультимедиа телефона и разрешение звука для сайта.'}</p>
    </div>}
  </div>;
}
