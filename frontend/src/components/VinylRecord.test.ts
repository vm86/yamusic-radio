import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import VinylRecord from "./VinylRecord.vue";

const cover = "https://avatars.yandex.net/x/%%";

describe("VinylRecord", () => {
  it("puts the cover on the label", () => {
    const wrapper = mount(VinylRecord, { props: { cover, spinning: false } });
    expect(wrapper.get("img").attributes("src")).toBe("https://avatars.yandex.net/x/200x200");
  });

  it("turns only while it is playing", async () => {
    const wrapper = mount(VinylRecord, { props: { cover, spinning: false } });
    expect(wrapper.get(".disc").classes()).not.toContain("spinning");

    await wrapper.setProps({ spinning: true });

    expect(wrapper.get(".disc").classes()).toContain("spinning");
  });

  it("is a plain label without a cover, and when the cover does not load", async () => {
    const without = mount(VinylRecord, { props: { cover: "", spinning: true } });
    expect(without.find("img").exists()).toBe(false);

    const wrapper = mount(VinylRecord, { props: { cover, spinning: true } });
    await wrapper.get("img").trigger("error");
    expect(wrapper.find("img").exists()).toBe(false);

    // The next track gets another try.
    await wrapper.setProps({ cover: "https://avatars.yandex.net/y/%%" });
    expect(wrapper.get("img").attributes("src")).toBe("https://avatars.yandex.net/y/200x200");
  });

  it("is decoration: the title already says what plays", () => {
    const wrapper = mount(VinylRecord, { props: { cover, spinning: true } });
    expect(wrapper.attributes("aria-hidden")).toBe("true");
  });
});
