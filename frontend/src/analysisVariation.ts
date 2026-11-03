import { Chess } from 'chess.js';
import type { AnalysedGameMove } from './api';

/** Replay the legal prefix from the position before the reviewed move. */
export function analysisVariation(move: AnalysedGameMove | undefined) {
  if (!move) return [];
  const board = new Chess(move.before_fen);
  const positions: { fen: string; lastMove: string | null; san: string; label: string }[] = [
    { fen: board.fen(), lastMove: null, san: '', label: 'Исходная позиция варианта' },
  ];
  for (const uci of move.best_line_uci) {
    if (!/^[a-h][1-8][a-h][1-8][qrbn]?$/.test(uci)) break;
    try {
      const number = board.moveNumber(), separator = board.turn() === 'w' ? '.' : '…';
      const played = board.move({ from: uci.slice(0, 2), to: uci.slice(2, 4), promotion: uci[4] });
      positions.push({ fen: board.fen(), lastMove: uci, san: played.san, label: `${number}${separator} ${played.san}` });
    } catch { break; }
  }
  return positions;
}
