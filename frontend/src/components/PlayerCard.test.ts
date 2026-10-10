import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import type { NowTrack } from "../api";
import PlayerCard from "./PlayerCard.vue";

const now: NowTrack = {
  track_id: "a",
  title: "Song",
  artists: ["Artist"],
  bpm: 120,
  loudness_db: -9,
  duration_seconds: 200,
  mood: "calm",
  camelot: "8A",
  key: "A minor",
  decided: true,
  cover: "",
  elapsed_seconds: 10,
};

function card(props: Record<string, unknown> = {}) {
  return mount(PlayerCard, {
    props: {
      now,
      elapsed: 10,
      playing: true,
      label: "Остановить",
      canSkip: true,
      skipPending: false,
      volume: 80,
      ...props,
    },
  });
}

const skipButton = (wrapper: ReturnType<typeof card>) =>
  wrapper.findAll("button").find((b) => b.attributes("aria-label")?.includes("трек") || b.attributes("aria-label")?.includes("бит"))!;

describe("PlayerCard next track button", () => {
  it("asks for the next track", async () => {
    const wrapper = card();
    const button = skipButton(wrapper);
    expect(button.attributes("aria-label")).toBe("Следующий трек");
    expect(button.attributes("disabled")).toBeUndefined();

    await button.trigger("click");

    expect(wrapper.emitted("skip")).toHaveLength(1);
  });

  it("waits while the request is pending, and says so", () => {
    const button = skipButton(card({ skipPending: true }));
    expect(button.attributes("disabled")).toBeDefined();
    expect(button.attributes("aria-busy")).toBe("true");
    expect(button.attributes("title")).toBe("Ждём подходящий бит…");
  });

  it("shows that it waits: a ring round the button and a chip, and nothing otherwise", async () => {
    const waiting = card({ skipPending: true });
    expect(waiting.find("button.skip .ring").exists()).toBe(true);
    expect(waiting.find("button.skip").classes()).toContain("busy");
    expect(waiting.get(".waiting-chip").text()).toBe("Ждём подходящий бит…");
    // The chip is in the part that is read out loud.
    expect(waiting.get(".meta").find(".waiting-chip").exists()).toBe(true);

    const idle = card();
    expect(idle.find(".ring").exists()).toBe(false);
    expect(idle.find(".waiting-chip").exists()).toBe(false);
    expect(idle.find("button.skip").classes()).not.toContain("busy");

    await waiting.setProps({ skipPending: false });
    expect(waiting.find(".ring").exists()).toBe(false);
    expect(waiting.find(".waiting-chip").exists()).toBe(false);
  });

  it("is off when nothing plays or the app is not authorized", () => {
    expect(skipButton(card({ canSkip: false })).attributes("disabled")).toBeDefined();
  });
});
