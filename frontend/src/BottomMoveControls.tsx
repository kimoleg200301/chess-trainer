import type { ReactNode } from 'react';
import './move-layout.css';

/** One viewport-anchored control bar for game review and puzzle replay. */
export default function BottomMoveControls({ label, children }: { label: string; children: ReactNode }) {
  return <div className="move-navigation-dock" role="group" aria-label={label}>
    <div className="move-navigation-inner">{children}</div>
  </div>;
}
