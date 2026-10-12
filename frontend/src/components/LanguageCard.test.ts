import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import type { LanguageInfo } from "../api";
import LanguageCard from "./LanguageCard.vue";

const language = (selected: string | null, station = ""): LanguageInfo => ({
  available: ["any", "russian", "not-russian", "without-words"],
  selected,
  station,
  steered: true,
});

const chips = (wrapper: ReturnType<typeof mount>) => wrapper.findAll(".chip");
const pressed = (wrapper: ReturnType<typeof mount>) =>
  chips(wrapper)
    .filter((chip) => chip.attributes("aria-pressed") === "true")
    .map((chip) => chip.text());

describe("LanguageCard", () => {
  it("offers any, and the languages by name", () => {
    const wrapper = mount(LanguageCard, { props: { language: language(null) } });

    expect(chips(wrapper).map((chip) => chip.text())).toEqual([
      "Любой",
      "Русский",
      "Иностранный",
      "Без слов",
    ]);
    expect(pressed(wrapper)).toEqual(["Любой"]);
  });

  it("shows what the station is set to, if nothing was picked here", () => {
    const wrapper = mount(LanguageCard, { props: { language: language(null, "not-russian") } });

    expect(pressed(wrapper)).toEqual(["Иностранный"]);
  });

  it("picks one language, and a new pick replaces the one before", async () => {
    const wrapper = mount(LanguageCard, { props: { language: language("russian") } });
    expect(pressed(wrapper)).toEqual(["Русский"]);

    await chips(wrapper)[3]?.trigger("click"); // without words
    await chips(wrapper)[1]?.trigger("click"); // the one that is on: off
    await chips(wrapper)[0]?.trigger("click"); // any

    expect(wrapper.emitted("change")).toEqual([["without-words"], ["any"], ["any"]]);
  });
});
