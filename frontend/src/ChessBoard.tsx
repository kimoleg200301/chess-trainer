import { useEffect, useMemo, useRef, useState } from 'react';
import { Chess } from 'chess.js';
import type { Square } from 'chess.js';
import type { Attempt } from './api';

const names: Record<string, string> = { p: 'пешка', n: 'конь', b: 'слон', r: 'ладья', q: 'ферзь', k: 'король' };
const initialFen = new Chess().fen();

export default function ChessBoard({ attempt, busy, hintSquare, onMove }: {
  attempt: Attempt | null; busy: boolean; hintSquare: string | null; onMove: (move: string) => void;
}) {
  const [selected, setSelected] = useState<Square | null>(null);
  const [promotion, setPromotion] = useState<{ from: Square; to: Square } | null>(null);
  const grid = useRef<HTMLDivElement>(null);
  const dialog = useRef<HTMLDivElement>(null);
  const position = useMemo(() => new Chess(attempt?.fen || initialFen), [attempt?.fen]);
  const color = attempt?.turn === 'black' ? 'b' : 'w';
  const flipped = color === 'b';
  const files = flipped ? 'hgfedcba' : 'abcdefgh';
  const ranks = flipped ? '12345678' : '87654321';
  const playable = !!attempt && attempt.status === 'active' && !busy && !promotion;
  const targets = selected ? position.moves({ square: selected, verbose: true }).map(move => move.to) : [];
  useEffect(() => {
    if (!promotion) return;
    const previousFocus = document.activeElement as HTMLElement | null;
    dialog.current?.querySelector('button')?.focus();
    return () => { if (previousFocus?.isConnected) previousFocus.focus(); };
  }, [promotion]);

  function choose(from: Square, to: Square) {
    if (!playable) return;
    const moves = position.moves({ square: from, verbose: true }).filter(move => move.to === to);
    if (!moves.length) { setSelected(null); return; }
    if (moves.some(move => move.promotion)) setPromotion({ from, to });
    else onMove(`${from}${to}`);
    setSelected(null);
  }

  function click(square: Square) {
    if (!playable) return;
    const piece = position.get(square);
    if (selected === square) { setSelected(null); return; }
    if (piece?.color === color) { setSelected(square); return; }
    if (selected) choose(selected, square);
  }

  return <div className="board-frame">
    <div ref={grid} className={`chessboard ${!attempt ? 'board-idle' : ''}`} aria-label="Шахматная доска. Выберите фигуру, затем поле назначения. Стрелки перемещают фокус.">
      {[...ranks].flatMap((rank, row) => [...files].map((file, column) => {
        const square = `${file}${rank}` as Square;
        const piece = position.get(square);
        const light = (row + column) % 2 === 0;
        const last = !!attempt?.last_move && [attempt.last_move.slice(0, 2), attempt.last_move.slice(2, 4)].includes(square);
        const check = piece?.type === 'k' && piece.color === position.turn() && position.isCheck();
        return <button key={square} type="button" className={`square ${light ? 'light' : 'dark'} ${last ? 'last-move' : ''} ${selected === square ? 'selected' : ''} ${hintSquare === square ? 'hinted' : ''} ${check ? 'in-check' : ''}`}
          aria-label={`${square}${piece ? `, ${piece.color === 'w' ? 'белые' : 'чёрные'} ${names[piece.type]}` : ', пустое поле'}`}
          aria-pressed={selected === square} aria-disabled={!playable}
          onClick={() => click(square)}
          draggable={playable && piece?.color === color}
          onDragStart={event => { if (!playable || piece?.color !== color) { event.preventDefault(); return; } event.dataTransfer.setData('text/plain', square); event.dataTransfer.effectAllowed = 'move'; setSelected(square); }}
          onDragOver={event => { if (playable) { event.preventDefault(); event.dataTransfer.dropEffect = 'move'; } }}
          onDrop={event => { event.preventDefault(); const from = event.dataTransfer.getData('text/plain'); if (/^[a-h][1-8]$/.test(from)) choose(from as Square, square); }}
          onKeyDown={event => {
            const delta: Record<string, number> = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -8, ArrowDown: 8 };
            if (event.key === 'Escape') { setSelected(null); return; }
            if (delta[event.key] !== undefined) { event.preventDefault(); const index = Math.max(0, Math.min(63, row * 8 + column + delta[event.key])); (grid.current?.children[index] as HTMLElement)?.focus(); }
          }}>
          {column === 0 && <span className="rank-label" aria-hidden="true">{rank}</span>}
          {row === 7 && <span className="file-label" aria-hidden="true">{file}</span>}
          {piece && <img src={`/pieces/${piece.color}${piece.type.toUpperCase()}.svg`} alt="" draggable={false} className="piece" />}
          {targets.includes(square) && <span className={piece ? 'move-ring' : 'move-dot'} aria-hidden="true" />}
        </button>;
      }))}
    </div>
    {promotion && <div className="promotion-backdrop"><div ref={dialog} className="promotion-picker" role="dialog" aria-modal="true" aria-label="Выберите фигуру для превращения" onKeyDown={event => {
      if (event.key === 'Escape') { event.preventDefault(); setPromotion(null); }
      if (event.key === 'Tab') {
        const buttons = dialog.current?.querySelectorAll('button');
        if (!buttons?.length) return;
        const first = buttons[0], last = buttons[buttons.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    }}>
      <p>Превращение пешки</p><div>{['q', 'r', 'b', 'n'].map(piece => <button key={piece} aria-label={names[piece]} onClick={() => { onMove(`${promotion.from}${promotion.to}${piece}`); setPromotion(null); }}><img src={`/pieces/${color}${piece.toUpperCase()}.svg`} alt={names[piece]} /></button>)}</div>
      <button className="text-button" onClick={() => setPromotion(null)}>Отмена</button>
    </div></div>}
  </div>;
}
