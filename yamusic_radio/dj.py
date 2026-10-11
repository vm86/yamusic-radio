"""DJ radio: an endless beat-matched mix of "My Wave" over HTTP.

Before it starts streaming, the radio downloads a pool of tracks from the
station and analyzes their tempo. From then on it always picks the track
that fits the one playing best (tempo, loudness, key) and blends it in over
a whole number of beats, the tempo of both tracks gliding from that of the
one playing to that of the next. Meanwhile new tracks
keep arriving in the background. The result is encoded to MP3 and served as
an Icecast-style stream (with ICY titles) to any number of listeners.

Needs numpy and the ffmpeg binary.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import logging
import shutil
import tempfile
import time
from asyncio import (
    CancelledError,
    Event,
    Lock,
    Queue,
    QueueFull,
    Semaphore,
    Task,
    create_task,
    gather,
    sleep,
)
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass
from math import log10
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import numpy as np
from aiohttp import web

from yamusic_radio import dj_mix
from yamusic_radio.backend import AuthError, Feedback
from yamusic_radio.dj_mix import (
    CHANNELS,
    SAMPLE_RATE,
    Pcm,
    TrackAnalysis,
)
from yamusic_radio.multi import MultiBackend

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable

    import numpy.typing as npt

    from yamusic_radio.backend import MusicBackend, Track
    from yamusic_radio.cache import TrackCache

log = logging.getLogger(__name__)

DEFAULT_PORT = 8000
DEFAULT_POOL_SIZE = 12
EFFECT_LEVELS = {"off": 0.0, "light": 0.6, "full": 1.0}
DEFAULT_EFFECTS = "light"

# Tracks downloaded and analyzed at the same time.
PREPARE_CONCURRENCY = 3
# Give up filling the pool after this many batches without a usable track:
# the station only repeats itself.
MAX_EMPTY_BATCHES = 5
# Batches of the station with nothing of the wanted genres in a row.
MAX_OFF_GENRE_BATCHES = 20
# Pause before the mix starts again after a failure.
RESTART_SECONDS = 5
# When no track in the pool fits the one playing, load this many more...
EXTRA_TRACKS = 3
# ...up to this many, and take the best there is after that.
MAX_EXTRA_TRACKS = 6
# While the station gives nothing new, look again this often.
REPLENISH_RETRY_SECONDS = 120
# While the first tracks cannot be had, the radio asks again this often.
STARTUP_RETRY_SECONDS = 30
# Played tracks that can be repeated, oldest forgotten first.
MAX_PLAYED_REMEMBERED = 500
# The API lists at most this many tracks, before and after the current one.
MAX_LISTED = 15
# What is kept in the cache across restarts (see Radio.restore_state).
SETTING_MOODS = "moods"
SETTING_GENRES = "genres"
SETTING_BLOCKED = "blocked_genres"
SETTING_STATION = "station_settings"
# The genres of My Wave known so far: offered while the cache has none yet.
KNOWN_GENRES = (
    "dance", "pop", "house", "electronics", "rock", "country", "alternative",
    "hardrock", "numetal", "alternativemetal", "classicmetal", "industrial",
    "metal", "epicmetal", "lounge", "edmgenre", "gothicmetal", "indie",
    "punk", "relax", "rnb", "estrada", "metalcoregenre", "rap", "soundtrack",
    "trance", "tvseries", "disco", "dnb", "films", "folkmetal", "folkrock",
    "kpop", "newwave", "posthardcore", "ruspop", "rusrock", "soul",
    "stonerrock", "triphopgenre", "videogame",
)  # fmt: skip
# The blends kept to look at (see Radio.recent_blends).
MAX_RECENT_BLENDS = 10
# A beat grid of a blend needs at least this many points to give a period.
MIN_BEATS_IN_GRID = 2
# The page the radio shows at /.
WEB_DIR = Path(__file__).parent / "web"
WEB_PAGE = WEB_DIR / "index.html"
# Tracks played before a restart that the radio does not pick again.
CACHED_PLAYED_SKIPPED = 200
# The next track is chosen and decoded this long before the blend.
LOOKAHEAD_SECONDS = 40
# The mix is produced in pieces of this length.
CHUNK_SECONDS = 0.25
# How far ahead of the listeners the mix may run.
PACING_LEAD_SECONDS = 2.0

MP3_BITRATE = "192k"
# A new listener gets this much of the latest audio at once, so that the
# player has something to start on (browsers wait for ~1.5 s of audio before
# they play) instead of waiting for it to arrive at the pace it plays.
PREROLL_SECONDS = 3.0
MP3_BYTES_PER_SECOND = int(MP3_BITRATE.rstrip("k")) * 1000 // 8
ICY_METAINT = 16000
ICY_NAME = "Yandex Music DJ"
CLIENT_QUEUE_SIZE = 256


def is_available() -> bool:
    return shutil.which("ffmpeg") is not None


@dataclass(frozen=True, slots=True)
class BlendReport:
    """How one blend was made, to look at when it did not sound right."""

    from_title: str
    to_title: str
    kind: str
    effect: str
    beat_matched: bool
    from_bpm: float
    to_bpm: float
    # How far the tempo glided, as a fraction (0.05: 5% faster); 0 if not.
    glide: float
    seconds: float
    loudness_gap_db: float
    key_relation: str
    # How far above the ceiling the blend peaked before it was limited.
    over_db: float
    at: float

    def to_dict(self) -> dict[str, object]:
        return {
            "from": self.from_title,
            "to": self.to_title,
            "kind": self.kind,
            "effect": self.effect,
            "beat_matched": self.beat_matched,
            "from_bpm": self.from_bpm,
            "to_bpm": self.to_bpm,
            "glide": self.glide,
            "seconds": self.seconds,
            "loudness_gap_db": self.loudness_gap_db,
            "key_relation": self.key_relation,
            "over_db": self.over_db,
            "at": self.at,
        }


class SkipRefusedError(Exception):
    """The next track can't be asked for now."""


@dataclass(frozen=True, slots=True)
class TrackInfo:
    """A track as the API shows it."""

    track_id: str
    title: str
    artists: tuple[str, ...]
    bpm: float
    loudness_db: float
    duration_seconds: float
    mood: str
    # Camelot Wheel code and the name of the key; "" when it isn't known.
    camelot: str = ""
    key: str = ""
    # False for the tracks of a forecast: the radio may still change them.
    decided: bool = True
    # The cover's URL, "%%" for the size; "" when there is none.
    cover: str = ""

    @classmethod
    def of(
        cls,
        title: str,
        analysis: TrackAnalysis,
        track: Track | None,
        mood: str,
        *,
        decided: bool = True,
    ) -> TrackInfo:
        return cls(
            track.track_id if track is not None else "",
            title,
            track.artists if track is not None else (),
            round(analysis.bpm, 1),
            round(analysis.loudness_db, 1),
            round((analysis.end - analysis.start) / SAMPLE_RATE, 1),
            mood,
            analysis.camelot,
            analysis.key_name,
            decided,
            track.cover if track is not None else "",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "track_id": self.track_id,
            "title": self.title,
            "artists": list(self.artists),
            "bpm": self.bpm,
            "loudness_db": self.loudness_db,
            "duration_seconds": self.duration_seconds,
            "mood": self.mood,
            "camelot": self.camelot,
            "key": self.key,
            "decided": self.decided,
            "cover": self.cover,
        }


@dataclass(slots=True)
class Candidate:
    """A downloaded and analyzed track waiting for its turn."""

    title: str
    path: Path
    analysis: TrackAnalysis
    artists: frozenset[str] = frozenset()
    track: Track | None = None
    # The station mood the track was fetched with.
    mood: str = ""
    genre: str = ""


@dataclass(slots=True)
class Played:
    """What is kept of a track once it has been played, to repeat it."""

    track: Track
    title: str
    analysis: TrackAnalysis
    artists: frozenset[str]


