import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { nextTick, ref } from "vue";
import { useToast } from "./useToast";

// An <audio> that plays until it is told, like the live stream does.
class FakeAudio extends EventTarget {
  static last: FakeAudio;
  paused = true;
  volume = 1;
  preload = "";
  crossOrigin: string | null = null;
  currentTime = 0;
  private source: string | null = null;
  constructor() {
    super();
    FakeAudio.last = this;
  }
  set src(value: string) {
    this.source = value;
  }
  getAttribute(name: string): string | null {
    return name === "src" ? this.source : null;
  }
  removeAttribute(): void {
    this.source = null;
  }
  load(): void {}
  play(): Promise<void> {
    this.paused = false;
    this.dispatchEvent(new Event("playing"));
    return Promise.resolve();
  }
  pause(): void {
    if (this.paused) return;
    this.paused = true;
    this.dispatchEvent(new Event("pause"));
  }
}

import { usePlayer } from "./usePlayer";

beforeEach(() => vi.stubGlobal("Audio", FakeAudio));
afterEach(() => vi.unstubAllGlobals());

describe("the audio element", () => {
  it("asks for the stream with CORS, so that the sound can be analyzed", () => {
    usePlayer(ref(null));
    expect(FakeAudio.last.crossOrigin).toBe("anonymous");
  });
});

describe("who is listening", () => {
  it("names every connection to the stream, to say where it starts", async () => {
    const player = usePlayer(ref(null));
    expect(player.token.value).toBe("");

    await player.toggle();
    const first = player.token.value;
    expect(first).not.toBe("");
    expect(FakeAudio.last.getAttribute("src")).toContain(`?l=${first}&`);

    player.format.value = "flac";
    await nextTick();
    await Promise.resolve();
    expect(player.token.value).not.toBe(first);
    expect(FakeAudio.last.getAttribute("src")).toContain(`?l=${player.token.value}&`);

    FakeAudio.last.currentTime = 4.5;
    expect(player.currentTime()).toBe(4.5);
  });
});

describe("the stream format", () => {
  beforeEach(() => localStorage.removeItem("format"));

  it("listens to MP3 at first, and goes over to FLAC at once while playing", async () => {
    const player = usePlayer(ref(null));
    await player.toggle();
    expect(FakeAudio.last.getAttribute("src")).toMatch(/^stream\?/);

    player.format.value = "flac";
    await nextTick();
    await Promise.resolve();

    expect(FakeAudio.last.getAttribute("src")).toMatch(/^stream\.flac\?/);
    expect(player.playing.value).toBe(true);
    expect(localStorage.getItem("format")).toBe("flac");
  });

  it("remembers the format, and doesn't start playing by it", async () => {
    localStorage.setItem("format", "flac");
    const player = usePlayer(ref(null));
    expect(player.format.value).toBe("flac");
    expect(player.playing.value).toBe(false);
    await player.toggle();
    expect(FakeAudio.last.getAttribute("src")).toMatch(/^stream\.flac\?/);
  });
});

describe("the stream going away", () => {
  it("pauses the player when the stream fails", async () => {
    const player = usePlayer(ref(null));
    await player.toggle();
    expect(player.playing.value).toBe(true);

    FakeAudio.last.dispatchEvent(new Event("error"));

    expect(player.playing.value).toBe(false);
    expect(FakeAudio.last.paused).toBe(true);
    expect(useToast().message.value).toBe("Поток недоступен");
  });

  it("pauses the player when the stream ends", async () => {
    const player = usePlayer(ref(null));
    await player.toggle();

    FakeAudio.last.dispatchEvent(new Event("ended"));

    expect(player.playing.value).toBe(false);
    // and the next press listens again
    await player.toggle();
    expect(player.playing.value).toBe(true);
  });
});
