import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import type { TrackInfo } from "../api";
import TrackList from "./TrackList.vue";

function track(id: string, decided: boolean): TrackInfo {
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
  };
}

describe("TrackList", () => {
  it("tells a screen reader which tracks are only a forecast", () => {
    const wrapper = mount(TrackList, {
      props: { tracks: [track("a", true), track("b", false)], empty: "-", kind: "queue" },
    });
    const badges = wrapper.findAll(".leading").map((b) => b.text().replace(/\s+/g, " "));
    expect(badges).toEqual(["+1", "+2 , прогноз"]);
    expect(wrapper.findAll(".item.forecast")).toHaveLength(1);
  });

  it("marks the played ones and counts them back from the last", () => {
    const wrapper = mount(TrackList, {
      props: { tracks: [track("a", true), track("b", true)], empty: "-", kind: "past" },
    });
    const badges = wrapper.findAll(".leading").map((b) => b.text().replace(/\s+/g, " "));
    expect(badges).toEqual(["−2 , уже играл", "−1 , уже играл"]);
  });

  it("shows the empty text", () => {
    const wrapper = mount(TrackList, { props: { tracks: [], empty: "Пока ничего", kind: "past" } });
    expect(wrapper.text()).toBe("Пока ничего");
  });
});
