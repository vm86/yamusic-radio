import { mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it } from "vitest";
import ThemeButton from "./ThemeButton.vue";
import { useTheme } from "../composables/useTheme";

beforeEach(() => {
  localStorage.clear();
  useTheme().setMode("auto");
});

describe("ThemeButton", () => {
  it("names the theme in its title and accessible name", () => {
    const button = mount(ThemeButton).get("button");
    expect(button.attributes("title")).toBe("Тема: системная");
    expect(button.attributes("aria-label")).toBe("Тема: системная");
  });

  it("goes system -> light -> dark -> system on each press", async () => {
    const wrapper = mount(ThemeButton);
    const seen: string[] = [];
    for (let i = 0; i < 4; i++) {
      await wrapper.get("button").trigger("click");
      seen.push(`${useTheme().mode.value}: ${wrapper.get("button").attributes("title")}`);
    }
    expect(seen).toEqual([
      "light: Тема: светлая",
      "dark: Тема: тёмная",
      "auto: Тема: системная",
      "light: Тема: светлая",
    ]);
  });

  it("shows the icon of the mode", async () => {
    const wrapper = mount(ThemeButton);
    expect(wrapper.get("use").attributes("href")).toBe("#i-auto");
    await wrapper.get("button").trigger("click");
    expect(wrapper.get("use").attributes("href")).toBe("#i-light");
    await wrapper.get("button").trigger("click");
    expect(wrapper.get("use").attributes("href")).toBe("#i-dark");
  });
});
