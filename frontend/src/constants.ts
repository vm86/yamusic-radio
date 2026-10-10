export const MOODS: Record<string, string> = {
  sad: "Грустное",
  calm: "Спокойное",
  fun: "Весёлое",
  active: "Бодрое",
};

export const TRANSITIONS: Record<string, string> = {
  "bass swap": "Обмен басами",
  "eq blend": "Трёхполосный EQ",
  "filter sweep": "Фильтр-свип",
  crossfade: "Кроссфейд",
};

export const EFFECTS: Record<string, string> = {
  none: "Без эффектов",
  drop: "Райзер и удар",
  echo: "Эхо-аут",
};

export const QUALITY: Record<string, string> = {
  good: "Удачные",
  fair: "Допустимые",
  poor: "Неудачные",
};

export const APP_TITLE = "Яндекс Музыка DJ";
export const POLL_MS = 3000;
// After a mood is picked the radio waits this long for another one, so that
// a few picks in a row make one change of the station's setting, not several.
export const MOODS_DELAY_MS = 1500;
