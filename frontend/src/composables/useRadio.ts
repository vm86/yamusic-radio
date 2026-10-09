import { computed, onMounted, onUnmounted, ref } from "vue";
import {
  api,
  type AuthInfo,
  type GenresInfo,
  type MoodsInfo,
  type Queue,
  type Stats,
} from "../api";
import { MOODS, POLL_MS } from "../constants";
import { toast } from "./useToast";

/** What the radio says, polled, and the things that can be changed in it. */
export function useRadio() {
  const auth = ref<AuthInfo | null>(null);
  const queue = ref<Queue | null>(null);
  const stats = ref<Stats | null>(null);
  const genres = ref<GenresInfo | null>(null);
  const moods = ref<MoodsInfo | null>(null);
  const busy = ref(false);
  const refreshing = ref(false);

  // For the progress: when the queue came, to count the seconds since.
  const fetchedAt = ref(0);
  const tick = ref(performance.now());

  let failures = 0;
  let poll: ReturnType<typeof setInterval> | undefined;
  let clock: ReturnType<typeof setInterval> | undefined;

  function takeQueue(value: Queue): void {
    queue.value = value;
    fetchedAt.value = performance.now();
  }

  async function refresh(): Promise<void> {
    try {
      const [a, q, s, g] = await Promise.all([
        api.auth(),
        api.queue(),
        api.stats(),
        api.genres(),
      ]);
      failures = 0;
      auth.value = a;
      takeQueue(q);
      stats.value = s;
      genres.value = g;
      // Don't undo a choice that is on its way to the radio.
      if (!busy.value) moods.value = q.moods;
    } catch {
      if (++failures === 2) toast("Нет связи с радио");
    }
  }

  async function setMoods(list: string[]): Promise<void> {
    if (busy.value) return;
    busy.value = true;
    try {
      moods.value = await api.setMoods(list);
      toast(
        list.length
          ? `Играем: ${list.map((m) => MOODS[m] ?? m).join(", ")}`
          : "Любое настроение",
      );
    } catch (error) {
      toast(`Не удалось сменить настроение: ${(error as Error).message}`);
      void refresh();
    } finally {
      busy.value = false;
    }
  }

  async function setGenres(list: string[]): Promise<void> {
    try {
      genres.value = await api.setGenres(list);
    } catch (error) {
      toast(`Не удалось выбрать жанры: ${(error as Error).message}`);
    }
  }

  async function refreshQueue(): Promise<void> {
    refreshing.value = true;
    try {
      takeQueue(await api.refreshQueue());
      genres.value = await api.genres();
      toast("Очередь пересобрана из загруженных треков");
    } catch (error) {
      toast(`Не удалось обновить очередь: ${(error as Error).message}`);
    } finally {
      refreshing.value = false;
    }
  }

  const now = computed(() => queue.value?.now ?? null);
  // Seconds of the current track, ticking between the polls.
  const elapsed = computed(() => {
    const track = now.value;
    if (!track) return 0;
    const passed = track.elapsed_seconds + (tick.value - fetchedAt.value) / 1000;
    return Math.min(passed, track.duration_seconds);
  });

  function onVisible(): void {
    if (!document.hidden) void refresh();
  }

  onMounted(() => {
    void refresh();
    poll = setInterval(refresh, POLL_MS);
    clock = setInterval(() => (tick.value = performance.now()), 500);
    document.addEventListener("visibilitychange", onVisible);
  });
  onUnmounted(() => {
    clearInterval(poll);
    clearInterval(clock);
    document.removeEventListener("visibilitychange", onVisible);
  });

  return {
    auth,
    queue,
    stats,
    genres,
    moods,
    now,
    elapsed,
    busy,
    refreshing,
    setMoods,
    setGenres,
    refreshQueue,
  };
}
