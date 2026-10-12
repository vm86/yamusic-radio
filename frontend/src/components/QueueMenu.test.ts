import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import QueueMenu from "./QueueMenu.vue";

const menu = () =>
  mount(QueueMenu, {
    props: { title: "Song", canPlay: true, canUp: true, canDown: true, canRemove: true },
    attachTo: document.body,
  });

describe("QueueMenu", () => {
  it("is shut until its button is pressed, and says what it is for", async () => {
    const wrapper = menu();
    const button = wrapper.find("button");
    expect(wrapper.find("[role=menu]").exists()).toBe(false);
    expect(button.attributes("aria-label")).toBe("Действия с треком Song");
    expect(button.attributes("aria-expanded")).toBe("false");

    await button.trigger("click");

    expect(wrapper.findAll("[role=menuitem]")).toHaveLength(4);
    expect(button.attributes("aria-expanded")).toBe("true");
    wrapper.unmount();
  });

  it("shuts when something else is pressed, or on Escape, and when it chooses", async () => {
    const wrapper = menu();
    await wrapper.find("button").trigger("click");
    document.body.click();
    await wrapper.vm.$nextTick();
    expect(wrapper.find("[role=menu]").exists()).toBe(false);

    await wrapper.find("button").trigger("click");
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    await wrapper.vm.$nextTick();
    expect(wrapper.find("[role=menu]").exists()).toBe(false);

    await wrapper.find("button").trigger("click");
    await wrapper.findAll("[role=menuitem]")[0]!.trigger("click");
    expect(wrapper.emitted("play")).toHaveLength(1);
    expect(wrapper.find("[role=menu]").exists()).toBe(false);
    wrapper.unmount();
  });
});