@dataclass(slots=True)
class Deck:
    """A decoded track as it will be played."""

    title: str
    pcm: Pcm
    analysis: TrackAnalysis
    beat_matched: bool
    track: Track | None = None
    mood: str = ""
    # The ratio of the tempo of the track before it to its own tempo (a
    # beat-matched blend glides through it, see dj_mix.tempo_glide).
    speed: float = 1.0
    # The downloaded track it was made from, kept until the deck is heard
    # so that the deck can go back to the pool (see Radio.refresh_queue).
    source: Candidate | None = None
    # Radio.generation when the deck was chosen.
    generation: int = 0


async def decode_file(path: Path) -> Pcm:
    """Decode to float32 44.1 kHz stereo."""
    command = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path)]
    command += [
        "-f",
        "f32le",
        "-ac",
        str(CHANNELS),
        "-ar",
        str(SAMPLE_RATE),
        "pipe:1",
    ]
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    raw, _ = await proc.communicate()
    frame = 4 * CHANNELS
    usable = len(raw) // frame * frame
    if proc.returncode != 0 or not usable:
        raise RuntimeError(f"ffmpeg could not decode {path.name}")
    pcm: Pcm = np.frombuffer(raw[:usable], dtype=np.float32).reshape(
        -1, CHANNELS
    )
    return pcm


