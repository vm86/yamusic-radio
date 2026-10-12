import { computed, ref, watch, type Ref } from "vue";
import type { NowTrack } from "../api";
import { t } from "../i18n";
import { toast } from "./useToast";

const VOLUME_KEY = "volume";
const FORMAT_KEY = "format";

/** The streams of the radio: MP3 at 320 kbit/s, and lossless FLAC. */
export type StreamFormat = "mp3" | "flac";
export const STREAM_PATHS: Record<StreamFormat, string> = {
  mp3: "stream",
  flac: "stream.flac",
};

/**
 * Is the browser's audio WebKit's: Safari, and every browser on iOS. It
 * doesn't play a live stream of bare FLAC, but plays FLAC in Ogg.
 */
export function isWebKitAudio(
  userAgent = navigator.userAgent,
  touchPoints = navigator.maxTouchPoints,
): boolean {
  if (/iP(hone|ad|od)/.test(userAgent)) return true;
  // iPadOS says it is a Mac, but has a touch screen.
  if (/Macintosh/.test(userAgent) && touchPoints > 1) return true;
  return /Safari/.test(userAgent) && !/Chrome|Chromium|Android|Edg|OPR|Firefox/.test(userAgent);
}

/** The address of the stream of a format in this browser. */
export function streamPath(format: StreamFormat): string {
  return format === "flac" && isWebKitAudio() ? "stream.ogg" : STREAM_PATHS[format];
}

/** A name for a listener: the radio is told it, to say where the page starts. */
function newToken(): string {
  try {
    return crypto.randomUUID();
  } catch {
    return `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
  }
}

function loadFormat(): StreamFormat {
  try {
    const saved = localStorage.getItem(FORMAT_KEY);
    if (saved === "mp3" || saved === "flac") return saved;
  } catch {
    /* private mode */
  }
  return "mp3";
}

function loadVolume(): number {
  try {
    const saved = Number(localStorage.getItem(VOLUME_KEY));
    if (localStorage.getItem(VOLUME_KEY) !== null && saved >= 0 && saved <= 100) {
      return saved;
    }
  } catch {
    /* private mode */
  }
  return 100;
}

/** The <audio> of the live stream, its volume and the lock screen card. */
export function usePlayer(now: Ref<NowTrack | null>) {
  const audio = new Audio();
  audio.preload = "none";
  // So that the sound can be analyzed (Web Audio): the stream sends CORS headers.
  audio.crossOrigin = "anonymous";
  const playing = ref(false);
  const volume = ref(loadVolume());
  const format = ref<StreamFormat>(loadFormat());
  // Who this connection to the stream is (see the spectrum).
  const token = ref("");

  // Where the player's clock stands when the stream is begun: a stream that
  // is joined in the middle may not start it at 0.
  let base = 0;
  audio.addEventListener("loadedmetadata", () => {
    base = audio.currentTime;
  });
  const sync = () => (playing.value = !audio.paused);
  audio.addEventListener("playing", sync);
  audio.addEventListener("pause", sync);
  // The stream is gone (the radio restarted, the network dropped): the
  // element doesn't pause by itself, so it is stopped to show that it is.
  function dropStream(): void {
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
    sync();
  }
  audio.addEventListener("error", () => {
    if (audio.getAttribute("src")) {
      toast(t("toast.streamDown"));
      dropStream();
    }
  });
  audio.addEventListener("ended", dropStream);

  watch(
    volume,
    (value) => {
      audio.volume = value / 100;
      try {
        localStorage.setItem(VOLUME_KEY, String(value));
      } catch {
        /* private mode */
      }
    },
    { immediate: true },
  );

  async function start(): Promise<void> {
    token.value = newToken();
    base = 0;
    audio.src = `${streamPath(format.value)}?l=${token.value}&${Date.now()}`;
    try {
      await audio.play();
    } catch (error) {
      toast(t("toast.playFailed", { reason: (error as Error).message }));
    }
  }

  async function toggle(): Promise<void> {
    if (!audio.paused) {
      audio.pause();
      audio.removeAttribute("src");
      audio.load(); // drop the buffer: the stream is live, resume at its edge
    } else {
      await start();
    }
    sync();
  }

  // Another stream while listening: connect to it at once.
  watch(format, async (value) => {
    try {
      localStorage.setItem(FORMAT_KEY, value);
    } catch {
      /* private mode */
    }
    if (!audio.paused) {
      await start();
      sync();
    }
  });

  watch(
    now,
    (track) => {
      document.title = track ? `${track.title} — ${t("app.title")}` : t("app.title");
      if ("mediaSession" in navigator) {
        navigator.mediaSession.metadata = track
          ? new MediaMetadata({
              title: track.title,
              artist: track.artists.join(", "),
              album: t("app.title"),
            })
          : null;
      }
    },
    { immediate: true },
  );

  watch(playing, (value) => document.body.classList.toggle("playing", value), {
    immediate: true,
  });

  return {
    playing,
    volume,
    format,
    token,
    /** Seconds of the stream that were played since it was connected. */
    currentTime: () => audio.currentTime - base,
    toggle,
    label: computed(() => (playing.value ? t("player.stop") : t("player.play"))),
  };
}
