import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import type { Stats, TransitionData } from "../api";
import StatsCard from "./StatsCard.vue";

function data(overrides: Partial<TransitionData> = {}): TransitionData {
  return {
    total: 10,
    beat_matched: 8,
    by_type: { "bass swap": 6, "eq blend": 2, crossfade: 1, "filter sweep": 1 },
    by_effect: { none: 4, drop: 3, echo: 1 },
    by_key: {},
    by_quality: { good: 5, fair: 3, poor: 1, unknown: 1 },
    ...overrides,
  };
}

const groups = (wrapper: ReturnType<typeof mount>) =>
  Object.fromEntries(
    wrapper.findAll("h3").map((h) => {
      const rows: string[] = [];
      for (let el = h.element.nextElementSibling; el && el.tagName !== "H3"; el = el.nextElementSibling) {
        if (el.classList.contains("stat")) {
          rows.push([...el.querySelectorAll("span")].map((span) => span.textContent?.trim()).join(" "));
        }
      }
      return [h.text(), rows];
    }),
  );

describe("StatsCard", () => {
  it("counts the crossfades and the effects next to the other blends", () => {
    const stats: Stats = { session: data(), all_time: null };
    const wrapper = mount(StatsCard, { props: { stats } });

    const shown = groups(wrapper);
    expect(shown["Тип"]).toContain("Кроссфейд 1 · 10%");
    expect(shown["Тип"]).toContain("Трёхполосный EQ 2 · 20%");
    expect(shown["Эффекты"]).toEqual([
      "Без эффектов 4 · 50%",
      "Райзер и удар 3 · 38%",
      "Эхо-аут 1 · 13%",
    ]);
    expect(wrapper.text()).toContain("с запуска");
  });

  it("does not make a row of the blends from before the effects were counted", () => {
    const stats: Stats = {
      session: data(),
      all_time: data({ total: 100, by_effect: { unknown: 90, none: 6, drop: 4 } }),
    };
    const wrapper = mount(StatsCard, { props: { stats } });

    expect(groups(wrapper)["Эффекты"]).toEqual(["Без эффектов 6 · 60%", "Райзер и удар 4 · 40%"]);
    expect(wrapper.text()).toContain("Ещё 90 переходов — до учёта эффектов");
    expect(wrapper.text()).toContain("за всё время");
  });

  it("is hidden until there is a blend", () => {
    const empty = data({ total: 0, by_type: {}, by_effect: {} });
    const wrapper = mount(StatsCard, { props: { stats: { session: empty, all_time: null } } });
    expect(wrapper.find("section").exists()).toBe(false);
  });
});