class Radio:
    """Keeps a pool of prepared tracks and hands out the best next one."""

    def __init__(
        self,
        backend: MusicBackend,
        workdir: Path,
        *,
        station_id: str = "",
        pool_size: int = DEFAULT_POOL_SIZE,
        mood_arc: bool = False,
        cache: TrackCache | None = None,
    ) -> None:
        log.info("Keys are found by %s", dj_mix.key_detector())
        self._backend = backend
        self._cache = cache
        self._workdir = workdir
        self._station = station_id
        self._pool_size = pool_size
        self._pool: list[Candidate] = []
        # Artists of the tracks played so far, and how many there were.
        self._history: deque[frozenset[str]] = deque(
            maxlen=dj_mix.REPEAT_WINDOW
        )
        self._position = 0
        # Tracks already played, the one played longest ago first. They
        # are never picked again until the station has nothing new.
        self._played: OrderedDict[str, Played] = OrderedDict()
        # With the mood arc, the station's mood setting follows the set.
        self._mood_arc = mood_arc
        # Tracks turned down for their mood, counted by the loading.
        self._off_mood = 0
        self._mood = ""
        self._saved_settings: tuple[str, str, str] | None = None
        self._playing_id = ""
        # What the API shows: the track heard now, the ones before it and
        # the one already decoded to come next.
        self._now: Deck | None = None
        self._now_since = 0.0
        # The next track was asked for (see request_skip); a transition is
        # under way (from its count to the end of the track that is left);
        # and how long the track left was heard, if it was skipped.
        self._skip = False
        self._blending = False
        self._heard_seconds: float | None = None
        self._next: Deck | None = None
        self._before: deque[TrackInfo] = deque(maxlen=MAX_LISTED)
        # Only these station moods are played; empty is any.
        self._allowed_moods: tuple[str, ...] = ()
        # Genres picked on the page, and the ones the queue is built from:
        # the first become the second only when the queue is rebuilt.
        self._wanted_genres: tuple[str, ...] = ()
        self._allowed_genres: tuple[str, ...] = ()
        # Genres that are never played: picked, and in force (the same way).
        self._wanted_blocked: tuple[str, ...] = ()
        self._allowed_blocked: tuple[str, ...] = ()
        # Tracks of other genres that the station offered: not asked again
        # for as long as the genres stay the same.
        self._off_genre: set[str] = set()
        # Tracks that were fetched or played; not picked again. What was
        # played before this run comes from the cache.
        self._seen: set[str] = set()
        if cache is not None:
            self._seen |= cache.recently_played(CACHED_PLAYED_SKIPPED)
        # Counts the rebuilds of the queue: a deck chosen before the last
        # one is stale.
        self._generation = 0
        # What the transitions of this run were like.
        self._transitions: Counter[str] = Counter()
        self._effects: Counter[str] = Counter()
        self._recent: deque[BlendReport] = deque(maxlen=MAX_RECENT_BLENDS)
        self._transition_keys: Counter[str] = Counter()
        self._matched_transitions = 0
        self._key_pairs: Counter[tuple[str, str]] = Counter()
        self._slots = Semaphore(PREPARE_CONCURRENCY)
        self._load_lock = Lock()
        self._feedback_tasks: set[Task[None]] = set()
        self._added = Event()
        self._consumed = Event()

    @property
    def pool(self) -> list[Candidate]:
        return self._pool

    @property
    def played(self) -> OrderedDict[str, Played]:
        return self._played

    def _remember(self, key: str, value: object) -> None:
        if self._cache is not None:
            self._cache.put_setting(key, value)

    def _forget(self, key: str) -> None:
        if self._cache is not None:
            self._cache.delete_setting(key)

    async def start_mood_arc(self) -> None:
        """Remember the station's settings, to put them back at the end.

        They are kept on disk as well, for as long as the account has
        something else: if the radio is killed, the next run puts them back.
        """
        if not self._backend.supports_moods:
            return
        self._saved_settings = await self._backend.station_settings(
            self.station_id
        )
        self._mood = self._saved_settings[0]
        self._remember(
            SETTING_STATION,
            {"station": self.station_id, "settings": self._saved_settings},
        )

    async def restore_settings(self) -> None:
        """Put the station's settings back as they were."""
        if self._saved_settings is None:
            return
        if self._mood != self._saved_settings[0]:
            await self._backend.set_station_settings(
                self.station_id, *self._saved_settings
            )
            self._mood = self._saved_settings[0]
        self._forget(SETTING_STATION)

    async def restore_state(self) -> None:
        """Take up where the last run left off.

        If it was killed with the station's settings changed, they are put
        back first; then the moods and the genres that were picked are
        picked again. Needs the app to be authorized.
        """
        if self._cache is None:
            return
        left = self._cache.get_setting(SETTING_STATION)
        if isinstance(left, dict):
            try:
                await self._backend.set_station_settings(
                    str(left["station"]), *left["settings"]
                )
            except (KeyError, TypeError, ValueError):
                log.warning("Ignoring the station settings left on disk")
                self._forget(SETTING_STATION)
            except Exception:
                # Left for the next run, which may be able to.
                log.exception("Could not put the station's settings back")
            else:
                log.info(
                    "The station's settings put back (the last run left them)"
                )
                self._forget(SETTING_STATION)
        genres = self._cache.get_setting(SETTING_GENRES)
        if isinstance(genres, list):
            # Nothing is queued yet, so they are in force at once.
            self._wanted_genres = tuple(g for g in genres if isinstance(g, str))
            self._allowed_genres = self._wanted_genres
        blocked = self._cache.get_setting(SETTING_BLOCKED)
        if isinstance(blocked, list):
            self._wanted_blocked = tuple(
                b for b in blocked if isinstance(b, str)
            )
            self._allowed_blocked = self._wanted_blocked
        moods = self._cache.get_setting(SETTING_MOODS)
        if isinstance(moods, list) and moods:
            await self.set_allowed_moods(m for m in moods if isinstance(m, str))

    @property
    def authorized(self) -> bool:
        return self._backend.authorized

    async def authorize(self, values: dict[str, str]) -> None:
        """Sign in to the backend with what the user typed."""
        await self._backend.authorize(values)

    @property
    def station_id(self) -> str:
        """The station asked for, else the one the backend starts with."""
        return self._station or self._backend.default_station

    def auth_info(self) -> dict[str, object]:
        """Return what to do to authorize the app, while it isn't."""
        authorized = self.authorized
        prompt = self._backend.auth_prompt
        return {
            "authorized": authorized,
            "url": None if authorized else prompt.url,
            "code": None if authorized else prompt.code,
            "fields": []
            if authorized
            else [dataclasses.asdict(f) for f in prompt.fields],
        }

    @property
    def allowed_moods(self) -> tuple[str, ...]:
        return self._allowed_moods

    @property
    def mood_arc_saved(self) -> tuple[str, str, str] | None:
        """The station's settings put aside, None before that."""
        return self._saved_settings

    @property
    def mood(self) -> str:
        """The mood setting the station has now."""
        return self._mood

    async def set_allowed_moods(self, moods: Iterable[str]) -> None:
        """Play only tracks of these moods, or any with none given.

        The station's mood setting is kept on the account: it is changed
        for as long as the radio runs and put back when it stops.
        """
        chosen = set(moods)
        wanted = tuple(m for m in dj_mix.ALL_MOODS if m in chosen)
        if wanted and self._saved_settings is None:
            await self.start_mood_arc()
        self._allowed_moods = wanted
        self._remember(SETTING_MOODS, list(wanted))
        if not wanted and not self._mood_arc:
            await self.restore_settings()
            self._saved_settings = None
        self._drop_unwanted()
        log.info("Moods played: %s", ", ".join(wanted) or "any")
        # Wake up the refill: the pool may be short of tracks now.
        self._consumed.set()

    @property
    def generation(self) -> int:
        return self._generation

    def refresh_queue(self) -> None:
        """Choose the next tracks anew among the ones already loaded.

        The track that was already chosen and decoded goes back to the
        pool, tracks of moods that are not wanted are dropped, and the
        next track is picked again, so the queue follows the moods
        selected now. Nothing is downloaded for it.
        """
        self._generation += 1
        if (self._allowed_genres, self._allowed_blocked) != (
            self._wanted_genres,
            self._wanted_blocked,
        ):
            self._allowed_genres = self._wanted_genres
            self._allowed_blocked = self._wanted_blocked
            self._off_genre.clear()
            log.info(
                "Genres played: %s; never played: %s",
                ", ".join(self._allowed_genres) or "any",
                ", ".join(self._allowed_blocked) or "none",
            )
        deck, self._next = self._next, None
        if deck is not None:
            self._unpick(deck)
        self._drop_unwanted()
        log.info("Queue rebuilt from %d loaded tracks", len(self._pool))
        # Wake up the refill: the pool may be short of tracks now.
        self._consumed.set()

    def is_stale(self, deck: Deck) -> bool:
        """Say whether the queue was rebuilt after the deck was chosen."""
        return deck.generation != self._generation

    def _unpick(self, deck: Deck) -> None:
        """Take back the choice of a deck that has not been heard yet."""
        source = deck.source
        if source is None:
            return
        if self._history and self._history[-1] == source.artists:
            self._history.pop()
        self._position = max(0, self._position - 1)
        track = source.track
        if track is not None:
            self._played.pop(track.track_id, None)
            now = self._now.track if self._now is not None else None
            self._playing_id = now.track_id if now is not None else ""
        if source.path.exists():
            self._pool.append(source)

    def _wanted(self, candidate: Candidate) -> bool:
        """Say whether the mood and the genre of a pool track are allowed."""
        return self._mood_allowed(candidate.mood) and self._genre_allowed(
            candidate.genre
        )

    def _mood_allowed(self, mood: str) -> bool:
        return not self._allowed_moods or mood in self._allowed_moods

    def _genre_allowed(self, genre: str) -> bool:
        if genre in self._allowed_blocked:
            return False
        return not self._allowed_genres or genre in self._allowed_genres

    def _drop_unwanted(self) -> None:
        """Forget the pool tracks of other moods or genres."""
        for candidate in list(self._pool):
            if not self._wanted(candidate):
                self._pool.remove(candidate)
                candidate.path.unlink(missing_ok=True)

    def set_wanted_genres(self, genres: Iterable[str]) -> None:
        """Pick the genres. The queue follows them once it is rebuilt."""
        self._wanted_genres = tuple(dict.fromkeys(genres))
        self._remember(SETTING_GENRES, list(self._wanted_genres))

    def set_blocked_genres(self, genres: Iterable[str]) -> None:
        """Pick the genres never to play, in force like the wanted ones."""
        self._wanted_blocked = tuple(dict.fromkeys(genres))
        self._remember(SETTING_BLOCKED, list(self._wanted_blocked))

    def genres_info(self) -> dict[str, object]:
        """Genres to pick from, the ones picked and the ones in force."""
        known = self._cache.known_genres() if self._cache is not None else []
        available = [name for name, _ in known] or list(KNOWN_GENRES)
        for genre in (
            *self._wanted_genres,
            *self._allowed_genres,
            *self._wanted_blocked,
            *self._allowed_blocked,
        ):
            if genre not in available:
                available.append(genre)
        return {
            "available": available,
            "selected": list(self._wanted_genres),
            "applied": list(self._allowed_genres),
            "blocked": list(self._wanted_blocked),
            "blocked_applied": list(self._allowed_blocked),
        }

    def queue(
        self, before: int = 2, after: int = MAX_LISTED
    ) -> dict[str, object]:
        """Describe what was played, what plays now and what comes next."""
        before = max(0, min(before, MAX_LISTED))
        after = max(0, min(after, MAX_LISTED))
        now = None
        if self._now is not None:
            now = TrackInfo.of(
                self._now.title,
                self._now.analysis,
                self._now.track,
                self._now.mood,
            )
        return {
            "previous": [
                t.to_dict() for t in list(self._before)[-before:] if before
            ],
            "now": (
                {
                    **now.to_dict(),
                    "elapsed_seconds": round(
                        time.monotonic() - self._now_since, 1
                    ),
                }
                if now is not None
                else None
            ),
            "next": [t.to_dict() for t in self.upcoming(after)],
            "moods": self.moods_info(),
            "skip_pending": self._skip,
        }

    @property
    def skip_requested(self) -> bool:
        return self._skip

    def request_skip(self) -> None:
        """Ask for the next track: the blend starts on the next good beat.

        It is not at once: the next track has to be ready, and the blend
        starts on a bar line of the track that plays, as smoothly as any.
        """
        if self._now is None:
            raise SkipRefusedError("nothing is playing yet")
        if self._blending:
            raise SkipRefusedError("a transition is already under way")
        self._skip = True

    def skip_taken(self) -> None:
        """Note that the track that plays is left early, at the user's wish."""
        self._skip = False
        self._heard_seconds = time.monotonic() - self._now_since

    def moods_info(self) -> dict[str, object]:
        return {
            "available": list(dj_mix.ALL_MOODS),
            "selected": list(self._allowed_moods),
            "station": self._mood,
            # Whether the backend steers its station by the mood; if not,
            # the radio judges the tracks by their sound.
            "steered": self._backend.supports_moods,
        }

    def upcoming(self, limit: int) -> list[TrackInfo]:
        """Return the track decoded to come next, then a forecast of the pool.

        The forecast chooses the way the radio does, one track after the
        other, but the radio decides only ~40 seconds before each blend,
        by when the pool has changed.
        """
        found: list[TrackInfo] = []
        last = self._next or self._now
        current = last.analysis if last is not None else None
        if self._next is not None and limit > 0:
            found.append(
                TrackInfo.of(
                    self._next.title,
                    self._next.analysis,
                    self._next.track,
                    self._next.mood,
                )
            )
        pool = [c for c in self._pool if self._wanted(c)]
        history = tuple(self._history)
        position = self._position
        while len(found) < limit and pool:
            entries = [
                dj_mix.Entry(c.analysis, c.artists, c.mood) for c in pool
            ]
            index = dj_mix.choose_next(current, entries, history, position)
            _, current = dj_mix.step_cost(
                current, entries[index], history, position
            )
            chosen = pool.pop(index)
            found.append(
                TrackInfo.of(
                    chosen.title,
                    current,
                    chosen.track,
                    chosen.mood,
                    decided=False,
                )
            )
            history = (*history, chosen.artists)[-dj_mix.REPEAT_WINDOW :]
            position += 1
        return found

    async def _set_mood(self, mood: str) -> None:
        if self._saved_settings is None or mood == self._mood:
            return
        _, diversity, language = self._saved_settings
        await self._backend.set_station_settings(
            self.station_id, mood, diversity, language
        )
        self._mood = mood

    async def _load(
        self, count: int, *, if_empty: bool = False, mood: str = ""
    ) -> int:
        """Download and analyze `count` tracks the radio hasn't had yet.

        Returns how many were added: 0 when the station only repeats
        tracks that were already seen. With `if_empty`, nothing is loaded
        if somebody else has filled the pool in the meantime.
        """
        async with self._load_lock:
            self._drop_unwanted()
            if if_empty and self._pool:
                return 1
            before = len(self._pool)
            empty_batches = 0
            steer = self._backend.supports_moods and (
                self._mood_arc or bool(self._allowed_moods)
            )
            if steer:
                # Tracks loaded now are played about when the pool has been
                # worked through: aim for the mood wanted then, as far as
                # the moods allowed go.
                mood = mood or dj_mix.mood_for(self._position + len(self._pool))
                mood = dj_mix.nearest_mood(mood, self._allowed_moods)
                await self._set_mood(mood)
            else:
                mood = ""
            off_genre_batches = 0
            off_mood_batches = 0
            while (wanted := count - (len(self._pool) - before)) > 0:
                batch = await self._backend.station_batch(
                    self.station_id,
                    exclude_track_ids=self._seen | self._off_genre,
                )
                fresh = [
                    t
                    for t in batch
                    if t.track_id not in self._seen
                    and t.track_id not in self._off_genre
                ]
                wanted_genre = [
                    t for t in fresh if self._genre_allowed(t.genre)
                ]
                self._off_genre.update(
                    t.track_id for t in fresh if t not in wanted_genre
                )
                if fresh and not wanted_genre:
                    off_genre_batches += 1
                    if off_genre_batches >= MAX_OFF_GENRE_BATCHES:
                        break
                    continue
                off_genre_batches = 0
                fresh = wanted_genre[:wanted]
                self._seen.update(t.track_id for t in fresh)
                added = len(self._pool)
                rejected = self._off_mood
                await gather(*(self.prepare(t) for t in fresh))
                if len(self._pool) == added and self._off_mood != rejected:
                    # Good tracks, but not of the moods wanted.
                    off_mood_batches += 1
                    if off_mood_batches >= MAX_OFF_GENRE_BATCHES:
                        break
                elif len(self._pool) == added:
                    empty_batches += 1
                    if empty_batches >= MAX_EMPTY_BATCHES:
                        break
                else:
                    empty_batches = 0
            return len(self._pool) - before

    async def fill(self, size: int | None = None) -> None:
        """Load tracks until the pool has `size` of them, if there are."""
        size = size or self._pool_size
        while len(self._pool) < size:
            wanted = size - len(self._pool)
            if self._backend.supports_moods and (
                self._mood_arc or self._allowed_moods
            ):
                # A few at a time: each round has its own mood.
                wanted = min(wanted, EXTRA_TRACKS)
            if not await self._load(wanted):
                log.warning("The station has no new tracks")
                return

    async def keep_filled(self) -> None:
        """Background task: top the pool up whenever tracks are taken."""
        while True:
            self._consumed.clear()
            try:
                await self.fill()
            except CancelledError:
                raise
            except Exception:
                log.exception("Could not refill the pool, retry in 30s")
                await sleep(30)
                continue
            if len(self._pool) < self._pool_size:
                await sleep(REPLENISH_RETRY_SECONDS)
                continue
            await self._consumed.wait()

    async def prepare(self, track: Track) -> None:
        async with self._slots:
            title = f"{', '.join(track.artists)} - {track.title}"
            path = self._workdir / f"{track.track_id}.{track.codec}"
            try:
                await self._download(track, path)
                analysis = self._cached_analysis(track)
                if analysis is None:
                    pcm = await decode_file(path)
                    analysis = await asyncio.to_thread(dj_mix.analyze, pcm)
                    del pcm
                    self._store_analysis(track, title, analysis)
                self._store_genre(track)
                mood = track.mood or dj_mix.mood_of(analysis)
            except CancelledError:
                raise
            except Exception:
                log.exception("Skip %s", title)
                path.unlink(missing_ok=True)
                return
            if not self._mood_allowed(mood):
                log.info("Skip %s: %s is not a mood wanted", title, mood)
                path.unlink(missing_ok=True)
                self._off_mood += 1
                return
        log.info(
            "Ready: %s (%.1f BPM, %.1f dB, key %s)",
            title,
            analysis.bpm,
            analysis.loudness_db,
            f"{analysis.camelot} {analysis.key_name}"
            if analysis.camelot
            else "?",
        )
        self._pool.append(
            Candidate(
                title,
                path,
                analysis,
                frozenset(track.artists),
                track,
                mood,
                track.genre,
            )
        )
        self._added.set()

    def _cached_analysis(self, track: Track) -> TrackAnalysis | None:
        if self._cache is None:
            return None
        analysis = self._cache.get_analysis(track.track_id, track.codec)
        if analysis is not None:
            log.debug("Analysis of %s is cached", track.track_id)
        return analysis

    def _store_analysis(
        self, track: Track, title: str, analysis: TrackAnalysis
    ) -> None:
        if self._cache is not None:
            self._cache.put_analysis(
                track.track_id, track.codec, title, analysis, track.genre
            )

    def _store_genre(self, track: Track) -> None:
        """Keep the genre of a track whose analysis was cached before."""
        if self._cache is not None and track.genre:
            self._cache.set_genre(track.track_id, track.codec, track.genre)

    async def _download(self, track: Track, path: Path) -> None:
        await self._backend.download(track, path)

    def _choose(self, current: TrackAnalysis | None) -> tuple[Candidate, float]:
        """Return the best next track and what its transition costs."""
        entries = [
            dj_mix.Entry(c.analysis, c.artists, c.mood) for c in self._pool
        ]
        history = tuple(self._history)
        index = dj_mix.choose_next(current, entries, history, self._position)
        cost, _ = dj_mix.step_cost(
            current, entries[index], history, self._position
        )
        return self._pool[index], cost

    async def _pick(self, current: TrackAnalysis | None) -> Candidate:
        """Take the track to play next out of the pool.

        If nothing in the pool fits, a few more tracks are loaded to choose
        from. If the station has nothing new at all, the track that was
        played the longest ago is played again.
        """
        extra = 0
        while True:
            self._drop_unwanted()
            if not self._pool and not await self._load(
                EXTRA_TRACKS, if_empty=True
            ):
                return await self._replay_oldest()
            if not self._pool:
                continue
            candidate, cost = self._choose(current)
            if cost <= dj_mix.SUITABLE_COST or extra >= MAX_EXTRA_TRACKS:
                break
            log.info(
                "Nothing fits well (best: %s, cost %.1f), loading more",
                candidate.title,
                cost,
            )
            added = await self._load(
                min(EXTRA_TRACKS, MAX_EXTRA_TRACKS - extra),
                mood=dj_mix.mood_for(self._position),
            )
            if not added:
                break
            extra += added
        self._pool.remove(candidate)
        return candidate

    async def _replay_oldest(self) -> Candidate:
        """Download again the track that was played the longest ago."""
        for track_id, played in list(self._played.items()):
            if track_id == self._playing_id and len(self._played) > 1:
                continue
            path = self._workdir / f"{track_id}-again.{played.track.codec}"
            try:
                await self._download(played.track, path)
            except CancelledError:
                raise
            except Exception:
                log.exception("Cannot repeat %s", played.title)
                path.unlink(missing_ok=True)
                continue
            log.info("The station repeats itself, again: %s", played.title)
            return Candidate(
                played.title,
                path,
                played.analysis,
                played.artists,
                played.track,
                genre=played.track.genre,
            )
        raise RuntimeError("No tracks to play")

    def _mark_played(self, candidate: Candidate) -> None:
        self._history.append(candidate.artists)
        self._position += 1
        track = candidate.track
        if track is None:
            return
        self._playing_id = track.track_id
        if self._cache is not None:
            self._cache.mark_played(track.track_id, track.codec)
        self._played[track.track_id] = Played(
            track, candidate.title, candidate.analysis, candidate.artists
        )
        self._played.move_to_end(track.track_id)
        while len(self._played) > MAX_PLAYED_REMEMBERED:
            self._played.popitem(last=False)

    async def next_deck(self, current: TrackAnalysis | None) -> Deck:
        """Choose, tempo-match and decode the track to play after current."""
        while True:
            generation = self._generation
            candidate = await self._pick(current)
            self._mark_played(candidate)
            self._consumed.set()

            speed = 0.0
            if current is not None:
                speed = dj_mix.transition_speed(current.bpm, candidate.analysis)
            try:
                pcm = await decode_file(candidate.path)
            except BaseException:
                candidate.path.unlink(missing_ok=True)
                raise
            deck = Deck(
                candidate.title,
                pcm,
                candidate.analysis,
                beat_matched=bool(speed),
                track=candidate.track,
                mood=candidate.mood,
                speed=speed or 1.0,
                source=candidate,
                generation=generation,
            )
            if generation != self._generation:
                # The queue was rebuilt while the track was decoded.
                self._unpick(deck)
                continue
            log.info(
                "Next: %s (%.1f BPM, key %s%s)",
                candidate.title,
                deck.analysis.bpm,
                deck.analysis.camelot or "?",
                f", tempo glides by {abs(speed - 1):.1%}" if speed else "",
            )
            self._next = deck
            return deck

    def transition(
        self,
        kind: dj_mix.Transition,
        current: Deck,
        following: Deck,
        effect: dj_mix.Effect = dj_mix.Effect.NONE,
        report: BlendReport | None = None,
    ) -> None:
        """Count a blend of the current deck into the following one."""
        if report is not None:
            self._recent.append(report)
        # Natural blends answer a request for the next track as well.
        self._skip = False
        self._blending = True
        relation = dj_mix.key_relation(
            current.analysis.key, following.analysis.key
        )
        self._transitions[kind.value] += 1
        self._effects[effect.value] += 1
        self._transition_keys[relation] += 1
        self._key_pairs[
            current.analysis.camelot, following.analysis.camelot
        ] += 1
        self._matched_transitions += following.beat_matched
        if self._cache is not None:
            self._cache.add_transition(
                kind.value,
                beat_matched=following.beat_matched,
                key_relation=relation,
                from_track=current.track.track_id if current.track else "",
                to_track=following.track.track_id if following.track else "",
                from_key=current.analysis.camelot,
                to_key=following.analysis.camelot,
                effect=effect.value,
            )

    async def wait_feedback(self) -> None:
        """Wait until the feedback that is on its way has been sent."""
        await gather(*self._feedback_tasks)

    def recent_blends(self) -> list[dict[str, object]]:
        """Describe the last blends, the newest last."""
        return [report.to_dict() for report in self._recent]

    def transition_stats(self) -> dict[str, object]:
        """Describe the transitions of this run and of all runs."""
        by_key = dict(self._transition_keys.most_common())
        session: dict[str, object] = {
            "total": self._transitions.total(),
            "beat_matched": self._matched_transitions,
            "by_type": dict(self._transitions.most_common()),
            "by_effect": dict(self._effects.most_common()),
            "by_key": by_key,
            "by_quality": dj_mix.quality_counts(by_key),
        }
        all_time = (
            self._cache.transition_stats() if self._cache is not None else None
        )
        return {"session": session, "all_time": all_time}

    def key_pair_stats(self) -> dict[str, object]:
        """List every change of key there was, with how often."""
        session = [
            _key_pair(now, then, number)
            for (now, then), number in self._key_pairs.most_common()
        ]
        all_time = (
            [_key_pair(a, b, n) for a, b, n in self._cache.key_pairs()]
            if self._cache is not None
            else None
        )
        return {"session": session, "all_time": all_time}

    def started(self, deck: Deck) -> None:
        """Tell the station the track has begun to be heard."""
        if self._now is not None:
            self._before.append(
                TrackInfo.of(
                    self._now.title,
                    self._now.analysis,
                    self._now.track,
                    self._now.mood,
                )
            )
        self._now = deck
        self._now_since = time.monotonic()
        if deck.source is not None:
            deck.source.path.unlink(missing_ok=True)
            deck.source = None
        if self._next is deck:
            self._next = None
        self._send_feedback(deck, Feedback.STARTED, 0.0)

    def finished(self, deck: Deck) -> None:
        """Tell the station the track has been heard to the end, or skipped."""
        self._blending = False
        if self._heard_seconds is not None:
            seconds, self._heard_seconds = self._heard_seconds, None
            self._send_feedback(deck, Feedback.SKIPPED, seconds)
            return
        seconds = (deck.analysis.end - deck.analysis.start) / SAMPLE_RATE
        self._send_feedback(deck, Feedback.FINISHED, seconds)

    def _send_feedback(
        self, deck: Deck, kind: Feedback, seconds: float
    ) -> None:
        track = deck.track
        if track is None:
            return
        task = create_task(
            self._backend.feedback_track(track, kind, self.station_id, seconds)
        )
        self._feedback_tasks.add(task)
        task.add_done_callback(self._feedback_tasks.discard)


