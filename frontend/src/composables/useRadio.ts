import { computed, onMounted, onUnmounted, ref } from "vue";
import {
  api,
  type AuthInfo,
  type GenresInfo,
  type MoodsInfo,
  type Queue,
  type Stats,
} from "../api";
import { MOODS_DELAY_MS, POLL_MS } from "../constants";
import { moodNames, t } from "../i18n";
import { toast } from "./useToast";

function sameSet(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((item) => b.includes(item));
}

/** What the radio says, polled, and the things that can be changed in it. */
export function useRadio() {
  const auth = ref<AuthInfo | null>(null);
  const queue = ref<Queue | null>(null);
  const stats = ref<Stats | null>(null);
  const genres = ref<GenresInfo | null>(null);
  const moods = ref<MoodsInfo | null>(null);
  const busy = ref(false);
  const refreshing = ref(false);
  // The moods picked and not yet sent: the radio waits a little in case
  // another one is picked. Until then the page shows them as chosen.
  const pendingMoods = ref<string[] | null>(null);
  let moodTimer: ReturnType<typeof setTimeout> | undefined;

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
      // Don't undo a choice that is waiting for, or on its way to, the radio.
      if (!busy.value && pendingMoods.value === null) moods.value = q.moods;
    } catch {
      if (++failures === 2) toast(t("toast.offline"));
    }
  }

  // Picking a mood changes what the chips show at once, and the radio is told
  // after MOODS_DELAY_MS without another pick.
  function setMoods(list: string[]): void {
    pendingMoods.value = list;
    clearTimeout(moodTimer);
    moodTimer = setTimeout(() => void applyMoods(), MOODS_DELAY_MS);
  }

  async function applyMoods(): Promise<void> {
    clearTimeout(moodTimer);
    const wanted = pendingMoods.value;
    if (wanted === null) return;
    if (busy.value) {
      // The last change is still on its way: send this one after it.
      moodTimer = setTimeout(() => void applyMoods(), MOODS_DELAY_MS);
      return;
    }
    if (sameSet(wanted, moods.value?.selected ?? [])) {
      pendingMoods.value = null; // picked and picked back: nothing to change
      return;
    }
    busy.value = true;
    try {
      moods.value = await api.setMoods(wanted);
      toast(
        wanted.length
          ? t("toast.playing", { moods: wanted.map((m) => moodNames()[m] ?? m).join(", ") })
          : t("toast.anyMood"),
      );
      // A pick made while this was on its way is still waiting.
      if (pendingMoods.value === wanted) pendingMoods.value = null;
    } catch (error) {
      toast(t("toast.moodFailed", { reason: (error as Error).message }));
      pendingMoods.value = null;
      void refresh();
    } finally {
      busy.value = false;
    }
  }

  // What the chips show: the pick that is waiting, else what the radio has.
  const selectedMoods = computed(
    () => pendingMoods.value ?? moods.value?.selected ?? [],
  );

  async function selectBackend(id: string): Promise<void> {
    try {
      auth.value = await api.selectBackend(id);
    } catch (error) {
      toast(t("toast.backendFailed", { reason: (error as Error).message }));
    }
  }

  async function signIn(values: Record<string, string>): Promise<void> {
    try {
      auth.value = await api.signIn(values);
    } catch (error) {
      toast(t("toast.signInFailed", { reason: (error as Error).message }));
    }
  }

  async function setGenres(list: string[]): Promise<void> {
    try {
      genres.value = await api.setGenres(list);
    } catch (error) {
      toast(t("toast.genresFailed", { reason: (error as Error).message }));
    }
  }

  async function setBlockedGenres(list: string[]): Promise<void> {
    try {
      genres.value = await api.setBlockedGenres(list);
    } catch (error) {
      toast(t("toast.blockedFailed", { reason: (error as Error).message }));
    }
  }

  // The blend starts on a good beat, not at once: the queue says when the
  // request has been taken (skip_pending goes back to false).
  async function nextTrack(): Promise<void> {
    try {
      // Only the news is taken from the answer: that the request waits for a
      // beat. The lists stay as they are, or they would flicker.
      const { skip_pending } = await api.nextTrack();
      if (queue.value) queue.value = { ...queue.value, skip_pending };
      toast(t("toast.skipAsked"));
    } catch (error) {
      const reason = (error as Error).message;
      toast(
        reason.includes("under way")
          ? t("toast.skipBusy")
          : reason.includes("nothing is playing")
            ? t("toast.skipIdle")
            : t("toast.skipFailed", { reason }),
      );
    }
  }

  async function refreshQueue(): Promise<void> {
    refreshing.value = true;
    try {
      takeQueue(await api.refreshQueue());
      genres.value = await api.genres();
      toast(t("toast.refreshed"));
    } catch (error) {
      toast(t("toast.refreshFailed", { reason: (error as Error).message }));
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
    // Leaving the page: don't lose a pick that is still waiting.
    else void applyMoods();
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
    clearTimeout(moodTimer);
    document.removeEventListener("visibilitychange", onVisible);
  });

  return {
    auth,
    queue,
    stats,
    genres,
    moods,
    selectedMoods,
    pendingMoods,
    now,
    elapsed,
    busy,
    refreshing,
    setMoods,
    selectBackend,
    signIn,
    setGenres,
    setBlockedGenres,
    refreshQueue,
    nextTrack,
  };
}
