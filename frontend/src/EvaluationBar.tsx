import { Chess } from 'chess.js';
import type { AnalysedGameMove } from './api';

export function positionEvaluation(move: AnalysedGameMove | undefined, before: boolean, fen: string, variation = false) {
  const board = new Chess(fen);
  if (board.isCheckmate()) {
    const whiteWins = board.turn() === 'b';
    return { label: '#', description: whiteWins ? 'Мат. Победа белых' : 'Мат. Победа чёрных', whitePercent: whiteWins ? 100 : 0, known: true };
  }
  if (board.isStalemate() || board.isInsufficientMaterial()) return { label: '0.0', description: 'Ничья', whitePercent: 50, known: true };
  const score = move && !variation ? before ? move.score_before : move.score_after : null;
  const mate = move && !variation ? before ? move.mate_before : move.mate_after : null;
  const sign = move?.side === 'black' ? -1 : 1;
  if (mate != null) {
    const whiteWins = mate * sign > 0;
    return { label: `${whiteWins ? '+' : '−'}M${Math.abs(mate)}`, description: `Мат в ${Math.abs(mate)}. Преимущество ${whiteWins ? 'белых' : 'чёрных'}`, whitePercent: whiteWins ? 100 : 0, known: true };
  }
  if (score == null) return { label: '—', description: 'Для этой позиции оценка не рассчитана', whitePercent: 50, known: false };
  const pawns = score * sign / 100;
  return {
    label: `${pawns > 0 ? '+' : ''}${pawns.toFixed(1)}`,
    description: pawns === 0 ? 'Равная позиция' : `Преимущество ${pawns > 0 ? 'белых' : 'чёрных'}: ${Math.abs(pawns).toFixed(2)} пешки`,
    whitePercent: 50 + 50 * Math.tanh(pawns / 4), known: true,
  };
}

export default function EvaluationBar({ evaluation, side }: { evaluation: ReturnType<typeof positionEvaluation>; side: 'white' | 'black' }) {
  return <div className={`evaluation-bar ${side === 'black' ? 'reversed' : ''} ${evaluation.known ? '' : 'unknown'}`} role="img" aria-label={`Оценка позиции: ${evaluation.description}`} title={`${evaluation.description}. Оценка Stockfish в пешках, «+» за белых, «−» за чёрных.`}>
    <div className="evaluation-white" style={{ height: `${evaluation.whitePercent}%` }} />
    <strong>{evaluation.label}</strong>
  </div>;
}
