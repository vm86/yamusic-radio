import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import type { MoodsInfo } from "../api";
import MoodCard from "./MoodCard.vue";

const moods: MoodsInfo = {
  available: ["sad", "calm", "fun", "active"],
  selected: [],
  station: "",
};

function card(props: Record<string, unknown> = {}) {
  return mount(MoodCard, {
    props: { moods, selected: [], pending: false, disabled: false, ...props },
  });
}

const pressed = (wrapper: ReturnType<typeof card>) =>
  wrapper.findAll("button").map((b) => b.attributes("aria-pressed"));

describe("MoodCard", () => {
  it("shows the picks it is given, not only what the radio has", () => {
    // The radio has none; two are picked and waiting to be sent.
    const wrapper = card({ selected: ["calm", "active"], pending: true });
    expect(pressed(wrapper)).toEqual(["false", "false", "true", "false", "true"]);
  });

  it("says that it waits for another pick, and then goes back to the usual hint", async () => {
    const wrapper = card({ pending: true });
    expect(wrapper.get(".hint").text()).toContain("подождём");
    expect(wrapper.get(".hint").classes()).toContain("pending");

    await wrapper.setProps({ pending: false });

    expect(wrapper.get(".hint").text()).toContain("только между выбранными");
    expect(wrapper.get(".hint").classes()).not.toContain("pending");
  });

  it("passes a pick on at once", async () => {
    const wrapper = card();
    await wrapper.findAll("button")[2]!.trigger("click");
    expect(wrapper.emitted("change")).toEqual([[["calm"]]]);
  });
});
