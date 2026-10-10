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
    const groups = wrapper.findAll(".chips");
    await groups[0]?.findAll(".chip")[1]?.trigger("click");
    await groups[1]?.findAll(".chip")[2]?.trigger("click");
    expect(wrapper.emitted("change")).toEqual([[["house"]]]);
    expect(wrapper.emitted("block")).toEqual([[["rap"]]]);
  });

  it("waits for the refresh when only the blocked genres changed", () => {
    const wrapper = mount(GenreCard, { props: { genres: { ...genres, blocked: ["rap"] } } });
    expect(wrapper.find(".hint").classes()).toContain("pending");
  });
});