type Piece = tuple[Pcm, str]


def _pieces(pcm: Pcm, title: str) -> list[Piece]:
    size = round(CHUNK_SECONDS * SAMPLE_RATE)
    return [(pcm[i : i + size], title) for i in range(0, len(pcm), size)]


@dataclass(frozen=True, slots=True)
class BlendPlan:
    """Where a blend takes its pieces from (samples), and at what pace."""

    start: int  # in the outgoing track
    following_start: int  # in the incoming track
    length: int  # of the outgoing piece
    speed: float  # incoming samples per outgoing sample


def _plan(current: Deck, following: Deck, position: int) -> BlendPlan:
    """Plan the blend of the following deck into the current one."""
    planned = None
    if following.beat_matched:
        planned = dj_mix.plan_matched(
            current.analysis, following.analysis, following.speed
        )
    # Without room for a blend on the beat the tracks are not matched.
    following.beat_matched = planned is not None
    if planned is None:
        planned = dj_mix.plan_blend(
            current.analysis, following.analysis, beat_matched=False
        )
    start, following_start, length = planned
    speed = following.speed if following.beat_matched else 1.0
    start = max(start, position)
    length = max(
        0,
        min(
            length,
            current.analysis.end - start,
            # The decoded track may be a few samples shorter than its
            # analysis says (rounding).
            len(current.pcm) - start,
            int((len(following.pcm) - following_start) / speed),
        ),
    )
    return BlendPlan(start, following_start, length, speed)


