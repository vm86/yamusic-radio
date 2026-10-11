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
    audio.src = `${STREAM_PATHS[format.value]}?${Date.now()}`;
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
    toggle,
    label: computed(() => (playing.value ? t("player.stop") : t("player.play"))),
  };
}
