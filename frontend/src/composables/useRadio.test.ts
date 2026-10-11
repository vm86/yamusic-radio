import { mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { defineComponent } from "vue";
import type { MoodsInfo, Queue } from "../api";
import { MOODS_DELAY_MS } from "../constants";

const moodsOf = (selected: string[]): MoodsInfo => ({
  available: ["sad", "calm", "fun", "active"],
  selected,
  station: "",
});
const queueOf = (moods: MoodsInfo): Queue => ({
  previous: [],
  now: null,
  next: [],
  moods,
  skip_pending: false,
  paused: false,
  pause_reason: null,
});

const api = vi.hoisted(() => ({
  auth: vi.fn(),
  queue: vi.fn(),
  stats: vi.fn(),
  genres: vi.fn(),
  setMoods: vi.fn(),
  setGenres: vi.fn(),
  refreshQueue: vi.fn(),
  nextTrack: vi.fn(),
}));
vi.mock("../api", () => ({ api }));

import { useRadio } from "./useRadio";
import { useToast } from "./useToast";

function setup() {
  let radio!: ReturnType<typeof useRadio>;
  const wrapper = mount(
    defineComponent({
      setup() {
        radio = useRadio();
        return () => null;
      },
    }),
  );
  return { radio, wrapper };
}

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

beforeEach(() => {
  vi.useFakeTimers();
  api.auth.mockResolvedValue({ authorized: true, url: null, code: null, fields: [] });
  api.queue.mockImplementation(async () => queueOf(moodsOf([])));
  api.stats.mockResolvedValue({ session: { total: 0 }, all_time: null });
  api.genres.mockResolvedValue({ available: [], selected: [], applied: [] });
  api.setMoods.mockImplementation(async (list: string[]) => moodsOf(list));
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("the time of the track", () => {
  const playing = (paused: boolean): Queue => ({
    ...queueOf(moodsOf([])),
    now: {
      title: "A",
      artists: [],
      duration_seconds: 100,
      elapsed_seconds: 10,
    } as unknown as Queue["now"],
    paused,
    pause_reason: paused ? "user" : null,
  });

  it("runs between the polls, and stands while the radio is paused", async () => {
    api.queue.mockResolvedValue(playing(false));
    const { radio, wrapper } = setup();
    await settle();
    await vi.advanceTimersByTimeAsync(2000);
    expect(radio.elapsed.value).toBeGreaterThan(11);

    api.queue.mockResolvedValue(playing(true));
    await vi.advanceTimersByTimeAsync(3000);
    const stood = radio.elapsed.value;
    expect(stood).toBe(10);
    expect(radio.pausedByUser.value).toBe(true);
    await vi.advanceTimersByTimeAsync(2000);
    expect(radio.elapsed.value).toBe(stood);
    wrapper.unmount();
  });
});

describe("picking moods", () => {
  it("shows a pick at once but tells the radio only after a pause", async () => {
    const { radio, wrapper } = setup();
    await settle();

    radio.setMoods(["calm"]);

    expect(radio.selectedMoods.value).toEqual(["calm"]); // the chip, at once
    expect(radio.pendingMoods.value).toEqual(["calm"]);
    await vi.advanceTimersByTimeAsync(MOODS_DELAY_MS - 1);
    expect(api.setMoods).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(1);

    expect(api.setMoods).toHaveBeenCalledExactlyOnceWith(["calm"]);
    expect(radio.pendingMoods.value).toBeNull();
    expect(radio.moods.value?.selected).toEqual(["calm"]);
    wrapper.unmount();
  });

  it("waits for the last pick: several picks make one change", async () => {
    const { radio, wrapper } = setup();
    await settle();

    radio.setMoods(["calm"]);
    await vi.advanceTimersByTimeAsync(MOODS_DELAY_MS - 500);
    radio.setMoods(["calm", "fun"]);
    await vi.advanceTimersByTimeAsync(MOODS_DELAY_MS - 500);
    radio.setMoods(["calm", "fun", "active"]);
    await vi.advanceTimersByTimeAsync(MOODS_DELAY_MS - 1);
    expect(api.setMoods).not.toHaveBeenCalled(); // each pick starts the wait again

    await vi.advanceTimersByTimeAsync(1);

    expect(api.setMoods).toHaveBeenCalledExactlyOnceWith(["calm", "fun", "active"]);
    wrapper.unmount();
  });

  it("does not let a poll undo a pick that is waiting", async () => {
    const { radio, wrapper } = setup();
    await settle();
    radio.setMoods(["fun"]);

    await vi.advanceTimersByTimeAsync(1000); // polls bring the radio's [] meanwhile

    expect(radio.selectedMoods.value).toEqual(["fun"]);
    wrapper.unmount();
  });

  it("sends nothing when the picks come back to what the radio has", async () => {
    const { radio, wrapper } = setup();
    await settle();

    radio.setMoods(["calm"]);
    radio.setMoods([]);
    await vi.advanceTimersByTimeAsync(MOODS_DELAY_MS);

    expect(api.setMoods).not.toHaveBeenCalled();
    expect(radio.pendingMoods.value).toBeNull();
    wrapper.unmount();
  });

  it("sends a pick made while the last one is on its way after it", async () => {
    let finish!: (value: MoodsInfo) => void;
    api.setMoods.mockImplementationOnce(
      () => new Promise<MoodsInfo>((resolve) => (finish = resolve)),
    );
    const { radio, wrapper } = setup();
    await settle();
    radio.setMoods(["calm"]);
    await vi.advanceTimersByTimeAsync(MOODS_DELAY_MS);
    expect(api.setMoods).toHaveBeenCalledTimes(1);
    expect(radio.busy.value).toBe(true);

    radio.setMoods(["calm", "fun"]);
    await vi.advanceTimersByTimeAsync(MOODS_DELAY_MS);
    expect(api.setMoods).toHaveBeenCalledTimes(1); // still waiting for the first

    finish(moodsOf(["calm"]));
    await vi.advanceTimersByTimeAsync(MOODS_DELAY_MS);

    expect(api.setMoods).toHaveBeenCalledTimes(2);
    expect(api.setMoods).toHaveBeenLastCalledWith(["calm", "fun"]);
    expect(radio.pendingMoods.value).toBeNull();
    wrapper.unmount();
  });

  it("tells about a failure and goes back to what the radio has", async () => {
    api.setMoods.mockRejectedValueOnce(new Error("not authorized yet"));
    const { radio, wrapper } = setup();
    await settle();

    radio.setMoods(["sad"]);
    await vi.advanceTimersByTimeAsync(MOODS_DELAY_MS);
    await settle();

    expect(useToast().message.value).toContain("Не удалось сменить настроение");
    expect(radio.pendingMoods.value).toBeNull();
    expect(radio.selectedMoods.value).toEqual([]);
    wrapper.unmount();
  });

  it("sends a waiting pick when the page is left", async () => {
    const { radio, wrapper } = setup();
    await settle();
    radio.setMoods(["active"]);

    Object.defineProperty(document, "hidden", { configurable: true, value: true });
    document.dispatchEvent(new Event("visibilitychange"));
    await settle();
    Object.defineProperty(document, "hidden", { configurable: true, value: false });

    expect(api.setMoods).toHaveBeenCalledExactlyOnceWith(["active"]);
    wrapper.unmount();
  });
});

describe("the next track button", () => {
  const track = (title: string) => ({
    track_id: title,
    title,
    artists: [],
    bpm: 120,
    loudness_db: -9,
    duration_seconds: 200,
    mood: "",
    camelot: "",
    key: "",
    decided: true,
    cover: "",
  });

  it("marks the request as waiting without touching the lists", async () => {
    const full: Queue = {
      ...queueOf(moodsOf([])),
      previous: [track("p1"), track("p2")],
      next: [track("n1"), track("n2"), track("n3")],
    };
    api.queue.mockResolvedValue(full);
    // What an older server answers: a part of the queue.
    api.nextTrack.mockResolvedValue({
      ...queueOf(moodsOf([])),
      next: [track("n1")],
      skip_pending: true,
    });
    const { radio, wrapper } = setup();
    await settle();
    expect(radio.queue.value?.next).toHaveLength(3);

    await radio.nextTrack();

    expect(radio.queue.value?.skip_pending).toBe(true);
    expect(radio.queue.value?.next.map((t) => t.title)).toEqual(["n1", "n2", "n3"]);
    expect(radio.queue.value?.previous).toHaveLength(2);
    wrapper.unmount();
  });
});
