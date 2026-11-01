import { useEffect, useRef } from 'react';
import { ArrowRight, CheckCircle2, LoaderCircle, Play, RotateCcw, Settings2, Target, X } from 'lucide-react';
import type { Meta, Stats } from './api';
import './training-setup.css';

export type TrainingSetupProps = {
  meta: Meta | null;
  stats: Stats | null;
  collection: string;
  onCollectionChange: (value: string) => void;
  theme: string;
  onThemeChange: (value: string) => void;
  minRating: number;
  maxRating: number;
  onMinRatingChange: (value: number) => void;
  onMaxRatingChange: (value: number) => void;
  mode: 'practice' | 'review';
  onModeChange: (value: 'practice' | 'review') => void;
  busy: boolean;
  error: string;
  onDismissError: () => void;
  onStart: () => void;
  onResume?: () => void;
  sessionSolved: number;
};

const fmt = new Intl.NumberFormat('ru-RU');
const presets = [
  { label: 'Разминка', min: 600, max: 1200 },
  { label: 'Практика', min: 1200, max: 1800 },
  { label: 'Вызов', min: 1800, max: 2400 },
];

export default function TrainingSetup({
  meta, stats, collection, onCollectionChange, theme, onThemeChange, minRating, maxRating, onMinRatingChange,
  onMaxRatingChange, mode, onModeChange, busy, error, onDismissError, onStart,
  onResume, sessionSolved,
}: TrainingSetupProps) {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { heading.current?.focus({ preventScroll: true }); }, []);
  return <section className="training-setup" aria-labelledby="training-setup-title">
    <div className="setup-intro">
      <span className="setup-emblem" aria-hidden="true"><Target size={25} /></span>
      <h1 ref={heading} tabIndex={-1} id="training-setup-title">Начните тренировку</h1>
      <p>Выберите мотив и комфортный уровень сложности.</p>
    </div>

    {onResume && <div className="setup-resume">
      <span>Текущая позиция</span>
      <button className="button secondary" type="button" disabled={busy} onClick={onResume}><Play size={16} /> Вернуться к позиции</button>
    </div>}

    <form className="panel setup-card" onSubmit={event => { event.preventDefault(); onStart(); }}>
      <fieldset className="setup-fields" disabled={busy}>
        <legend className="setup-card-label"><Settings2 size={16} aria-hidden="true" /> Параметры тренировки</legend>
        <div className="segmented" role="group" aria-label="Режим тренировки">
          <button type="button" aria-pressed={mode === 'practice'} className={mode === 'practice' ? 'active' : ''} onClick={() => onModeChange('practice')}><Target size={16} />Практика</button>
          <button type="button" aria-pressed={mode === 'review'} className={mode === 'review' ? 'active' : ''} onClick={() => onModeChange('review')}><RotateCcw size={16} />Повторение{(stats?.due_count || 0) > 0 && <span className="count-badge">{stats?.due_count}</span>}</button>
        </div>
        <label className="field-label" htmlFor="collection">База задач</label>
        <select id="collection" value={collection} onChange={event => onCollectionChange(event.target.value)} disabled={busy || !meta}>
          {(meta?.collections || [{ id: 'lichess', name: 'Lichess', count: 0 }, { id: 'public', name: 'Открытые партии', count: 0 }, { id: 'personal', name: 'Мои задачи', count: 0 }, { id: 'all', name: 'Все доступные', count: 0 }]).map(item => <option key={item.id} value={item.id}>{item.name} ({fmt.format(item.count)})</option>)}
        </select>
        <p className="collection-description">{collection === 'personal' ? 'Задачи из ваших партий Chess.com сохраняются автоматически. Сначала новые задачи из самых свежих партий, затем более старые. Пройденные и разобранные позиции автоматически не повторяются.' : collection === 'public' ? 'Тактические моменты из реальных партий, проверенные Stockfish.' : collection === 'all' ? 'Задачи Lichess, открытые партии и ваша личная коллекция.' : 'Готовые задачи Lichess с рейтингом по результатам решений.'}</p>
        {meta?.total_puzzles === 0 && <p className="collection-empty">В этой базе пока нет задач. Добавьте их в разделе «Мои партии» или выберите другую базу.</p>}
        <label className="field-label" htmlFor="theme">Тактический мотив</label>
        <select id="theme" value={theme} onChange={event => onThemeChange(event.target.value)} disabled={!meta || busy}>
          <option value="">Смешанные задачи</option>
          {meta?.themes.map(item => <option key={item.id} value={item.id}>{item.name} ({item.count})</option>)}
        </select>

        <div className="difficulty-label"><label className="field-label" htmlFor="rating-min">Рейтинг задач</label><span>{minRating}–{maxRating}</span></div>
        <div className="rating-fields">
          <label><span>От</span><input id="rating-min" type="number" min="0" max="4000" step="1" value={minRating} onChange={event => onMinRatingChange(Number(event.target.value))} required /></label>
          <span className="rating-dash" aria-hidden="true">—</span>
          <label><span>До</span><input aria-label="Верхняя граница рейтинга" type="number" min="0" max="4000" step="1" value={maxRating} onChange={event => onMaxRatingChange(Number(event.target.value))} required /></label>
        </div>
        <div className="difficulty-presets" role="group" aria-label="Уровень сложности">{presets.map(preset => <button type="button" key={preset.label} aria-pressed={minRating === preset.min && maxRating === preset.max} className={minRating === preset.min && maxRating === preset.max ? 'selected-preset' : ''} onClick={() => { onMinRatingChange(preset.min); onMaxRatingChange(preset.max); }}>{preset.label}</button>)}</div>
        {collection !== 'lichess' && <p className="collection-rating-note">Сложность задач, созданных из партий, оценивается приблизительно.</p>}
      </fieldset>

      {error && <div className="error-notice" role="alert"><span>{error}</span><button type="button" aria-label="Закрыть сообщение" onClick={onDismissError}><X size={16} /></button></div>}
      <button type="submit" className="button primary setup-start-button" disabled={busy || !meta}>
        <span>{onResume ? 'Начать новую тренировку' : 'Начать тренировку'}</span>
        {busy ? <LoaderCircle size={18} className="spinner" aria-hidden="true" /> : <ArrowRight size={18} />}
      </button>
      <p className="settings-footnote">{mode === 'review' ? 'Задачи для закрепления знакомых мотивов. Выбранные фильтры применяются и к повторению.' : meta ? `${fmt.format(meta.total_puzzles)} позиций в выбранной базе. Решайте в своём темпе.` : 'Загружаем библиотеку задач…'}</p>
    </form>

    {(stats || sessionSolved > 0) && <div className="setup-summary">
      <span><CheckCircle2 size={16} /> {sessionSolved > 0 ? `${fmt.format(sessionSolved)} решено в этой тренировке` : `${fmt.format(stats?.solved || 0)} задач решено`}</span>
      {(stats?.due_count || 0) > 0 && <span><RotateCcw size={15} /> {fmt.format(stats?.due_count || 0)} к повторению</span>}
    </div>}
  </section>;
}
