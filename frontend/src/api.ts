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
}

export interface Queue {
  previous: TrackInfo[];
  now: NowTrack | null;
  next: TrackInfo[];
  moods: MoodsInfo;
}

export interface AuthInfo {
  authorized: boolean;
  url: string | null;
  code: string | null;
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

export const api = {
  auth: () => request<AuthInfo>("api/auth"),
  queue: () => request<Queue>("api/queue"),
  stats: () => request<Stats>("api/stats"),
  genres: () => request<GenresInfo>("api/genres"),
  moods: () => request<MoodsInfo>("api/moods"),
  refreshQueue: () => request<Queue>("api/queue/refresh", { method: "POST" }),
  setMoods: (moods: string[]) =>
    moods.length
      ? put<MoodsInfo>("api/moods", { moods })
      : request<MoodsInfo>("api/moods", { method: "DELETE" }),
  setGenres: (genres: string[]) =>
    genres.length
      ? put<GenresInfo>("api/genres", { genres })
      : request<GenresInfo>("api/genres", { method: "DELETE" }),
};
