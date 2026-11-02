import { useSyncExternalStore } from 'react';
import { Volume2, VolumeX } from 'lucide-react';
import { soundEnabled, subscribeSound, toggleSound } from './sound';

export default function SoundToggle() {
  const enabled = useSyncExternalStore(subscribeSound, soundEnabled);
  const label = enabled ? 'Выключить звук' : 'Включить звук';
  return <button type="button" className="icon-button sound-toggle" aria-label={label} title={label} onClick={toggleSound}>
    {enabled ? <Volume2 size={19} aria-hidden="true" /> : <VolumeX size={19} aria-hidden="true" />}
  </button>;
}