class DeckSource(Protocol):
    """What the mix needs of the radio: decks to play, and word of them."""

    @property
    def skip_requested(self) -> bool: ...

    async def next_deck(self, current: TrackAnalysis | None, /) -> Deck: ...

    def is_stale(self, deck: Deck, /) -> bool: ...

    def skip_taken(self) -> None: ...

    def transition(
        self,
        kind: dj_mix.Transition,
        current: Deck,
        following: Deck,
        effect: dj_mix.Effect = ...,
        report: BlendReport | None = ...,
    ) -> None: ...

    def started(self, deck: Deck, /) -> None: ...

    def finished(self, deck: Deck, /) -> None: ...


def _early_plan(
    radio: DeckSource,
    current: Deck,
    task: Task[Deck],
    position: int,
) -> BlendPlan | None:
    """Plan the blend asked for by the skip button, if it can be made now.

    It needs the next deck to be ready (the music goes on until it is) and
    some room left in both tracks.
    """
    if task.cancelled() or not task.done() or task.exception() is not None:
        return None
    following = task.result()
    if radio.is_stale(following):
        return None
    speed = following.speed if following.beat_matched else 1.0
    planned = dj_mix.plan_early(
        current.analysis,
        following.analysis,
        position,
        beat_matched=following.beat_matched,
        speed=speed,
    )
    if planned is None:
        return None
    start, following_start, length = planned
    length = min(
        length,
        len(current.pcm) - start,
        int((len(following.pcm) - following_start) / speed),
    )
    if length < dj_mix.MIN_EARLY_BLEND_SECONDS * SAMPLE_RATE:
        return None
    return BlendPlan(start, following_start, length, speed)


