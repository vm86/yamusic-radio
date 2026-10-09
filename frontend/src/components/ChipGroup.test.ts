import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import ChipGroup from "./ChipGroup.vue";

const props = {
  label: "Настроение",
  anyLabel: "Любое",
  options: ["calm", "fun"],
  names: { calm: "Спокойное", fun: "Весёлое" },
};

describe("ChipGroup", () => {
  it("presses 'any' when nothing is selected", () => {
    const wrapper = mount(ChipGroup, { props: { ...props, selected: [] } });
    const pressed = wrapper.findAll("button").map((b) => b.attributes("aria-pressed"));
    expect(pressed).toEqual(["true", "false", "false"]);
    expect(wrapper.text()).toContain("Спокойное");
  });

  it("adds and removes an option", async () => {
    const wrapper = mount(ChipGroup, { props: { ...props, selected: ["calm"] } });
    const buttons = wrapper.findAll("button");

    await buttons[2]!.trigger("click");
    await buttons[1]!.trigger("click");

    expect(wrapper.emitted("change")).toEqual([[["calm", "fun"]], [[]]]);
  });

  it("clears the selection with 'any'", async () => {
    const wrapper = mount(ChipGroup, { props: { ...props, selected: ["fun"] } });
    await wrapper.findAll("button")[0]!.trigger("click");
    expect(wrapper.emitted("change")).toEqual([[[]]]);
  });

  it("can be disabled", () => {
    const wrapper = mount(ChipGroup, { props: { ...props, selected: [], disabled: true } });
    expect(wrapper.findAll("button").every((b) => b.attributes("disabled") !== undefined)).toBe(true);
  });
});
