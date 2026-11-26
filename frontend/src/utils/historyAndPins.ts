
import { HistoryItem, PinItem } from '../components/common/types';

const HISTORY_KEY = 'legislation-explorer:history';
const PINS_KEY = 'legislation-explorer:pins';

export function getHistory(): HistoryItem[] {
  try {
    return JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]');
  } catch { return []; }
}

export function addHistory(item: HistoryItem) {
  const hist = getHistory().filter(h => !(h.act === item.act && h.section === item.section));
  hist.unshift(item);
  localStorage.setItem(HISTORY_KEY, JSON.stringify(hist.slice(0, 10)));
}

export function getPins(): PinItem[] {
  try {
    return JSON.parse(localStorage.getItem(PINS_KEY) || '[]');
  } catch { return []; }
}
