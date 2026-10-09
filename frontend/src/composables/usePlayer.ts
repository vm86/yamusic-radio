import { computed, ref, watch, type Ref } from "vue";
import type { NowTrack } from "../api";
import { APP_TITLE } from "../constants";
import { toast } from "./useToast";

const VOLUME_KEY = "volume";

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
  const playing = ref(false);
  const volume = ref(loadVolume());

  const sync = () => (playing.value = !audio.paused);
  audio.addEventListener("playing", sync);
  audio.addEventListener("pause", sync);
  audio.addEventListener("error", () => {
    if (audio.getAttribute("src")) toast("Поток недоступен");
    sync();
  });

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

  async function toggle(): Promise<void> {
    if (!audio.paused) {
      audio.pause();
      audio.removeAttribute("src");
      audio.load(); // drop the buffer: the stream is live, resume at its edge
    } else {
      audio.src = `stream?${Date.now()}`;
      try {
        await audio.play();
      } catch (error) {
        toast(`Не удалось начать воспроизведение: ${(error as Error).message}`);
      }
    }
    sync();
  }

  watch(
    now,
    (track) => {
      document.title = track ? `${track.title} — ${APP_TITLE}` : APP_TITLE;
      if ("mediaSession" in navigator) {
        navigator.mediaSession.metadata = track
          ? new MediaMetadata({
              title: track.title,
              artist: track.artists.join(", "),
              album: APP_TITLE,
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
    toggle,
    label: computed(() => (playing.value ? "Остановить" : "Слушать")),
  };
}
