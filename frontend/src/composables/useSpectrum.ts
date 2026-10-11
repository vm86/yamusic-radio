import { onScopeDispose, ref, watch, type Ref } from "vue";

/** One value of the spectrum: its position in the mix, and its bands (0..1). */
export interface Frame {
  t: number;
  b: number[];
}

export const BANDS = 12;
const EMPTY: number[] = Array.from({ length: BANDS }, () => 0);
// What is kept of what the radio sends ahead: ~30 values a second.
const MAX_FRAMES = 600;

/**
 * The bands to show at a position of the mix: those of the last frame that
 * is not after it. The frames before that one are dropped.
 */
export function pickFrame(frames: Frame[], target: number): number[] | null {
  let last = -1;
  while (last + 1 < frames.length && (frames[last + 1] as Frame).t <= target) last++;
  if (last < 0) return null;
  frames.splice(0, last);
  return (frames[0] as Frame).b;
}

/**
 * The spectrum of the radio's mix, in time with what the player plays: the
 * radio tells where in the mix the stream of this page starts, and the
 * frames come with their positions in it. The player is some seconds behind
 * the mix, so what comes is kept until the player gets to it.
 */
export function useSpectrum(player: {
  playing: Ref<boolean>;
  token: Ref<string>;
  currentTime: () => number;
}) {
  const bands = ref<number[]>(EMPTY);
  let source: EventSource | null = null;
  let frames: Frame[] = [];
  let lastT = -Infinity;
  let start: number | null = null;
  let timer = 0;

  function close(): void {
    source?.close();
    source = null;
    cancelAnimationFrame(timer);
    frames = [];
    lastT = -Infinity;
    start = null;
    bands.value = EMPTY;
  }

  function show(): void {
    if (start !== null) {
      const picked = pickFrame(frames, start + player.currentTime());
      if (picked && picked !== bands.value) bands.value = picked;
    }
    timer = requestAnimationFrame(show);
  }

  function open(token: string): void {
    if (typeof EventSource === "undefined") return;
    source = new EventSource(`api/spectrum?l=${encodeURIComponent(token)}`);
    source.addEventListener("start", (event) => {
      start = (JSON.parse((event as MessageEvent<string>).data) as { position: number | null }).position;
    });
    source.onmessage = (event: MessageEvent<string>) => {
      const frame = JSON.parse(event.data) as Frame;
      // A connection that was made again begins with what was already seen.
      if (frame.t <= lastT) return;
      lastT = frame.t;
      frames.push(frame);
      if (frames.length > MAX_FRAMES) frames.splice(0, frames.length - MAX_FRAMES);
    };
    timer = requestAnimationFrame(show);
  }

  watch(
    () => [player.playing.value, player.token.value] as const,
    ([playing, token]) => {
      close();
      if (playing && token) open(token);
    },
    { immediate: true },
  );
  onScopeDispose(close);

  return { bands };
}
