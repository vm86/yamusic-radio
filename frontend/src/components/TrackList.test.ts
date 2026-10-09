import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import type { TrackInfo } from "../api";
import TrackList from "./TrackList.vue";

function track(id: string, decided: boolean, cover = ""): TrackInfo {
  return {
    track_id: id,
    title: `Track ${id}`,
    artists: ["Artist"],
    bpm: 120,
    loudness_db: -9,
    duration_seconds: 200,
    mood: "calm",
    camelot: "8A",
    key: "A minor",
    decided,
    cover,
  };
}

const subs = (wrapper: ReturnType<typeof mount>) =>
  wrapper.findAll(".s").map((s) => s.text().replace(/\s+/g, " "));

describe("TrackList", () => {
  it("tells a screen reader which tracks are only a forecast", () => {
    const wrapper = mount(TrackList, {
      props: { tracks: [track("a", true), track("b", false)], empty: "-", kind: "queue" },
    });
    expect(subs(wrapper)).toEqual([
      "+1 · Artist · Спокойное",
      "+2, прогноз · Artist · Спокойное",
    ]);
    expect(wrapper.findAll(".item.ready")).toHaveLength(1);
    expect(wrapper.findAll(".item.forecast")).toHaveLength(1);
  });

  it("marks the played ones and counts them back from the last", () => {
    const wrapper = mount(TrackList, {
      props: { tracks: [track("a", true), track("b", true)], empty: "-", kind: "past" },
    });
    expect(subs(wrapper)).toEqual([
      "−2, уже играл · Artist · Спокойное",
      "−1, уже играл · Artist · Спокойное",
    ]);
  });

  it("shows the empty text", () => {
    const wrapper = mount(TrackList, { props: { tracks: [], empty: "Пока ничего", kind: "past" } });
    expect(wrapper.text()).toBe("Пока ничего");
  });

  it("shows the cover in a size for a list, or a disc without one", () => {
    const cover = "https://avatars.yandex.net/x/%%";
    const wrapper = mount(TrackList, {
      props: { tracks: [track("a", true, cover), track("b", true)], empty: "-", kind: "queue" },
    });
    const images = wrapper.findAll("img");
    expect(images).toHaveLength(1);
    expect(images[0]!.attributes("src")).toBe("https://avatars.yandex.net/x/100x100");
    expect(images[0]!.attributes("referrerpolicy")).toBe("no-referrer");
    expect(wrapper.findAll("use")).toHaveLength(1); // the disc of the track without one
  });
});
