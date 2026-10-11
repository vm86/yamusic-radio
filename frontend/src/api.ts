export interface TrackInfo {
  track_id: string;
  title: string;
  artists: string[];
  bpm: number;
  loudness_db: number;
  duration_seconds: number;
  mood: string;
  camelot: string;
  key: string;
  decided: boolean;
  /** The cover's URL with %% where the size goes, "" if there is none. */
  cover: string;
}

export interface NowTrack extends TrackInfo {
  elapsed_seconds: number;
}

export interface MoodsInfo {
  available: string[];
  selected: string[];
  station: string;
}

export interface GenresInfo {
  available: string[];
  selected: string[];
  applied: string[];
  blocked: string[];
  blocked_applied: string[];
}

export interface Queue {
  previous: TrackInfo[];
  now: NowTrack | null;
  next: TrackInfo[];
  moods: MoodsInfo;
  /** The next track was asked for and its blend has not begun. */
  skip_pending: boolean;
  /** The blend is under way: the next track can't be asked for again yet. */
  blending: boolean;
  /** A track plays, and the next one was not asked for, nor is being blended. */
  can_skip: boolean;
  /** The mix stands: paused through the API, or nobody listens. */
  paused: boolean;
  pause_reason: "user" | "no_listeners" | null;
}

export interface AuthField {
  name: string;
  label: string;
  secret: boolean;
}

/** One of the music services the radio can play from. */
export interface BackendInfo {
  id: string;
  name: string;
  /** The radio takes tracks from it (once it is signed in). */
  enabled: boolean;
  authorized: boolean;
  /** Signed in and not resting after failures. */
  available: boolean;
  /** Where to sign in, and the code to enter there, once the service gave them. */
  url: string | null;
  code: string | null;
  /** What to type to sign in, for a backend that asks for it. */
  fields: AuthField[];
}

export interface AuthInfo {
  /** At least one of the services that are on is signed in. */
  authorized: boolean;
  backends?: BackendInfo[];
  url: string | null;
  code: string | null;
  fields: AuthField[];
}

export interface TransitionData {
  total: number;
  beat_matched: number;
  by_type: Record<string, number>;
  /** none, drop, echo; "unknown" for blends made before they were counted. */
  by_effect: Record<string, number>;
  by_key: Record<string, number>;
  by_quality: { good: number; fair: number; poor: number; unknown: number };
}

export interface Stats {
  session: TransitionData;
  all_time: TransitionData | null;
}

// Relative URLs: the page works under any path the radio is served at.
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  if (!response.ok) {
    throw new Error((await response.text()) || response.statusText);
  }
  return (await response.json()) as T;
}

function put<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function post<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export const api = {
  auth: () => request<AuthInfo>("api/auth"),
  enableBackend: (backend: string) => put<AuthInfo>("api/auth/backend", { backend }),
  disableBackend: (backend: string) =>
    request<AuthInfo>(`api/auth/backend/${encodeURIComponent(backend)}`, { method: "DELETE" }),
  signIn: (backend: string, values: Record<string, string>) =>
    post<AuthInfo>("api/auth", { backend, values }),
  queue: () => request<Queue>("api/queue"),
  stats: () => request<Stats>("api/stats"),
  genres: () => request<GenresInfo>("api/genres"),
  moods: () => request<MoodsInfo>("api/moods"),
  refreshQueue: () => request<Queue>("api/queue/refresh", { method: "POST" }),
  nextTrack: () => request<Queue>("api/next", { method: "POST" }),
  setMoods: (moods: string[]) =>
    moods.length
      ? put<MoodsInfo>("api/moods", { moods })
      : request<MoodsInfo>("api/moods", { method: "DELETE" }),
  setGenres: (genres: string[]) =>
    genres.length
      ? put<GenresInfo>("api/genres", { genres })
      : request<GenresInfo>("api/genres", { method: "DELETE" }),
  setBlockedGenres: (genres: string[]) =>
    genres.length
      ? put<GenresInfo>("api/genres/blocked", { genres })
      : request<GenresInfo>("api/genres/blocked", { method: "DELETE" }),
};
