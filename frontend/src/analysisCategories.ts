import type { GameMoveCategory } from './api';

export const categories: { id: GameMoveCategory; name: string; symbol: string }[] = [
  { id: 'brilliant', name: 'Бриллиантовый', symbol: '!!' }, { id: 'great', name: 'Сильный', symbol: '!' },
  { id: 'best', name: 'Лучший', symbol: '★' }, { id: 'excellent', name: 'Отличный', symbol: '✓' },
  { id: 'good', name: 'Хороший', symbol: '·' }, { id: 'inaccuracy', name: 'Неточность', symbol: '?!' },
  { id: 'mistake', name: 'Ошибка', symbol: '?' }, { id: 'blunder', name: 'Зевок', symbol: '??' },
  { id: 'forced', name: 'Вынужденный', symbol: '→' }, { id: 'unassessed', name: 'Без оценки', symbol: '—' },
];
export const categoryInfo = (id: GameMoveCategory) => categories.find(item => item.id === id) || categories[categories.length - 1];
