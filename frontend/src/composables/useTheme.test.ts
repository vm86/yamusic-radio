import { nextTick } from "vue";
import { beforeEach, describe, expect, it, vi } from "vitest";

// jsdom doesn't resolve light-dark(): answer the way a browser would for the
// probe element that reads the surface token.
const SURFACE = { light: "rgb(251, 248, 255)", dark: "rgb(18, 19, 24)" };
const realComputedStyle = window.getComputedStyle.bind(window);
vi.spyOn(window, "getComputedStyle").mockImplementation((element, pseudo) => {
  const scheme = (element as HTMLElement).style?.colorScheme;
  if (scheme === "light" || scheme === "dark") {
    return { backgroundColor: SURFACE[scheme] } as CSSStyleDeclaration;
  }
  return realComputedStyle(element, pseudo);
});

async function load() {
  vi.resetModules();
  return import("./useTheme");
}

function metas(): HTMLMetaElement[] {
  return [...document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]')];
}

beforeEach(() => {
  localStorage.clear();
  delete document.documentElement.dataset.theme;
  document.head.innerHTML = `
    <meta name="theme-color" content="#fbf8ff" media="(prefers-color-scheme: light)">
    <meta name="theme-color" content="#121318" media="(prefers-color-scheme: dark)">`;
});

describe("surfaceColor", () => {
  it("reads the surface of each scheme as a hex color", async () => {
    const { surfaceColor } = await load();
    expect(surfaceColor("light")).toBe("#fbf8ff");
    expect(surfaceColor("dark")).toBe("#121318");
  });

  it("is null while the tokens are not there, and leaves no probe behind", async () => {
    const { surfaceColor } = await load();
    vi.mocked(window.getComputedStyle).mockReturnValueOnce({ backgroundColor: "" } as CSSStyleDeclaration);
    const before = document.documentElement.children.length;
    expect(surfaceColor("dark")).toBeNull();
    expect(document.documentElement.children.length).toBe(before);
  });
});

describe("useTheme", () => {
  it("starts automatic: nothing forced on the page", async () => {
    const { useTheme } = await load();
    expect(useTheme().mode.value).toBe("auto");
    expect(document.documentElement.dataset.theme).toBeUndefined();
  });

  it("forces dark and light and remembers the choice", async () => {
    const { useTheme } = await load();
    const { mode, setMode } = useTheme();

    setMode("dark");
    await nextTick();
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem("theme")).toBe("dark");
    expect(metas().map((m) => m.content)).toEqual(["#121318", "#121318"]);

    setMode("light");
    await nextTick();
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(mode.value).toBe("light");
    expect(metas().map((m) => m.content)).toEqual(["#fbf8ff", "#fbf8ff"]);
  });

  it("goes back to the system theme on auto", async () => {
    const { useTheme } = await load();
    const { setMode } = useTheme();
    setMode("dark");
    await nextTick();

    setMode("auto");
    await nextTick();

    expect(document.documentElement.dataset.theme).toBeUndefined();
    expect(localStorage.getItem("theme")).toBeNull();
    expect(metas().map((m) => m.content)).toEqual(["#fbf8ff", "#121318"]);
  });

  it("restores the saved theme", async () => {
    localStorage.setItem("theme", "dark");
    const { useTheme } = await load();
    expect(useTheme().mode.value).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  it("ignores a saved value it does not know", async () => {
    localStorage.setItem("theme", "sepia");
    const { useTheme } = await load();
    expect(useTheme().mode.value).toBe("auto");
  });
});
