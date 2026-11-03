import { ArrowRight, CheckCircle2, CircleHelp, Clock3, RotateCcw, Search, TrendingUp } from 'lucide-react';
import type { Attempt } from './api';
import { formatDuration } from './time';
import './training-result.css';

export default function TrainingResult({ attempt, rating, busy, reviewing, onNext, onRetry, onReview }: {
  attempt: Attempt; rating: number; busy: boolean; reviewing: boolean;
  onNext: () => void; onRetry: () => void; onReview: () => void;
}) {
  const change = attempt.rating_change;
  const clean = attempt.status === 'completed' && !attempt.mistakes && !attempt.hints_used;
  return <div className="training-result" aria-label="Результат задачи">
    <div className="result-actions" aria-label="Действия после задачи">
      <button className="button secondary" disabled={busy} onClick={onRetry} title="Повторить без изменения рейтинга"><RotateCcw size={21} /><span>Повторить</span></button>
      <button className="button secondary" disabled={busy || reviewing} onClick={onReview}><Search size={21} /><span>Разбор</span></button>
      <button className="button primary result-next" aria-label="Следующая позиция" disabled={busy} onClick={onNext}><span>Следующая</span><ArrowRight size={26} /></button>
    </div>
    <section className="result-rating-card" aria-label="Рейтинг после задачи">
      <div className="result-rating-heading"><span><TrendingUp size={17} /> Ваш рейтинг</span><small>{change ? 'Результат сохранён' : 'Без изменения рейтинга'}</small></div>
      <div className="result-rating-value">
        {change && <><span className="rating-before">{change.before}</span><ArrowRight size={19} /></>}
        <strong>{change?.after ?? rating}</strong>
        {change && <b className={`rating-delta ${change.delta >= 0 ? 'positive' : 'negative'}`}>{change.delta > 0 ? '+' : ''}{change.delta}</b>}
      </div>
      <p>{change ? clean ? 'Чистое решение! Следующая задача подстроится под ваш уровень.' : 'Ошибка, подсказка или просмотр ответа учитываются как решение с помощью.' : 'Повторение, разбор и ручной подбор задач не меняют рейтинг.'}</p>
      <div className="result-facts"><span>{clean ? <CheckCircle2 size={15} /> : <CircleHelp size={15} />}{clean ? 'Без ошибок' : `Ошибок: ${attempt.mistakes} · Подсказок: ${attempt.hints_used}`}</span><span><Clock3 size={15} />{formatDuration(attempt.elapsed_ms)}</span></div>
    </section>
  </div>;
}