async def _blend_pieces(
    current: Deck, following: Deck, plan: BlendPlan
) -> tuple[Pcm, Pcm, npt.NDArray[np.float64] | None]:
    """Cut the two pieces that are blended, equally long.

    If the tempos differ, the pieces glide from one to the other (and the
    beats of the blend are returned); otherwise they are just cut out.
    """
    start, following_start = plan.start, plan.following_start
    glides = (
        following.beat_matched
        and abs(plan.speed - 1) >= dj_mix.NEGLIGIBLE_TEMPO_SHIFT
    )
    if glides:
        span = round(plan.length * plan.speed)
        margin = dj_mix.GLIDE_MARGIN
        try:
            # Stretching takes a while too.
            return await asyncio.to_thread(
                dj_mix.tempo_glide,
                current.pcm[start : start + plan.length + margin],
                following.pcm[
                    following_start : following_start + span + margin
                ],
                current.analysis.bpm,
                plan.speed,
                plan.length,
            )
        except Exception:
            log.exception("The tempo glide failed, the beats will not match")
            following.beat_matched = False
    outgoing = current.pcm[start : start + plan.length]
    incoming = following.pcm[following_start : following_start + plan.length]
    # The pieces must be equally long, whatever the analysis says.
    length = min(len(outgoing), len(incoming))
    return outgoing[:length], incoming[:length], None


def _beat_period(
    current: Deck, following: Deck, beat_grid: npt.NDArray[np.float64] | None
) -> float:
    """Return the samples per beat of the blend, 0 if the beats don't match."""
    if not following.beat_matched:
        return 0.0
    if beat_grid is None or len(beat_grid) < MIN_BEATS_IN_GRID:
        return current.analysis.beat_period
    return float(beat_grid[-1] - beat_grid[0]) / (len(beat_grid) - 1)


def _followed_until(plan: BlendPlan, following: Deck) -> int:
    """Return where in the following deck its own tempo goes on after a blend.

    A blend that glided takes `speed` times as much of the following track
    as of the outgoing one, however long the glide made the blend.
    """
    glided = (
        following.beat_matched
        and abs(plan.speed - 1) >= dj_mix.NEGLIGIBLE_TEMPO_SHIFT
    )
    return plan.following_start + round(
        plan.length * (plan.speed if glided else 1.0)
    )


async def _transition(
    radio: DeckSource,
    current: Deck,
    following: Deck,
    plan: BlendPlan,
    effects: float,
) -> AsyncIterator[Piece]:
    """Blend the following deck into the current one, piece by piece."""
    outgoing, incoming, beat_grid = await _blend_pieces(
        current, following, plan
    )
    beat_period = _beat_period(current, following, beat_grid)
    kind = dj_mix.choose_transition(
        current.analysis,
        following.analysis,
        beat_matched=following.beat_matched,
        blend_seconds=len(outgoing) / SAMPLE_RATE,
    )
    used = dj_mix.applied_blend(kind, len(outgoing), beat_period, effects)
    # The filtering takes a while, don't stall the event loop with it.
    try:
        blended = await asyncio.to_thread(
            functools.partial(
                dj_mix.blend,
                kind,
                outgoing,
                incoming,
                beat_period,
                effects=effects,
                beat_grid=beat_grid,
            )
        )
    except Exception:
        # A broken blend must not end the stream: fade instead.
        log.exception("Blend failed (%s), crossfading", kind.value)
        blended = dj_mix.crossfade(outgoing, incoming)
        used = dj_mix.Applied(dj_mix.Transition.CROSSFADE, dj_mix.Effect.NONE)
    # Two loud tracks and the effects add up to more than fits in the encoder.
    peak = float(np.abs(blended).max())
    over_db = max(0.0, 20 * log10(peak / dj_mix.LIMIT_CEILING)) if peak else 0.0
    blended = dj_mix.limit(blended)
    report = BlendReport(
        from_title=current.title,
        to_title=following.title,
        kind=used.kind.value,
        effect=used.effect.value,
        beat_matched=following.beat_matched,
        from_bpm=round(current.analysis.bpm, 1),
        to_bpm=round(following.analysis.bpm, 1),
        glide=round(plan.speed - 1, 3) if beat_grid is not None else 0.0,
        seconds=round(len(blended) / SAMPLE_RATE, 1),
        loudness_gap_db=round(
            abs(current.analysis.loudness_db - following.analysis.loudness_db),
            1,
        ),
        key_relation=dj_mix.key_relation(
            current.analysis.key, following.analysis.key
        ),
        over_db=round(over_db, 1),
        at=time.time(),
    )
    log.info(
        "Transition: %s, effect %s, %.1f s, glide %+.1f%%, over the ceiling "
        "by %.1f dB",
        used.kind.value,
        used.effect.value,
        report.seconds,
        report.glide * 100,
        report.over_db,
    )
    # Count what was really made, not what was meant.
    radio.transition(used.kind, current, following, used.effect, report)
    radio.started(following)
    half = len(blended) // 2
    for piece in _pieces(blended[:half], current.title):
        yield piece
    for piece in _pieces(blended[half:], following.title):
        yield piece
    radio.finished(current)


async def _ready_deck(
    radio: DeckSource, current: Deck, task: Task[Deck]
) -> Deck:
    """Wait for the next deck; choose again if the queue was rebuilt."""
    following = await task
    while radio.is_stale(following):
        following = await radio.next_deck(current.analysis)
    return following


async def mix(radio: DeckSource, effects: float = 0.0) -> AsyncIterator[Piece]:
    """Produce the endless mix: PCM pieces and the title of each."""
    current = await radio.next_deck(None)
    radio.started(current)
    position = current.analysis.start
    lookahead = LOOKAHEAD_SECONDS * SAMPLE_RATE

    following_task: Task[Deck] | None = None
    try:
        while True:
            following_task = create_task(radio.next_deck(current.analysis))

            # Everything up to the look-ahead point can be played without
            # knowing the next track, unless the next track is asked for.
            solo_end = max(position, current.analysis.end - lookahead)
            early: BlendPlan | None = None
            for piece in _pieces(current.pcm[position:solo_end], current.title):
                yield piece
                position += len(piece[0])
                if radio.skip_requested:
                    early = _early_plan(
                        radio, current, following_task, position
                    )
                    if early is not None:
                        break
            if early is None:
                position = solo_end

            following = await _ready_deck(radio, current, following_task)
            if early is not None:
                radio.skip_taken()
            plan = early or _plan(current, following, position)

            for piece in _pieces(
                current.pcm[position : plan.start], current.title
            ):
                yield piece
            async for piece in _transition(
                radio, current, following, plan, effects
            ):
                yield piece
            position = _followed_until(plan, following)
            current = following
    finally:
        if following_task is not None:
            following_task.cancel()


# MPEG-1 Layer III, which is what ffmpeg's libmp3lame makes of 44.1 kHz audio.
MP3_BITRATES_KBPS = (
    0,
    32,
    40,
    48,
    56,
    64,
    80,
    96,
    112,
    128,
    160,
    192,
    224,
    256,
    320,
)
MP3_SAMPLE_RATES = (44100, 48000, 32000)
MP3_HEADER_SIZE = 4
MP3_SYNC = 0xFF
# The second byte of a header without its last bit ("protection"): MPEG-1
# (the two version bits), Layer III (the two layer bits).
MP3_MPEG1_LAYER3 = 0xFA
MP3_PROTECTION_BIT = 0xFE


def mp3_frame_length(header: bytes) -> int:
    """Return the length of the MP3 frame that starts like this, 0 if none.

    Only MPEG-1 Layer III, the kind this radio makes.
    """
    if len(header) < MP3_HEADER_SIZE or header[0] != MP3_SYNC:
        return 0
    # Frame sync, MPEG-1, Layer III (the last bit is "no CRC" or "CRC").
    if header[1] & MP3_PROTECTION_BIT != MP3_MPEG1_LAYER3:
        return 0
    index, rate, padding = (
        header[2] >> 4,
        (header[2] >> 2) & 3,
        (header[2] >> 1) & 1,
    )
    # Bitrate index 0 is "free" and 15 is invalid.
    if not 0 < index < len(MP3_BITRATES_KBPS) or rate >= len(MP3_SAMPLE_RATES):
        return 0
    return (
        144 * MP3_BITRATES_KBPS[index] * 1000 // MP3_SAMPLE_RATES[rate]
        + padding
    )


def mp3_start(data: bytes) -> int:
    """Return where the first whole MP3 frame in data starts.

    A listener can't start in the middle of a frame. A frame counts when the
    next one follows it (the sync pattern may happen inside the audio).
    Returns len(data) if there is none.
    """
    place = data.find(b"\xff")
    while place != -1:
        length = mp3_frame_length(data[place : place + MP3_HEADER_SIZE])
        if length:
            following = place + length
            if following + MP3_HEADER_SIZE > len(data) or mp3_frame_length(
                data[following : following + MP3_HEADER_SIZE]
            ):
                return place
        place = data.find(b"\xff", place + 1)
    return len(data)


