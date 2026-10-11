import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import SpectrumBars from "./SpectrumBars.vue";

describe("SpectrumBars", () => {
  it("draws a bar to the height of every band, and never an empty one", () => {
    const wrapper = mount(SpectrumBars, { props: { bands: [0, 0.5, 1] } });

    const heights = wrapper.findAll("i").map((bar) => bar.attributes("style"));
    expect(heights).toEqual([
      "transform: scaleY(0.04);",
      "transform: scaleY(0.5);",
      "transform: scaleY(1);",
    ]);
    expect(wrapper.attributes("aria-hidden")).toBe("true");
  });
});
