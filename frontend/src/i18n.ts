import { computed, ref, watch } from "vue";
import { en } from "./locales/en";
import { ru, type MessageKey, type Messages } from "./locales/ru";

export type Locale = "ru" | "en";
export type LocaleMode = Locale | "auto";

// The order the button goes through them, starting with the system language.
export const LOCALE_MODES: readonly LocaleMode[] = ["auto", "ru", "en"];

const STORAGE_KEY = "locale";
const MESSAGES: Record<Locale, Messages> = { ru, en };

function load(): LocaleMode {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === "ru" || saved === "en") return saved;
  } catch {
    /* private mode */
  }
  return "auto";
}

function save(mode: LocaleMode): void {
  try {
    if (mode === "auto") localStorage.removeItem(STORAGE_KEY);
    else localStorage.setItem(STORAGE_KEY, mode);
  } catch {
    /* private mode */
  }
}

/** The first language of the browser that the page is translated to. */
export function systemLocale(languages: readonly string[] = navigator.languages): Locale {
  for (const language of languages) {
    const code = language.toLowerCase().split("-")[0];
    if (code === "ru") return "ru";
    if (code === "en") return "en";
  }
  return "en";
}

const mode = ref<LocaleMode>(load());
const locale = computed<Locale>(() => (mode.value === "auto" ? systemLocale() : mode.value));
const messages = computed(() => MESSAGES[locale.value]);

watch(
  locale,
  (value) => {
    document.documentElement.lang = value;
    document.title = messages.value["app.title"];
  },
  { immediate: true },
);
watch(mode, save);

/** A message in the language of the page; {name} is put in from params. */
export function t(key: MessageKey, params: Record<string, string | number> = {}): string {
  return messages.value[key].replace(/\{(\w+)\}/g, (_, name: string) => String(params[name] ?? ""));
}

/** The messages under a prefix by the rest of their keys: the names of the moods... */
function names(prefix: string): () => Record<string, string> {
  return () =>
    Object.fromEntries(
      Object.entries(messages.value)
        .filter(([key]) => key.startsWith(prefix))
        .map(([key, text]) => [key.slice(prefix.length), text]),
    );
}

export const moodNames = names("mood.");
export const transitionNames = names("transition.");
export const effectNames = names("effect.");
export const qualityNames = names("quality.");

export function useLocale() {
  return {
    mode,
    locale,
    setMode: (value: LocaleMode) => (mode.value = value),
    cycle: () => {
      const next = LOCALE_MODES.indexOf(mode.value) + 1;
      mode.value = LOCALE_MODES[next % LOCALE_MODES.length] ?? "auto";
    },
  };
}
