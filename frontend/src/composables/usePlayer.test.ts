import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ref } from "vue";
import { useToast } from "./useToast";

// An <audio> that plays until it is told, like the live stream does.
class FakeAudio extends EventTarget {
  static last: FakeAudio;
  paused = true;
  volume = 1;
  preload = "";
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
