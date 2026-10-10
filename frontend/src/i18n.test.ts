import { beforeEach, describe, expect, it } from "vitest";
import { nextTick } from "vue";
import { moodNames, systemLocale, t, useLocale } from "./i18n";
import { en } from "./locales/en";
import { ru } from "./locales/ru";

describe("i18n", () => {
  beforeEach(() => useLocale().setMode("ru"));

  it("takes the first language of the browser that is translated", () => {
    expect(systemLocale(["de-DE", "ru-RU", "en"])).toBe("ru");
    expect(systemLocale(["en-GB"])).toBe("en");
    expect(systemLocale(["fr", "de"])).toBe("en");
    expect(systemLocale([])).toBe("en");
  });

  it("has the same messages in every language", () => {
    expect(Object.keys(en).sort()).toEqual(Object.keys(ru).sort());
    for (const key of Object.keys(ru) as (keyof typeof ru)[]) {
      const params = (text: string) => (text.match(/\{\w+\}/g) ?? []).sort();
      expect(params(en[key]), key).toEqual(params(ru[key]));
    }
  });

  it("puts the params in", () => {
    expect(t("player.positionText", { elapsed: "1:00", total: "3:00" })).toBe("1:00 из 3:00");
    useLocale().setMode("en");
    expect(t("player.positionText", { elapsed: "1:00", total: "3:00" })).toBe("1:00 of 3:00");
  });

  it("switches the names and the page language, and cycles the modes", async () => {
    const locale = useLocale();
    expect(moodNames().calm).toBe("Спокойное");
    locale.setMode("auto");
    locale.cycle();
    expect(locale.mode.value).toBe("ru");
    locale.cycle();
    expect(locale.mode.value).toBe("en");
    await nextTick();
    expect(moodNames().calm).toBe("Calm");
    expect(document.documentElement.lang).toBe("en");
    expect(localStorage.getItem("locale")).toBe("en");
    locale.cycle();
    await nextTick();
    expect(locale.mode.value).toBe("auto");
    expect(localStorage.getItem("locale")).toBeNull();
  });
});
