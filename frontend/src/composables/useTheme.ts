import { ref, watch } from "vue";

export type ThemeMode = "light" | "dark" | "auto";

// The order the button goes through them, starting with the system theme.
export const THEME_MODES: readonly ThemeMode[] = ["auto", "light", "dark"];

const STORAGE_KEY = "theme";

type Scheme = "light" | "dark";

function load(): ThemeMode {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === "light" || saved === "dark") return saved;
  } catch {
    /* private mode */
  }
  return "auto";
}

function save(mode: ThemeMode): void {
  try {
    if (mode === "auto") localStorage.removeItem(STORAGE_KEY);
    else localStorage.setItem(STORAGE_KEY, mode);
  } catch {
    /* private mode */
  }
}

/** The surface color of a scheme, read from the tokens as the browser sees it. */
export function surfaceColor(scheme: Scheme): string | null {
  const probe = document.createElement("div");
  probe.style.cssText = `position:absolute;visibility:hidden;color-scheme:${scheme};background:var(--md-sys-color-surface)`;
  document.documentElement.append(probe);
  const color = getComputedStyle(probe).backgroundColor;
  probe.remove();
  const match = /^rgba?\((\d+),\s*(\d+),\s*(\d+)/.exec(color);
  if (!match) return null; // the tokens are not there (yet)
  return `#${match.slice(1, 4).map((v) => Number(v).toString(16).padStart(2, "0")).join("")}`;
}

/** Put the mode on the page: auto leaves it to prefers-color-scheme. */
export function applyTheme(mode: ThemeMode, root = document.documentElement): void {
  if (mode === "auto") delete root.dataset.theme;
  else root.dataset.theme = mode;

  // theme-color: both tags follow a forced theme; auto goes back to the
  // media queries they were written with.
  for (const meta of document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]')) {
    const scheme: Scheme = meta.getAttribute("media")?.includes("dark") ? "dark" : "light";
    meta.content = surfaceColor(mode === "auto" ? scheme : mode) ?? meta.content;
  }
}

const mode = ref<ThemeMode>(load());
watch(mode, (value) => {
  applyTheme(value);
  save(value);
});
applyTheme(mode.value);

export function useTheme() {
  return {
    mode,
    setMode: (value: ThemeMode) => (mode.value = value),
    cycle: () => {
      const next = THEME_MODES.indexOf(mode.value) + 1;
      mode.value = THEME_MODES[next % THEME_MODES.length] ?? "auto";
    },
  };
}
