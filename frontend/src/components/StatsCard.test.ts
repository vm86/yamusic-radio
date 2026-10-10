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
    // No word about them: they are not a row and not a hint.
    expect(wrapper.text()).not.toContain("до учёта эффектов");
    expect(wrapper.text()).not.toContain("90");
    expect(wrapper.text()).toContain("за всё время");
  });

  it("shows the blends with an unknown key as a bar of their own, not as text", () => {
    const stats: Stats = {
      session: data({ total: 151, by_quality: { good: 53, fair: 28, poor: 11, unknown: 6 } }),
      all_time: null,
    };
    const wrapper = mount(StatsCard, { props: { stats } });

    const quality = groups(wrapper)["Удачность по тональностям"];
    expect(quality).toHaveLength(4);
    expect(quality?.at(-1)).toBe("Тональность неизвестна, не учтены 6 · 4%");
    // A bar, 4% wide, dimmer than the others.
    const bars = wrapper.findAll(".meter.muted > i");
    expect(bars).toHaveLength(1);
    expect(bars[0]!.attributes("style")).toContain("width: 3.97");
    // And no sentence about it any more.
    expect(wrapper.find(".hint.unknown").exists()).toBe(false);
    expect(wrapper.text()).not.toContain("Ещё");
  });

  it("leaves the shares of the known keys out of the bar of the unknown ones", () => {
    const stats: Stats = {
      session: data({ total: 100, by_quality: { good: 50, fair: 30, poor: 10, unknown: 10 } }),
      all_time: null,
    };
    const quality = groups(mount(StatsCard, { props: { stats } }))["Удачность по тональностям"];
    // 50 of the 90 that have a key, and 10 of all 100.
    expect(quality?.[0]).toBe("Удачные 50 · 56%");
    expect(quality?.[3]).toBe("Тональность неизвестна, не учтены 10 · 10%");
  });

  it("has no such bar when every key is known", () => {
    const known = data({ by_quality: { good: 5, fair: 3, poor: 2, unknown: 0 } });
    const wrapper = mount(StatsCard, { props: { stats: { session: known, all_time: null } } });
    expect(groups(wrapper)["Удачность по тональностям"]).toHaveLength(3);
    expect(wrapper.find(".meter.muted").exists()).toBe(false);
  });

  it("is hidden until there is a blend", () => {
    const empty = data({ total: 0, by_type: {}, by_effect: {} });
    const wrapper = mount(StatsCard, { props: { stats: { session: empty, all_time: null } } });
    expect(wrapper.find("section").exists()).toBe(false);
  });
});