class Broadcaster:
    """Fans the encoded stream out to the connected listeners."""

    def __init__(self, preroll_seconds: float = PREROLL_SECONDS) -> None:
        self.title = ""
        self._clients: set[Queue[bytes]] = set()
        self._first_listener = Event()
        # The latest audio, for the listeners that join.
        self._recent: deque[bytes] = deque()
        self._recent_size = 0
        self._preroll = round(preroll_seconds * MP3_BYTES_PER_SECOND)

    @property
    def has_listeners(self) -> bool:
        return bool(self._clients)

    @property
    def buffered_bytes(self) -> int:
        """The size of the latest audio kept for the listeners that join."""
        return self._recent_size

    async def wait_for_listeners(self) -> None:
        while not self._clients:
            self._first_listener.clear()
            await self._first_listener.wait()

    def subscribe(self) -> Queue[bytes]:
        """Join the stream; the latest seconds of it come at once."""
        queue: Queue[bytes] = Queue(CLIENT_QUEUE_SIZE)
        if self._recent:
            data = b"".join(self._recent)[-self._preroll :]
            queue.put_nowait(data[mp3_start(data) :])
        self._clients.add(queue)
        self._first_listener.set()
        return queue

    def unsubscribe(self, queue: Queue[bytes]) -> None:
        self._clients.discard(queue)

    def publish(self, chunk: bytes) -> None:
        self._recent.append(chunk)
        self._recent_size += len(chunk)
        while (
            len(self._recent) > 1
            and self._recent_size - len(self._recent[0]) >= self._preroll
        ):
            self._recent_size -= len(self._recent.popleft())
        for queue in self._clients:
            # A listener that can't keep up loses its oldest audio.
            with contextlib.suppress(QueueFull):
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait(chunk)


