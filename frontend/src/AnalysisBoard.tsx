import { useLayoutEffect, useMemo, useRef } from 'react';
import { Chess } from 'chess.js';
import type { Square } from 'chess.js';
import { playSound } from './sound';

const pieceNames: Record<string, string> = { p: 'пешка', n: 'конь', b: 'слон', r: 'ладья', q: 'ферзь', k: 'король' };
const positionKey = (fen: string) => fen.split(' ').slice(0, 4).join(' ');

type BoardSnapshot = { fen: string; side: 'white' | 'black'; identity: number | string; visible: boolean };

export default function AnalysisBoard({ fen, side, lastMove, identity, visible }: {
  fen: string; side: 'white' | 'black'; lastMove: string | null; identity: number | string; visible: boolean;
}) {
  const grid = useRef<HTMLDivElement>(null);
  const previousPosition = useRef<BoardSnapshot | null>(null);
  const board = useMemo(() => new Chess(fen), [fen]);
  const files = side === 'black' ? 'hgfedcba' : 'abcdefgh';
  const ranks = side === 'black' ? '12345678' : '87654321';

  useLayoutEffect(() => {
    const currentFen = board.fen();
    const previous = previousPosition.current;
    previousPosition.current = { fen: currentFen, side, identity, visible };
    if (!previous || previous.identity !== identity || previous.side !== side
        || !previous.visible || !visible || document.visibilityState !== 'visible'
        || positionKey(previous.fen) === positionKey(currentFen)) return;
    const motion = window.matchMedia('(prefers-reduced-motion: reduce)');

    // Only one legal move can travel between squares. Selecting another game
    // or jumping over several moves shows the requested position immediately.
    const before = new Chess(previous.fen);
    let transition = before.moves({ verbose: true }).find(move => positionKey(move.after) === positionKey(currentFen));
    let backwards = false;
    if (!transition) {
      transition = board.moves({ verbose: true }).find(move => positionKey(move.after) === positionKey(previous.fen));
      backwards = true;
    }
    if (!transition) return;
    playSound(transition.captured ? 'capture' : 'move');
    if (motion.matches) return;

    const paths: { from: Square; to: Square }[] = [{ from: transition.from, to: transition.to }];
    if (transition.isKingsideCastle() || transition.isQueensideCastle()) {
      const rank = transition.from[1];
      paths.push({
        from: `${transition.isKingsideCastle() ? 'h' : 'a'}${rank}` as Square,
        to: `${transition.isKingsideCastle() ? 'f' : 'd'}${rank}` as Square,
      });
    }

    const animations: Animation[] = [];
    const movingSquares: HTMLElement[] = [];
    for (const path of paths) {
      const from = backwards ? path.to : path.from;
      const to = backwards ? path.from : path.to;
      const origin = grid.current?.querySelector<HTMLElement>(`[data-square="${from}"]`);
      const destination = grid.current?.querySelector<HTMLElement>(`[data-square="${to}"]`);
      const piece = destination?.querySelector<HTMLElement>('img');
      if (!origin || !destination || !piece?.animate) continue;
      const start = origin.getBoundingClientRect();
      const end = destination.getBoundingClientRect();
      destination.classList.add('analysis-square-moving');
      movingSquares.push(destination);
      animations.push(piece.animate([
        { transform: `translate(${start.left - end.left}px, ${start.top - end.top}px)` },
        { transform: 'translate(0, 0)' },
      ], { duration: 230, easing: 'cubic-bezier(.22,.61,.36,1)' }));
    }

    let cleared = false;
    const clear = () => {
      // A cancelled animation's promise settles after the next render. Do not
      // let that old cleanup remove the stacking layer of a newer movement.
      if (cleared) return;
      cleared = true;
      animations.forEach(animation => animation.cancel());
      movingSquares.forEach(square => square.classList.remove('analysis-square-moving'));
    };
    const preferenceChanged = () => { if (motion.matches) clear(); };
    const visibilityChanged = () => { if (document.visibilityState !== 'visible') clear(); };
    void Promise.all(animations.map(animation => animation.finished)).then(clear, clear);
    motion.addEventListener('change', preferenceChanged);
    document.addEventListener('visibilitychange', visibilityChanged);
    return () => {
      clear();
      motion.removeEventListener('change', preferenceChanged);
      document.removeEventListener('visibilitychange', visibilityChanged);
    };
  }, [board, side, identity, visible]);

  return <div ref={grid} className="analysis-position-board" role="img" aria-label={`Позиция партии. Доска со стороны ${side === 'white' ? 'белых' : 'чёрных'}.`}>
    {[...ranks].flatMap((rank, row) => [...files].map((file, col) => {
      const square = `${file}${rank}` as Square;
      const piece = board.get(square);
      const last = !!lastMove && [lastMove.slice(0, 2), lastMove.slice(2, 4)].includes(square);
      const check = piece?.type === 'k' && piece.color === board.turn() && board.isCheck();
      return <div key={square} data-square={square} className={`analysis-square ${(row + col) % 2 ? 'dark' : 'light'} ${last ? 'last-move' : ''} ${check ? 'in-check' : ''}`} title={`${square}${piece ? `: ${piece.color === 'w' ? 'белые' : 'чёрные'} ${pieceNames[piece.type]}` : ': пустое поле'}`}>
        {col === 0 && <span className="analysis-rank" aria-hidden="true">{rank}</span>}{row === 7 && <span className="analysis-file" aria-hidden="true">{file}</span>}
        {piece && <img src={`/pieces/${piece.color}${piece.type.toUpperCase()}.svg`} alt="" draggable={false} />}
      </div>;
    }))}
  </div>;
}
