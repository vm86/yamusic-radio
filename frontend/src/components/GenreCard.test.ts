import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import type { GenresInfo } from "../api";
import GenreCard from "./GenreCard.vue";

const genres: GenresInfo = {
  available: ["house", "rap"],
  selected: [],
  applied: [],
  blocked: [],
  blocked_applied: [],
};

describe("GenreCard", () => {
  it("emits the picked genres and the blocked ones apart", async () => {
    const wrapper = mount(GenreCard, { props: { genres } });
    await wrapper.findAll(".chip")[1]?.trigger("click");
    await wrapper.findAll(".tab")[1]?.trigger("click");
    await wrapper.findAll(".chip")[2]?.trigger("click");
    expect(wrapper.emitted("change")).toEqual([[["house"]]]);
    expect(wrapper.emitted("block")).toEqual([[["rap"]]]);
  });

  it("shows one group at a time and counts what is picked", async () => {
    const wrapper = mount(GenreCard, { props: { genres: { ...genres, blocked: ["rap"] } } });
    expect(wrapper.findAll(".chips")).toHaveLength(1);
    const tabs = wrapper.findAll(".tab");
    expect(tabs[0]?.attributes("aria-selected")).toBe("true");
    expect(tabs[1]?.find(".count").text()).toBe("1");
    await tabs[1]?.trigger("click");
    expect(wrapper.find(".chips").attributes("aria-label")).toBe("Не играть");
    expect(wrapper.findAll(".chip")[2]?.attributes("aria-pressed")).toBe("true");
  });

  it("waits for the refresh when only the blocked genres changed", () => {
    const wrapper = mount(GenreCard, { props: { genres: { ...genres, blocked: ["rap"] } } });
    expect(wrapper.find(".hint").classes()).toContain("pending");
  });
});
