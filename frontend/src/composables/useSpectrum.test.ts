import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { effectScope, nextTick, ref } from "vue";
import { BANDS, pickFrame, useSpectrum, type Frame } from "./useSpectrum";

const frame = (t: number, level: number): Frame => ({
  t,
  b: Array.from({ length: BANDS }, () => level),
});

describe("pickFrame", () => {
  it("takes the last frame that is not after the position, and drops the older ones", () => {
    const frames = [frame(1, 0.1), frame(2, 0.2), frame(3, 0.3)];

    expect(pickFrame(frames, 2.5)?.[0]).toBe(0.2);
    expect(frames.map((f) => f.t)).toEqual([2, 3]);
    expect(pickFrame(frames, 3)?.[0]).toBe(0.3);
    expect(frames.map((f) => f.t)).toEqual([3]);
  });

  it("has nothing for a position the frames have not got to", () => {
    const frames = [frame(5, 0.5)];

    expect(pickFrame(frames, 4.9)).toBeNull();
    expect(pickFrame([], 1)).toBeNull();
    expect(frames).toHaveLength(1);
  });
});

class FakeSource {
  static last: FakeSource;
  static opened = 0;
  closed = false;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  private listeners = new Map<string, (event: MessageEvent<string>) => void>();
  constructor(public url: string) {
    FakeSource.last = this;
    FakeSource.opened++;
  }
  addEventListener(name: string, listener: (event: MessageEvent<string>) => void): void {
    this.listeners.set(name, listener);
  }
  close(): void {
    this.closed = true;
  }
  say(data: unknown, name?: string): void {
    const event = { data: JSON.stringify(data) } as MessageEvent<string>;
    if (name) this.listeners.get(name)?.(event);
    else this.onmessage?.(event);
  }
}

describe("useSpectrum", () => {
  let paint: (() => void) | undefined;

  beforeEach(() => {
    FakeSource.opened = 0;
    vi.stubGlobal("EventSource", FakeSource);
    vi.stubGlobal("requestAnimationFrame", (callback: () => void) => {
      paint = callback;
      return 1;
    });
    vi.stubGlobal("cancelAnimationFrame", () => {
      paint = undefined;
    });
  });
  afterEach(() => vi.unstubAllGlobals());

  function setup() {
    const playing = ref(false);
    const token = ref("abc");
    const time = { now: 0 };
    const scope = effectScope();
    const spectrum = scope.run(() =>
      useSpectrum({ playing, token, currentTime: () => time.now }),
    )!;
    return { playing, token, time, scope, spectrum };
  }

  it("is quiet and connects to nothing until the player plays", () => {
    const { spectrum } = setup();

    expect(spectrum.bands.value).toEqual(Array(BANDS).fill(0));
    expect(FakeSource.opened).toBe(0);
  });

  it("shows the frame of the position the player is at, not the newest", async () => {
    const { playing, time, spectrum } = setup();
    playing.value = true;
    await nextTick();
    const source = FakeSource.last;
    expect(source.url).toBe("api/spectrum?l=abc");

    // The radio is 3 s ahead of the player, which starts at 7 s of the mix.
    source.say({ position: 7 }, "start");
    for (const [t, level] of [[7.0, 0.1], [7.5, 0.2], [8.0, 0.3], [10.0, 0.9]] as const) {
      source.say(frame(t, level));
    }

    time.now = 0.6; // the mix is at 7.6 for it
    paint?.();
    expect(spectrum.bands.value[0]).toBe(0.2);

    time.now = 1.0;
    paint?.();
    expect(spectrum.bands.value[0]).toBe(0.3);
  });

  it("shows nothing while it doesn't know where the stream starts", async () => {
    const { playing, spectrum } = setup();
    playing.value = true;
    await nextTick();
    const source = FakeSource.last;

    source.say({ position: null }, "start");
    source.say(frame(1, 0.5));
    paint?.();

    expect(spectrum.bands.value).toEqual(Array(BANDS).fill(0));
  });

  it("ignores what a connection made again says again", async () => {
    const { playing, time, spectrum } = setup();
    playing.value = true;
    await nextTick();
    const source = FakeSource.last;
    source.say({ position: 0 }, "start");
    source.say(frame(1, 0.4));
    source.say(frame(2, 0.6));
    source.say(frame(1, 0.4)); // the backlog again
    time.now = 1.5;

    paint?.();

    expect(spectrum.bands.value[0]).toBe(0.4);
  });

  it("closes and goes quiet when the player stops or connects anew", async () => {
    const { playing, token, time, scope, spectrum } = setup();
    playing.value = true;
    await nextTick();
    const first = FakeSource.last;
    first.say({ position: 0 }, "start");
    first.say(frame(1, 0.7));
    time.now = 2;
    paint?.();
    expect(spectrum.bands.value[0]).toBe(0.7);

    token.value = "def"; // another stream: the format was changed
    await nextTick();
    expect(first.closed).toBe(true);
    expect(FakeSource.last).not.toBe(first);
    expect(FakeSource.last.url).toBe("api/spectrum?l=def");
    expect(spectrum.bands.value).toEqual(Array(BANDS).fill(0));

    playing.value = false;
    await nextTick();
    expect(FakeSource.last.closed).toBe(true);
    scope.stop();
  });
});