def icy_block(title: str) -> bytes:
    """Build an ICY metadata block: length in 16-byte units, then text."""
    text = f"StreamTitle='{title.replace(chr(39), chr(8217))}';".encode()
    text = text[: 255 * 16]
    padding = -len(text) % 16
    return bytes([(len(text) + padding) // 16]) + text + bytes(padding)


def _count(request: web.Request, name: str, default: int) -> int:
    try:
        value = int(request.query.get(name, default))
    except ValueError:
        raise web.HTTPBadRequest(text=f"{name} must be a number") from None
    if value < 0:
        raise web.HTTPBadRequest(text=f"{name} must not be negative")
    return min(value, MAX_LISTED)


async def _json_body(request: web.Request) -> object:
    try:
        return await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="body must be JSON") from None


def _parse_moods(body: object) -> list[str]:
    moods = body.get("moods") if isinstance(body, dict) else None
    if not isinstance(moods, list) or not all(
        isinstance(m, str) for m in moods
    ):
        raise web.HTTPBadRequest(text='expected {"moods": [...]}')
    unknown = sorted(set(moods) - set(dj_mix.ALL_MOODS))
    if unknown:
        raise web.HTTPBadRequest(
            text=f"unknown moods {unknown}, available: {list(dj_mix.ALL_MOODS)}"
        )
    return moods


def _key_pair(now: str, then: str, number: int) -> dict[str, object]:
    """Describe one change of key; a key that was not found is null."""
    relation = dj_mix.key_relation(
        dj_mix.camelot_key(now), dj_mix.camelot_key(then)
    )
    return {
        "from": now or None,
        "to": then or None,
        "relation": relation,
        "quality": dj_mix.key_quality(relation),
        "count": number,
    }


def parse_genres(body: object) -> list[str]:
    genres = body.get("genres") if isinstance(body, dict) else None
    if not isinstance(genres, list) or not all(
        isinstance(g, str) for g in genres
    ):
        raise web.HTTPBadRequest(text='expected {"genres": [...]}')
    return genres


def _add_genres_api(app: web.Application, radio: Radio) -> None:
    """Add the genre endpoints: picked here, in force after a refresh."""

    async def get_genres(_: web.Request) -> web.Response:
        return web.json_response(radio.genres_info())

    async def put_genres(request: web.Request) -> web.Response:
        radio.set_wanted_genres(parse_genres(await _json_body(request)))
        return web.json_response(radio.genres_info())

    async def delete_genres(_: web.Request) -> web.Response:
        radio.set_wanted_genres([])
        return web.json_response(radio.genres_info())

    async def put_blocked(request: web.Request) -> web.Response:
        radio.set_blocked_genres(parse_genres(await _json_body(request)))
        return web.json_response(radio.genres_info())

    async def delete_blocked(_: web.Request) -> web.Response:
        radio.set_blocked_genres([])
        return web.json_response(radio.genres_info())

    app.router.add_put("/api/genres/blocked", put_blocked)
    app.router.add_delete("/api/genres/blocked", delete_blocked)
    app.router.add_get("/api/genres", get_genres)
    app.router.add_put("/api/genres", put_genres)
    app.router.add_delete("/api/genres", delete_genres)


def _add_stats_api(app: web.Application, radio: Radio) -> None:
    """Add the endpoints on how the tracks were blended."""

    async def stats(_: web.Request) -> web.Response:
        return web.json_response(radio.transition_stats())

    async def key_pairs(_: web.Request) -> web.Response:
        return web.json_response(radio.key_pair_stats())

    async def blends(_: web.Request) -> web.Response:
        return web.json_response(radio.recent_blends())

    app.router.add_get("/api/stats", stats)
    app.router.add_get("/api/transitions", blends)
    app.router.add_get("/api/stats/keys", key_pairs)


def _add_queue_api(app: web.Application, radio: Radio) -> None:
    """Add the endpoints that change what plays: rebuild the queue, skip."""

    async def refresh_queue(_: web.Request) -> web.Response:
        radio.refresh_queue()
        return web.json_response(radio.queue())

    async def next_track(_: web.Request) -> web.Response:
        try:
            radio.request_skip()
        except SkipRefusedError as error:
            raise web.HTTPConflict(text=str(error)) from None
        # 202: the blend starts when there is a good beat for it. The whole
        # queue, like GET /api/queue gives it, not a part to be taken for it.
        return web.json_response(radio.queue(), status=202)

    app.router.add_post("/api/queue/refresh", refresh_queue)
    app.router.add_post("/api/next", next_track)


def _auth_info(
    radio: Radio, backends: MultiBackend | None
) -> dict[str, object]:
    info = radio.auth_info()
    if backends is not None:
        info["backends"] = backends.status()
    return info


def _add_backend_api(
    app: web.Application,
    radio: Radio,
    backends: MultiBackend | None,
) -> None:
    def known() -> MultiBackend:
        if backends is None:
            raise web.HTTPNotFound(text="the backends are not selectable")
        return backends

    async def turn_on(request: web.Request) -> web.Response:
        body = await _json_body(request)
        chosen = body.get("backend") if isinstance(body, dict) else None
        if not isinstance(chosen, str):
            raise web.HTTPBadRequest(text='expected {"backend": "..."}')
        try:
            await known().enable(chosen)
        except ValueError as err:
            raise web.HTTPBadRequest(text=str(err)) from err
        return web.json_response(_auth_info(radio, backends))

    async def turn_off(request: web.Request) -> web.Response:
        try:
            await known().disable(request.match_info["backend"])
        except ValueError as err:
            raise web.HTTPNotFound(text=str(err)) from err
        return web.json_response(_auth_info(radio, backends))

    app.router.add_put("/api/auth/backend", turn_on)
    app.router.add_delete("/api/auth/backend/{backend}", turn_off)


def _add_auth_api(
    app: web.Application,
    radio: Radio,
    backends: MultiBackend | None,
) -> None:
    _add_backend_api(app, radio, backends)

    async def auth(_: web.Request) -> web.Response:
        return web.json_response(_auth_info(radio, backends))

    async def sign_in(request: web.Request) -> web.Response:
        body = await _json_body(request)
        values = body.get("values") if isinstance(body, dict) else None
        which = body.get("backend") if isinstance(body, dict) else None
        if not isinstance(values, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in values.items()
        ):
            raise web.HTTPBadRequest(text='expected {"values": {...}}')
        try:
            if backends is not None and isinstance(which, str):
                await backends.authorize_backend(which, values)
            else:
                await radio.authorize(values)
        except AuthError as err:
            raise web.HTTPBadRequest(text=str(err)) from err
        return web.json_response(_auth_info(radio, backends))

    app.router.add_get("/api/auth", auth)
    app.router.add_post("/api/auth", sign_in)


def add_api(
    app: web.Application,
    radio: Radio,
    backends: MultiBackend | None = None,
) -> None:
    """Add the REST API to the app, under /api."""
    _add_auth_api(app, radio, backends)

    async def queue(request: web.Request) -> web.Response:
        return web.json_response(
            radio.queue(
                _count(request, "previous", 2), _count(request, "next", 15)
            )
        )

    def need_token() -> None:
        # The moods are a setting of the account at the backend.
        if not radio.authorized:
            raise web.HTTPServiceUnavailable(text="not authorized yet")

    async def get_moods(_: web.Request) -> web.Response:
        return web.json_response(radio.moods_info())

    async def put_moods(request: web.Request) -> web.Response:
        need_token()
        moods = _parse_moods(await _json_body(request))
        await radio.set_allowed_moods(moods)
        return web.json_response(radio.moods_info())

    async def delete_moods(_: web.Request) -> web.Response:
        need_token()
        await radio.set_allowed_moods([])
        return web.json_response(radio.moods_info())

    app.router.add_get("/api/queue", queue)
    app.router.add_get("/api/moods", get_moods)
    app.router.add_put("/api/moods", put_moods)
    app.router.add_delete("/api/moods", delete_moods)
    _add_queue_api(app, radio)
    _add_genres_api(app, radio)
    _add_stats_api(app, radio)


def _add_web_page(app: web.Application) -> None:
    """Add the page and its scripts, built by `npm run build` in frontend/."""

    async def index(_: web.Request) -> web.Response:
        return web.Response(
            body=WEB_PAGE.read_bytes(),
            content_type="text/html",
            charset="utf-8",
            # The page names its scripts by hash: always ask for a fresh one.
            headers={"Cache-Control": "no-cache"},
        )

    app.router.add_get("/", index)
    if (WEB_DIR / "assets").is_dir():
        app.router.add_static("/assets", WEB_DIR / "assets")


def make_app(
    hub: Broadcaster,
    radio: Radio | None = None,
    backends: MultiBackend | None = None,
) -> web.Application:
    async def stream(request: web.Request) -> web.StreamResponse:
        with_titles = request.headers.get("Icy-MetaData") == "1"
        headers = {
            "Content-Type": "audio/mpeg",
            "Cache-Control": "no-cache",
            "icy-name": ICY_NAME,
        }
        if with_titles:
            headers["icy-metaint"] = str(ICY_METAINT)
        response = web.StreamResponse(headers=headers)
        await response.prepare(request)

        queue = hub.subscribe()
        log.info("Listener connected: %s", request.remote)
        pending = bytearray()
        sent_title = None
        try:
            while True:
                chunk = await queue.get()
                if not with_titles:
                    await response.write(chunk)
                    continue
                pending += chunk
                while len(pending) >= ICY_METAINT:
                    block = b"\0"
                    if hub.title != sent_title:
                        sent_title = hub.title
                        block = icy_block(sent_title)
                    await response.write(bytes(pending[:ICY_METAINT]) + block)
                    del pending[:ICY_METAINT]
        except (ConnectionError, TimeoutError):
            pass
        finally:
            hub.unsubscribe(queue)
            log.info("Listener gone: %s", request.remote)
        return response

    app = web.Application()
    app.router.add_get("/stream", stream)
    _add_web_page(app)
    if radio is not None:
        add_api(app, radio, backends)
    return app


async def _encode_loop(
    radio: DeckSource, hub: Broadcaster, effects: float
) -> None:
    """Mix, encode and publish in real time while somebody listens."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-f",
        "f32le",
        "-ac",
        str(CHANNELS),
        "-ar",
        str(SAMPLE_RATE),
        "-i",
        "pipe:0",
        "-c:a",
        "libmp3lame",
        "-b:a",
        MP3_BITRATE,
        "-flush_packets",
        "1",
        "-f",
        "mp3",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    if proc.stdin is None or proc.stdout is None:
        raise RuntimeError("ffmpeg has no pipes")
    stdout = proc.stdout

    async def forward() -> None:
        while chunk := await stdout.read(4096):
            hub.publish(chunk)

    forwarder = create_task(forward(), name="dj-forward")
    loop = asyncio.get_running_loop()
    started = loop.time()
    produced = 0.0
    try:
        async for pcm, title in mix(radio, effects):
            if not hub.has_listeners:
                await hub.wait_for_listeners()
                started, produced = loop.time(), 0.0
            hub.title = title
            proc.stdin.write(np.ascontiguousarray(pcm).tobytes())
            await proc.stdin.drain()
            produced += len(pcm) / SAMPLE_RATE
            ahead = produced - (loop.time() - started) - PACING_LEAD_SECONDS
            if ahead > 0:
                await sleep(ahead)
    finally:
        forwarder.cancel()
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()


async def encode_forever(
    radio: DeckSource, hub: Broadcaster, effects: float
) -> None:
    """Keep the mix going: a failure ends only the mix it happened in."""
    while True:
        try:
            await _encode_loop(radio, hub, effects)
        except CancelledError:
            raise
        except Exception:
            log.exception(
                "The mix failed, starting it again in %ds", RESTART_SECONDS
            )
            await sleep(RESTART_SECONDS)
        else:
            return


async def _first_tracks(radio: Radio) -> None:
    """Fill the pool, and go on trying while no backend gives any tracks.

    The radio must stay up, and tell on its page and in the log what is
    wrong, rather than stop and be started again by whoever runs it.
    """
    while True:
        try:
            await radio.fill()
        except Exception:
            log.exception("Could not load the first tracks")
        if radio.pool:
            return
        log.warning(
            "No playable tracks yet, is a music backend reachable? "
            "Retry in %d s",
            STARTUP_RETRY_SECONDS,
        )
        await sleep(STARTUP_RETRY_SECONDS)


async def serve(
    backend: MusicBackend,
    *,
    host: str,
    port: int,
    station_id: str = "",
    pool_size: int = DEFAULT_POOL_SIZE,
    effects: str = DEFAULT_EFFECTS,
    mood_arc: bool = False,
    cache: TrackCache | None = None,
    ready: Event | None = None,
) -> None:
    """Serve the stream and the API, then mix until cancelled.

    The server is up before the app is authorized, so that /api/auth can
    tell where to do it.
    """
    with tempfile.TemporaryDirectory(prefix="yamusic-dj-") as workdir:
        radio = Radio(
            backend,
            Path(workdir),
            station_id=station_id,
            pool_size=pool_size,
            mood_arc=mood_arc,
            cache=cache,
        )
        hub = Broadcaster()
        runner = web.AppRunner(
            make_app(
                hub,
                radio,
                backend if isinstance(backend, MultiBackend) else None,
            ),
            access_log=None,
        )
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        log.info("Streaming at http://%s:%d/stream", host, port)
        if ready is not None:
            ready.set()
        try:
            # Waiting for the user to authorize the app.
            await backend.wait_authorized()
            # What the last run left: a failure here must not stop the radio.
            try:
                await radio.restore_state()
            except Exception:
                log.exception("Could not take up the picks of the last run")
            if mood_arc and radio.mood_arc_saved is None:
                await radio.start_mood_arc()
            log.info(
                "Preparing %d tracks from %s...", pool_size, radio.station_id
            )
            await _first_tracks(radio)
            refill = create_task(radio.keep_filled(), name="dj-refill")
            try:
                await encode_forever(radio, hub, EFFECT_LEVELS[effects])
            finally:
                refill.cancel()
                with contextlib.suppress(CancelledError):
                    await refill
        finally:
            # The mood is a setting of the account: leave it as it was.
            with contextlib.suppress(Exception):
                await radio.restore_settings()
            await runner.cleanup()
