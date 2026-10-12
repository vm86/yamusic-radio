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
import json
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
from yamusic_radio.backend import AuthError, Feedback, Track
from yamusic_radio.dj_mix import (
    CHANNELS,
    SAMPLE_RATE,
    Pcm,
    TrackAnalysis,
)
from yamusic_radio.multi import MultiBackend
from yamusic_radio.spectrum import (
    SpectrumAnalyzer,
    SpectrumFeed,
    StartPositions,
)

if TYPE_CHECKING:
    from collections.abc import (
        AsyncIterator,
        Awaitable,
        Callable,
        Iterable,
    )

    import numpy.typing as npt

    from yamusic_radio.backend import MusicBackend
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
SETTING_LANGUAGE = "language"
# The tracks loaded and waiting for their turn: the files are kept as well.
# The order of the queue that was put in place by hand.
SETTING_ORDER = "queue_order"
# What a download can be called, of the ones that are cleared away.
AUDIO_SUFFIXES = frozenset(
    {
        f".{codec}"
        for codec in (
            "mp3",
            "flac",
            "flac-mp4",
            "aac",
            "aac-mp4",
            "he-aac",
            "he-aac-mp4",
            "m4a",
            "mp4",
            "ogg",
            "opus",
            "wav",
        )
    }
)
SETTING_PLAYLIST = "playlist"
# The languages of the music a station can be set to (Yandex's own names).
LANGUAGES = ("any", "russian", "not-russian", "without-words")
# When the language is changed, the tracks loaded for the old one are kept
# only as far as they are needed for the next blends.
LANGUAGE_KEEP_TRACKS = 2
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

# The regular stream is HQ MP3; the lossless one is FLAC (16 bit, dithered).
MP3_BITRATE = "320k"
# A new listener gets this much of the latest audio at once, so that the
# player has something to start on (browsers wait for ~1.5 s of audio before
# they play) instead of waiting for it to arrive at the pace it plays.
PREROLL_SECONDS = 3.0
MP3_BYTES_PER_SECOND = int(MP3_BITRATE.rstrip("k")) * 1000 // 8
ICY_METAINT = 16000
ICY_NAME = "Yandex Music DJ"
CLIENT_QUEUE_SIZE = 256
# What a second of FLAC takes about (it follows the music): sizes the preroll.
FLAC_BYTES_PER_SECOND = 110_000
# The rate of a stream is taken from at least this much of it.
MIN_RATE_SPAN_SECONDS = 1.0
# How far what an encoder has written is behind what it was fed (its own
# delay, and a frame that waits to be filled), measured by decoding the
# streams and looking for clicks of a known position. A FLAC frame waits
# for 93 ms, so the lag of it is 0-93 ms.
MP3_LAG_SECONDS = 0.136
FLAC_LAG_SECONDS = 0.077
OGG_LAG_SECONDS = 0.076
# How long the spectrum waits for the stream of a page to connect, how far
# before the start of it the frames begin, and how often a quiet connection
# is asked if it is there.
SPECTRUM_START_WAIT = 5.0
SPECTRUM_BACKLOG_MARGIN = 0.5
SPECTRUM_KEEPALIVE_SECONDS = 15.0


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


class QueueChangeRefusedError(Exception):
    """The queue can't be changed that way now (a blend is under way)."""


class UnknownQueueTrackError(KeyError):
    """The track is not in the queue."""


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
    # Put where it is by hand: the radio doesn't move it.
    pinned: bool = False

    @classmethod
    def of(
        cls,
        title: str,
        analysis: TrackAnalysis,
        track: Track | None,
        mood: str,
        *,
        decided: bool = True,
        pinned: bool = False,
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
            pinned,
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
            "pinned": self.pinned,
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


def _candidate_json(candidate: Candidate) -> dict[str, object]:
    """Keep a candidate (its analysis is in the cache) as JSON."""
    return {
        "title": candidate.title,
        "file": candidate.path.name,
        "mood": candidate.mood,
        "track": dataclasses.asdict(candidate.track)
        if candidate.track is not None
        else None,
    }


def _track_back(data: object) -> Track:
    if not isinstance(data, dict):
        raise TypeError("a track is an object")
    names = {field.name for field in dataclasses.fields(Track)}
    values = {k: v for k, v in data.items() if k in names}
    values["artists"] = tuple(values["artists"])
    return Track(**values)


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
        keep_playlist: bool = False,
    ) -> None:
        log.info("Keys are found by %s", dj_mix.key_detector())
        self._backend = backend
        self._cache = cache
        self._workdir = workdir
        # The loaded tracks are kept in the cache (the files stay in workdir,
        # which must be a place that lasts) for the next run to start on.
        self._keep_playlist = keep_playlist and cache is not None
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
        self._init_station_settings()
        self._playing_id = ""
        # What the API shows: the track heard now, the ones before it and
        # the one already decoded to come next.
        self._now: Deck | None = None
        self._now_since = 0.0
        # The mix is paused: by the API, or because nobody listens (held).
        # The time of the track that plays does not run meanwhile.
        self._user_paused = False
        self._held = False
        self._paused_at: float | None = None
        self._resumed = Event()
        self._resumed.set()
        # The next track was asked for (see request_skip); a transition is
        # under way (from its count to the end of the track that is left);
        # and how long the track left was heard, if it was skipped.
        self._skip = False
        self._blending = False
        self._heard_seconds: float | None = None
        self._next: Deck | None = None
        # The order of the tracks put in place by hand (their ids), first
        # to play first; the rest of the queue is the radio's choice.
        self._order: list[str] = []
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

    def _init_station_settings(self) -> None:
        """Start with the settings of the station not touched."""
        self._mood = ""
        # The language setting the station has now ("" before the settings
        # are put aside) and the one picked here, None for the account's own.
        self._language = ""
        self._picked_language: str | None = None
        self._saved_settings: tuple[str, str, str] | None = None

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
        self._language = self._saved_settings[2]
        self._remember(
            SETTING_STATION,
            {"station": self.station_id, "settings": self._saved_settings},
        )

    async def restore_settings(self) -> None:
        """Put the station's settings back as they were."""
        if self._saved_settings is None:
            return
        if (self._mood, self._language) != (
            self._saved_settings[0],
            self._saved_settings[2],
        ):
            await self._backend.set_station_settings(
                self.station_id, *self._saved_settings
            )
            self._mood = self._saved_settings[0]
            self._language = self._saved_settings[2]
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
        language = self._cache.get_setting(SETTING_LANGUAGE)
        if isinstance(language, str) and language in LANGUAGES:
            await self.set_language(language)

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
        await self._release_settings()
        self._drop_unwanted()
        log.info("Moods played: %s", ", ".join(wanted) or "any")
        # Wake up the refill: the pool may be short of tracks now.
        self._consumed.set()

    async def _release_settings(self) -> None:
        """Put the station's settings back once nothing here steers them."""
        if (
            self._saved_settings is not None
            and not self._allowed_moods
            and not self._mood_arc
            and self._picked_language is None
        ):
            await self.restore_settings()
            self._saved_settings = None

    async def set_language(self, language: str | None) -> None:
        """Play music of a language, or the account's own with None.

        Like the moods, it is a setting of the station, kept on the account:
        it is changed for as long as the radio runs and put back when it
        stops. The tracks loaded already are not of a known language, so
        most of them are given up (the ones needed next stay) and the
        station is asked for tracks anew.
        """
        if language is not None and language not in LANGUAGES:
            raise ValueError(f"unknown language {language!r}")
        if language is not None and self._saved_settings is None:
            await self.start_mood_arc()
        if self._saved_settings is None:
            return  # nothing was changed: nothing to put back
        target = self._saved_settings[2] if language is None else language
        if target != self._language:
            _, diversity, _ = self._saved_settings
            await self._backend.set_station_settings(
                self.station_id, self._mood, diversity, target
            )
            self._language = target
            self._give_up_the_pool()
        self._picked_language = language
        if language is None:
            self._forget(SETTING_LANGUAGE)
        else:
            self._remember(SETTING_LANGUAGE, language)
        await self._release_settings()
        log.info("Language of the music: %s", language or "the account's own")

    def _give_up_the_pool(self) -> None:
        """Drop the loaded tracks but the few that are needed next."""
        for candidate in self._pool[LANGUAGE_KEEP_TRACKS:]:
            candidate.path.unlink(missing_ok=True)
        del self._pool[LANGUAGE_KEEP_TRACKS:]
        self._save_playlist()
        # Wake up the refill: the pool is short of tracks now.
        self._consumed.set()

    def _save_playlist(self) -> None:
        """Keep what is loaded and waiting, for a restart to take up."""
        if not self._keep_playlist:
            return
        waiting = [self._next.source] if self._next is not None else []
        entries = [
            _candidate_json(c)
            for c in (*waiting, *self._pool)
            if c is not None and c.track is not None and c.path.exists()
        ]
        self._remember(SETTING_PLAYLIST, entries)
        self._remember(SETTING_ORDER, self._order)

    def restore_playlist(self) -> int:
        """Take back the tracks of the last run that were loaded and waiting.

        Their files are in the working directory and their analyses in the
        cache, so nothing is downloaded or analyzed again. Files that are
        not of them (a download that was cut short, the track that was
        playing) are removed. Returns how many tracks came back.
        """
        if not self._keep_playlist or self._cache is None:
            return 0
        saved = self._cache.get_setting(SETTING_PLAYLIST)
        kept: set[Path] = set()
        for entry in saved if isinstance(saved, list) else []:
            candidate = self._candidate_back(entry)
            if candidate is None:
                continue
            kept.add(candidate.path)
            if self._wanted(candidate):
                self._pool.append(candidate)
                if candidate.track is not None:
                    self._seen.add(candidate.track.track_id)
            else:
                log.info("Playlist: %s is not wanted now", candidate.title)
                candidate.path.unlink(missing_ok=True)
        waiting = {c.path for c in self._pool}
        for path in self._workdir.iterdir():
            # Only audio: the directory may be somebody's, and a mistake in
            # the option must not take the cache or the token with it.
            if path.suffix in AUDIO_SUFFIXES and path not in waiting:
                path.unlink(missing_ok=True)
        order = self._cache.get_setting(SETTING_ORDER)
        waiting_ids = {
            c.track.track_id for c in self._pool if c.track is not None
        }
        self._order = [
            i
            for i in (order if isinstance(order, list) else [])
            if isinstance(i, str) and i in waiting_ids
        ]
        if self._pool:
            self._added.set()
        self._save_playlist()
        log.info("Playlist of the last run: %d tracks", len(self._pool))
        return len(self._pool)

    def _candidate_back(self, entry: object) -> Candidate | None:
        """Make a candidate of what was kept of it; None if it can't be."""
        if self._cache is None:
            return None
        try:
            if not isinstance(entry, dict):
                raise TypeError("an entry is an object")  # noqa: TRY301
            track = _track_back(entry["track"])
            path = self._workdir / Path(str(entry["file"])).name
            title = str(entry["title"])
            mood = str(entry["mood"])
        except (KeyError, TypeError, ValueError):
            log.warning("Ignoring a damaged entry of the playlist")
            return None
        analysis = self._cache.get_analysis(track.track_id, track.codec)
        if analysis is None or not path.is_file() or path.stat().st_size == 0:
            log.info("Playlist: %s is gone, left out", title)
            path.unlink(missing_ok=True)
            return None
        return Candidate(
            title,
            path,
            analysis,
            frozenset(track.artists),
            track,
            mood,
            track.genre,
        )

    def language_info(self) -> dict[str, object]:
        return {
            "available": list(LANGUAGES),
            "selected": self._picked_language,
            "station": self._language,
            # Whether the backend can change its station's language.
            "steered": self._backend.supports_moods,
        }

    @property
    def generation(self) -> int:
        return self._generation

    def refresh_queue(self) -> None:
        """Choose the next tracks anew among the ones already loaded.

        The track that was already chosen and decoded goes back to the
        pool, tracks of moods that are not wanted are dropped, the order
        put in place by hand is forgotten, and the next track is picked
        again, so the queue follows the moods selected now. Nothing is
        downloaded for it.
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
        # What was put in place by hand is the radio's to choose again.
        self._order = []
        # The track being blended in stays: it is heard already.
        deck = None if self._blending else self._next
        if deck is not None:
            self._next = None
            self._unpick(deck)
        self._drop_unwanted()
        self._save_playlist()
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
            self._save_playlist()

    def _is_pinned(self, track: Track | None) -> bool:
        return track is not None and track.track_id in self._order

    def _pinned(self, pool: list[Candidate]) -> list[Candidate]:
        """List the tracks of the pool that were put in place, in order."""
        by_id = {c.track.track_id: c for c in pool if c.track is not None}
        return [by_id[i] for i in self._order if i in by_id]

    def _queue_ids(self) -> list[str]:
        """List the ids of the tracks the API shows as coming."""
        return [t.track_id for t in self.upcoming(MAX_LISTED) if t.track_id]

    def _restart_next(self) -> None:
        """Choose the next track anew: the one decoded goes back to the pool."""
        self._generation += 1
        deck, self._next = self._next, None
        if deck is not None:
            self._unpick(deck)
        self._consumed.set()

    def _locked(self, track_id: str) -> bool:
        """Say whether the track is the one being blended in just now."""
        deck = self._next
        return (
            self._blending
            and deck is not None
            and deck.track is not None
            and deck.track.track_id == track_id
        )

    def _set_order(self, ids: list[str]) -> None:
        """Put the tracks in this order; what comes first is played first."""
        coming = self._queue_ids()
        unknown = [i for i in ids if i not in coming]
        if unknown:
            raise UnknownQueueTrackError(unknown[0])
        ids = list(dict.fromkeys(ids))
        blending_in = self._blending and self._next is not None
        # The track that is being blended in can't be put behind another.
        if blending_in and ids and coming and ids[0] != coming[0]:
            raise QueueChangeRefusedError(
                "a transition is under way: the next track is fixed"
            )
        self._order = ids
        decoded = self._next.track if self._next is not None else None
        if (
            not blending_in
            and ids
            and decoded is not None
            and ids[0] != decoded.track_id
        ):
            self._restart_next()
        self._save_playlist()

    def queue_order(self, ids: list[str]) -> None:
        """Put the tracks of the queue in the order of `ids`.

        The tracks of the queue that are left out follow them, as the
        radio chooses.
        """
        self._set_order(ids)

    def queue_move(self, track_id: str, position: int) -> None:
        """Move a track of the queue to a place in it (0 is the next)."""
        ids = self._queue_ids()
        if track_id not in ids:
            raise UnknownQueueTrackError(track_id)
        ids.remove(track_id)
        ids.insert(max(0, min(position, len(ids))), track_id)
        self._set_order(ids)

    def queue_remove(self, track_id: str) -> None:
        """Take a track out of the queue: it is not played, nor loaded again."""
        if track_id not in self._queue_ids():
            raise UnknownQueueTrackError(track_id)
        if self._locked(track_id):
            raise QueueChangeRefusedError(
                "a transition is under way: the next track is fixed"
            )
        decoded = self._next.track if self._next is not None else None
        if decoded is not None and decoded.track_id == track_id:
            self._restart_next()
        removed = next(
            c
            for c in self._pool
            if c.track is not None and c.track.track_id == track_id
        )
        self._pool.remove(removed)
        removed.path.unlink(missing_ok=True)
        if self._cache is not None and removed.track is not None:
            # Not offered again after a restart either.
            self._cache.mark_played(removed.track.track_id, removed.track.codec)
        self._order = [i for i in self._order if i != track_id]
        self._save_playlist()
        self._consumed.set()
        log.info("Out of the queue: %s", removed.title)

    def queue_play(self, track_id: str) -> None:
        """Play a track of the queue next, as soon as a blend can start."""
        ids = self._queue_ids()
        if track_id not in ids:
            raise UnknownQueueTrackError(track_id)
        # Refused before anything is changed if a blend is under way.
        self.request_skip()
        ids.remove(track_id)
        self._set_order([track_id, *ids])

    def _wanted(self, candidate: Candidate) -> bool:
        """Say whether a pool track may be played.

        Its mood and genre are allowed, and its audio is there to be read.
        """
        return (
            self._mood_allowed(candidate.mood)
            and self._genre_allowed(candidate.genre)
            and self._reachable(candidate)
        )

    def _reachable(self, candidate: Candidate) -> bool:
        """Say whether the audio of a pool track can still be read.

        A track that was downloaded is a file of its own. One that is only
        linked (a folder's) stays in its backend's storage, so it goes
        with the backend when that is turned off or can't be reached.
        """
        if not candidate.path.exists():
            return False  # gone, or a link to what is gone
        track = candidate.track
        if track is None or not candidate.path.is_symlink():
            return True
        backend = self._backend
        gone = (
            backend.unavailable() if isinstance(backend, MultiBackend) else ()
        )
        return track.source not in gone

    def _mood_allowed(self, mood: str) -> bool:
        return not self._allowed_moods or mood in self._allowed_moods

    def _genre_allowed(self, genre: str) -> bool:
        if genre in self._allowed_blocked:
            return False
        return not self._allowed_genres or genre in self._allowed_genres

    def _drop_unwanted(self) -> None:
        """Forget the pool tracks of other moods or genres, or out of reach."""
        decoded = self._next.source if self._next is not None else None
        if (
            decoded is not None
            and not self._blending
            and not self._reachable(decoded)
        ):
            # Chosen and decoded already, but of a backend that went: it
            # goes back to the pool, to be given up like the others.
            self._restart_next()
        for candidate in list(self._pool):
            if not self._wanted(candidate):
                self._pool.remove(candidate)
                candidate.path.unlink(missing_ok=True)
                if not self._reachable(candidate) and candidate.track:
                    # Not heard: it may be offered again when it is back.
                    self._seen.discard(candidate.track.track_id)
                self._save_playlist()

    def backends_changed(self) -> None:
        """Let go of the tracks of a backend that went, and look for more."""
        self._drop_unwanted()
        self._consumed.set()

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
                    "elapsed_seconds": round(self._heard(), 1),
                }
                if now is not None
                else None
            ),
            "next": [t.to_dict() for t in self.upcoming(after)],
            "moods": self.moods_info(),
            "language": self.language_info(),
            **self.skip_info(),
            **self.pause_info(),
        }

    def skip_info(self) -> dict[str, object]:
        """Where the next track button is: asked for, under way, or free."""
        return {
            # Asked for, and waiting for a good beat to start the blend.
            "skip_pending": self._skip,
            # The blend is under way (until the old track is left behind).
            "blending": self._blending,
            "can_skip": (
                self._now is not None and not self._skip and not self._blending
            ),
        }

    def _heard(self) -> float:
        """Seconds of the track that plays, not counting the pauses."""
        return (self._paused_at or time.monotonic()) - self._now_since

    def _sync_clock(self) -> None:
        """Stop the time of the track while paused, run it again after."""
        paused = self.paused
        if paused and self._paused_at is None:
            self._paused_at = time.monotonic()
        elif not paused and self._paused_at is not None:
            self._now_since += time.monotonic() - self._paused_at
            self._paused_at = None

    @property
    def paused(self) -> bool:
        """Is the mix standing: paused by the API, or nobody listens."""
        return self._user_paused or self._held

    @property
    def user_paused(self) -> bool:
        return self._user_paused

    def pause(self) -> None:
        """Pause the mix for everyone, until resume()."""
        self._user_paused = True
        self._resumed.clear()
        self._sync_clock()

    def resume(self) -> None:
        self._user_paused = False
        self._resumed.set()
        self._sync_clock()

    def hold(self, *, held: bool) -> None:
        """Note that the mix waits for listeners, or goes on."""
        self._held = held
        self._sync_clock()

    async def wait_resumed(self) -> None:
        await self._resumed.wait()

    def pause_info(self) -> dict[str, object]:
        reason = (
            "user"
            if self._user_paused
            else "no_listeners"
            if self._held
            else None
        )
        return {"paused": reason is not None, "pause_reason": reason}

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
        self._heard_seconds = self._heard()

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
                    pinned=self._is_pinned(self._next.track),
                )
            )
        pool = [c for c in self._pool if self._wanted(c)]
        history = tuple(self._history)
        position = self._position
        # What was put in place by hand comes in that order.
        for candidate in self._pinned(pool):
            if len(found) >= limit:
                break
            pool.remove(candidate)
            found.append(
                TrackInfo.of(
                    candidate.title,
                    candidate.analysis,
                    candidate.track,
                    candidate.mood,
                    pinned=True,
                )
            )
            history = (*history, candidate.artists)[-dj_mix.REPEAT_WINDOW :]
            position += 1
            current = candidate.analysis
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
        _, diversity, _ = self._saved_settings
        await self._backend.set_station_settings(
            self.station_id, mood, diversity, self._language
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
            self._drop_unwanted()
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
        self._save_playlist()
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
            if pinned := self._pinned(self._pool):
                # Put in place by hand: no need to find what fits.
                candidate = pinned[0]
                break
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
        self._save_playlist()
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
            self._save_playlist()
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
        if self._paused_at is not None:
            self._paused_at = self._now_since
        if deck.source is not None:
            deck.source.path.unlink(missing_ok=True)
            deck.source = None
        if deck.track is not None and deck.track.track_id in self._order:
            self._order.remove(deck.track.track_id)
        if self._next is deck:
            self._next = None
            self._save_playlist()
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

    @property
    def user_paused(self) -> bool: ...

    def hold(self, *, held: bool) -> None: ...

    async def wait_resumed(self) -> None: ...


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


def _fresh(radio: DeckSource, current: Deck, task: Task[Deck]) -> Task[Deck]:
    """Choose the next deck anew at once if the queue was changed.

    A deck that was chosen before the change would wait to be found stale
    until the track is nearly over; a track asked for would wait with it.
    """
    stale = (
        task.done()
        and not task.cancelled()
        and task.exception() is None
        and radio.is_stale(task.result())
    )
    return create_task(radio.next_deck(current.analysis)) if stale else task


@dataclass(slots=True)
class _Approach:
    """The deck to blend in and the plan for it, as the blend nears."""

    following: Deck
    plan: BlendPlan
    position: int


async def _approach(
    radio: DeckSource, current: Deck, approach: _Approach
) -> AsyncIterator[Piece]:
    """Play the track up to its blend, and choose again if the queue changed.

    Until the blend begins the queue may still be changed by hand: the deck
    that was chosen is then given up for another, and the blend planned anew.
    """
    while True:
        pieces = _pieces(
            current.pcm[approach.position : approach.plan.start], current.title
        )
        for piece in pieces:
            yield piece
            approach.position += len(piece[0])
            if radio.is_stale(approach.following):
                break
        else:
            approach.position = max(approach.position, approach.plan.start)
            return
        task = create_task(radio.next_deck(current.analysis))
        approach.following = await _ready_deck(radio, current, task)
        approach.plan = _plan(current, approach.following, approach.position)


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
                following_task = _fresh(radio, current, following_task)
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

            approach = _Approach(following, plan, position)
            async for piece in _approach(radio, current, approach):
                yield piece
            following, plan = approach.following, approach.plan
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


FLAC_MARKER = b"fLaC"
FLAC_BLOCK_HEADER_SIZE = 4
FLAC_LAST_BLOCK = 0x80
FLAC_FRAME_SYNC = (0xFF, 0xF8)  # a fixed block size: what ffmpeg writes
FLAC_FRAME_HEADER_MIN = 6
FLAC_FRAME_HEADER_MAX = 16
FLAC_UTF8_LEAD_MAX = 8
FLAC_CRC8_POLYNOMIAL = 0x07


def flac_header_length(data: bytes) -> int | None:
    """Return how long the header (marker and metadata) of a FLAC stream is.

    None if data doesn't hold the whole of it yet.
    """
    if not data.startswith(FLAC_MARKER[: len(data)]):
        raise ValueError("not a FLAC stream")
    place = len(FLAC_MARKER)
    while place + FLAC_BLOCK_HEADER_SIZE <= len(data):
        last = data[place] & FLAC_LAST_BLOCK
        size = int.from_bytes(data[place + 1 : place + 4], "big")
        place += FLAC_BLOCK_HEADER_SIZE + size
        if last:
            return place if place <= len(data) else None
    return None


def _crc8(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (
                ((crc << 1) ^ FLAC_CRC8_POLYNOMIAL) & 0xFF
                if crc & 0x80
                else (crc << 1) & 0xFF
            )
    return crc


def _flac_frame_header_length(data: bytes, place: int) -> int:
    """Return the length of the frame header at place, 0 if it is not one."""
    head = data[place : place + FLAC_FRAME_HEADER_MAX]
    if len(head) < FLAC_FRAME_HEADER_MIN or head[3] & 1:  # reserved bit
        return 0
    block_code, rate_code = head[2] >> 4, head[2] & 0xF
    # The frame number is UTF-8 coded: the leading ones give its length.
    lead = 0
    while lead < FLAC_UTF8_LEAD_MAX and head[4] & (0x80 >> lead):
        lead += 1
    if lead in {1, FLAC_UTF8_LEAD_MAX}:
        return 0
    size = 4 + max(lead, 1)
    size += {6: 1, 7: 2}.get(block_code, 0)  # the block size follows
    size += {12: 1, 13: 2, 14: 2}.get(rate_code, 0)  # so does the rate
    return size + 1 if size + 1 <= len(head) else 0


def flac_start(data: bytes) -> int:
    """Return where the first whole FLAC frame in data starts.

    A frame counts when the CRC-8 of its header is right (the sync pattern
    may happen inside the audio). Returns len(data) if there is none.
    """
    place = data.find(bytes(FLAC_FRAME_SYNC))
    while place != -1:
        length = _flac_frame_header_length(data, place)
        if (
            length
            and _crc8(data[place : place + length - 1])
            == data[place + length - 1]
        ):
            return place
        place = data.find(bytes(FLAC_FRAME_SYNC), place + 1)
    return len(data)


OGG_MARKER = b"OggS"
OGG_PAGE_HEADER_SIZE = 27
OGG_CONTINUED = 0x01
OGG_BEGINNING = 0x02
OGG_CRC_POLYNOMIAL = 0x04C11DB7


def _ogg_crc_table() -> tuple[int, ...]:
    table = []
    for byte in range(256):
        crc = byte << 24
        for _ in range(8):
            crc = (
                ((crc << 1) ^ OGG_CRC_POLYNOMIAL) & 0xFFFFFFFF
                if crc & 0x80000000
                else (crc << 1) & 0xFFFFFFFF
            )
        table.append(crc)
    return tuple(table)


_OGG_CRC_TABLE = _ogg_crc_table()


def _ogg_crc(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _OGG_CRC_TABLE[(crc >> 24) ^ byte]
    return crc


def ogg_page_length(data: bytes, place: int = 0) -> int | None:
    """Return the length of the Ogg page at place; 0 if there is none.

    None if data ends before the page does. A page counts when its CRC is
    right (the marker may happen inside the audio).
    """
    if data[place : place + 4] != OGG_MARKER[: len(data) - place]:
        return 0
    if len(data) < place + OGG_PAGE_HEADER_SIZE:
        return None
    if data[place + 4] != 0:  # the version
        return 0
    segments = data[place + 26]
    if len(data) < place + OGG_PAGE_HEADER_SIZE + segments:
        return None
    table = data[place + OGG_PAGE_HEADER_SIZE : place + 27 + segments]
    size = OGG_PAGE_HEADER_SIZE + segments + sum(table)
    if len(data) < place + size:
        return None
    page = bytearray(data[place : place + size])
    stored = int.from_bytes(page[22:26], "little")
    page[22:26] = bytes(4)
    return size if _ogg_crc(bytes(page)) == stored else 0


def _ogg_granule(data: bytes, place: int) -> int:
    return int.from_bytes(data[place + 6 : place + 14], "little", signed=True)


def ogg_header_length(data: bytes) -> int | None:
    """Return how long the header pages of an Ogg stream are.

    These are the pages before the first one of audio, which says where it
    is in the stream (its granule position) when the header's say nothing.
    None if data doesn't hold them and that page yet.
    """
    place = 0
    while True:
        size = ogg_page_length(data, place)
        if size is None:
            return None
        if size == 0:
            raise ValueError("not an Ogg stream")
        if place and _ogg_granule(data, place) != 0:
            return place
        place += size


def ogg_start(data: bytes) -> int:
    """Return where the first whole page of audio in data starts.

    A listener can start on a page that doesn't go on from the one before
    it. Returns len(data) if there is none.
    """
    place = data.find(OGG_MARKER)
    while place != -1:
        size = ogg_page_length(data, place)
        if (
            size
            and not data[place + 5] & (OGG_CONTINUED | OGG_BEGINNING)
            and _ogg_granule(data, place) > 0
        ):
            return place
        place = data.find(OGG_MARKER, place + 1)
    return len(data)


class Broadcaster:
    """Fans the encoded stream out to the connected listeners."""

    def __init__(
        self,
        preroll_seconds: float = PREROLL_SECONDS,
        *,
        bytes_per_second: int = MP3_BYTES_PER_SECOND,
        frame_start: Callable[[bytes], int] = mp3_start,
        header_length: Callable[[bytes], int | None] | None = None,
        lag_seconds: float = MP3_LAG_SECONDS,
    ) -> None:
        self.title = ""
        # A stream that starts with a header (FLAC) says how to tell its
        # length; it is kept to be sent first to the listeners that join.
        self.header = b""
        self.header_length = header_length
        self._frame_start = frame_start
        self._clients: set[Queue[bytes]] = set()
        self._first_listener = Event()
        # The latest audio, for the listeners that join.
        self._recent: deque[bytes] = deque()
        self._recent_size = 0
        self._preroll = round(preroll_seconds * bytes_per_second)
        self._bytes_per_second = bytes_per_second
        self._lag = lag_seconds
        # Where in the mix the stream is: the seconds of it that were fed to
        # the encoder (set by whoever feeds it) and, for each recent chunk,
        # the bytes published up to its end and the seconds then.
        self.fed = 0.0
        self._total = 0
        self._marks: deque[tuple[int, float]] = deque()

    def reset_position(self) -> None:
        """Forget where the stream was: the mix starts over."""
        self.fed = 0.0
        self._marks.clear()

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

    async def wait_for_a_listener(self, *others: Broadcaster) -> None:
        """Wait until this stream or any of the others has a listener."""
        hubs = (self, *others)
        while not any(hub.has_listeners for hub in hubs):
            waits = [create_task(hub.wait_for_listeners()) for hub in hubs]
            _, pending = await asyncio.wait(
                waits, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()

    def position_of(self, offset: int) -> float | None:
        """Return the position in the mix of a byte of the stream.

        The end of what was published is where the mix was fed to; the
        bytes before it take as long as the stream takes per byte lately (a
        FLAC stream follows the music, so it is not the nominal rate). None
        if nothing was published.
        """
        if not self._marks:
            return None
        end, seconds = self._marks[-1]
        first_end, first_seconds = self._marks[0]
        lately = seconds - first_seconds
        rate = (
            (end - first_end) / lately
            if lately >= MIN_RATE_SPAN_SECONDS and end > first_end
            else self._bytes_per_second
        )
        return seconds - self._lag - (end - offset) / rate

    def subscribe(self) -> Queue[bytes]:
        """Join the stream; the latest seconds of it come at once."""
        return self.subscribe_at()[0]

    def subscribe_at(self) -> tuple[Queue[bytes], float | None]:
        """Join the stream, and say where in the mix it starts for you."""
        queue: Queue[bytes] = Queue(CLIENT_QUEUE_SIZE)
        if self.header:
            queue.put_nowait(self.header)
        # The first listener of a mix that has just begun has nothing to
        # catch up with: it starts at the beginning of the mix.
        position: float | None = 0.0 if self._total == 0 else None
        if self._recent:
            data = b"".join(self._recent)[-self._preroll :]
            sent = data[self._frame_start(data) :]
            queue.put_nowait(sent)
            position = self.position_of(self._total - len(sent))
        self._clients.add(queue)
        self._first_listener.set()
        return queue, position

    def unsubscribe(self, queue: Queue[bytes]) -> None:
        self._clients.discard(queue)

    def publish(self, chunk: bytes) -> None:
        self._recent.append(chunk)
        self._recent_size += len(chunk)
        self._total += len(chunk)
        self._marks.append((self._total, self.fed))
        while (
            len(self._recent) > 1
            and self._recent_size - len(self._recent[0]) >= self._preroll
        ):
            self._recent_size -= len(self._recent.popleft())
        while len(self._marks) > len(self._recent) + 1:
            self._marks.popleft()
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


def _parse_track_id(body: object) -> str:
    track_id = body.get("track_id") if isinstance(body, dict) else None
    if not isinstance(track_id, str) or not track_id:
        raise web.HTTPBadRequest(text='expected {"track_id": "..."}')
    return track_id


def _parse_track_ids(body: object) -> list[str]:
    ids = body.get("track_ids") if isinstance(body, dict) else None
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise web.HTTPBadRequest(text='expected {"track_ids": [...]}')
    return ids


def _parse_move(body: object) -> tuple[str, int]:
    position = body.get("position") if isinstance(body, dict) else None
    if not isinstance(position, int) or isinstance(position, bool):
        raise web.HTTPBadRequest(
            text='expected {"track_id": "...", "position": 0}'
        )
    return _parse_track_id(body), position


def _parse_language(body: object) -> str:
    language = body.get("language") if isinstance(body, dict) else None
    if language not in LANGUAGES:
        raise web.HTTPBadRequest(
            text=f'expected {{"language": one of {list(LANGUAGES)}}}'
        )
    return str(language)


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


def _add_language_api(app: web.Application, radio: Radio) -> None:
    """Add the endpoints of the language of the music (a station setting)."""

    def steerable() -> None:
        # The language is a setting of the account at the backend.
        if not radio.authorized:
            raise web.HTTPServiceUnavailable(text="not authorized yet")
        if not radio.language_info()["steered"]:
            raise web.HTTPConflict(text="the backend can't change the language")

    async def get_language(_: web.Request) -> web.Response:
        return web.json_response(radio.language_info())

    async def put_language(request: web.Request) -> web.Response:
        steerable()
        await radio.set_language(_parse_language(await _json_body(request)))
        return web.json_response(radio.language_info())

    async def delete_language(_: web.Request) -> web.Response:
        steerable()
        await radio.set_language(None)
        return web.json_response(radio.language_info())

    app.router.add_get("/api/language", get_language)
    app.router.add_put("/api/language", put_language)
    app.router.add_delete("/api/language", delete_language)


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

    async def pause(_: web.Request) -> web.Response:
        radio.pause()
        return web.json_response(radio.pause_info())

    async def resume(_: web.Request) -> web.Response:
        radio.resume()
        return web.json_response(radio.pause_info())

    app.router.add_post("/api/pause", pause)
    app.router.add_post("/api/resume", resume)
    app.router.add_post("/api/queue/refresh", refresh_queue)

    async def next_status(_: web.Request) -> web.Response:
        return web.json_response(radio.skip_info())

    app.router.add_get("/api/next", next_status)
    app.router.add_post("/api/next", next_track)


def _add_queue_edit_api(app: web.Application, radio: Radio) -> None:
    """Add the endpoints that change the queue by hand."""

    def changing(change: Callable[[], None]) -> web.Response:
        try:
            change()
        except UnknownQueueTrackError as err:
            raise web.HTTPNotFound(text=f"not in the queue: {err}") from None
        except (QueueChangeRefusedError, SkipRefusedError) as err:
            raise web.HTTPConflict(text=str(err)) from None
        return web.json_response(radio.queue())

    async def put_order(request: web.Request) -> web.Response:
        ids = _parse_track_ids(await _json_body(request))
        return changing(lambda: radio.queue_order(ids))

    async def move_track(request: web.Request) -> web.Response:
        track_id, position = _parse_move(await _json_body(request))
        return changing(lambda: radio.queue_move(track_id, position))

    async def remove_track(request: web.Request) -> web.Response:
        track_id = request.match_info["track_id"]
        return changing(lambda: radio.queue_remove(track_id))

    async def play_track(request: web.Request) -> web.Response:
        track_id = _parse_track_id(await _json_body(request))
        response = changing(lambda: radio.queue_play(track_id))
        response.set_status(202)  # the blend starts on a good beat
        return response

    app.router.add_put("/api/queue/order", put_order)
    app.router.add_post("/api/queue/move", move_track)
    app.router.add_delete("/api/queue/tracks/{track_id}", remove_track)
    app.router.add_post("/api/queue/play", play_track)


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
        radio.backends_changed()
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
    _add_queue_edit_api(app, radio)
    _add_language_api(app, radio)
    _add_genres_api(app, radio)
    _add_stats_api(app, radio)


def web_page_built() -> bool:
    """Say whether this build has the page (a backend-only one has not)."""
    return WEB_PAGE.is_file()


def _add_web_page(app: web.Application, *, serve: bool = True) -> None:
    """Add the page and its scripts, built by `npm run build` in frontend/.

    Without them (a backend-only build, or --no-web) the stream and the API
    are all there is, and the root says so.
    """

    async def no_page(_: web.Request) -> web.Response:
        return web.json_response(
            {"web": False, "stream": "/stream", "api": "/api/queue"},
            status=404,
        )

    if not serve or not web_page_built():
        app.router.add_get("/", no_page)
        return

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


def _stream_handler(
    hub: Broadcaster,
    content_type: str,
    *,
    titles: bool,
    starts: StartPositions | None = None,
) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
    """Make the handler that streams a hub to a listener."""

    async def stream(request: web.Request) -> web.StreamResponse:
        with_titles = titles and request.headers.get("Icy-MetaData") == "1"
        headers = {
            "Content-Type": content_type,
            "Cache-Control": "no-cache",
            "icy-name": ICY_NAME,
            # A page of another origin may analyze the audio: an <audio>
            # with crossorigin hands it to Web Audio only if this is sent.
            "Access-Control-Allow-Origin": "*",
        }
        if with_titles:
            headers["icy-metaint"] = str(ICY_METAINT)
        response = web.StreamResponse(headers=headers)
        queue, position = hub.subscribe_at()
        # The page says who it is in the address (?l=...) and asks for
        # where in the mix its player starts, to show the spectrum in time.
        name = request.query.get("l", "")
        if starts is not None and name and position is not None:
            starts.set(name, position)
        log.info("Listener connected (%s): %s", content_type, request.remote)
        pending = bytearray()
        sent_title = None
        try:
            await response.prepare(request)
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

    return stream


def _add_spectrum_api(
    app: web.Application, spectrum: SpectrumFeed, starts: StartPositions
) -> None:
    """Add GET /api/spectrum: the bands of the mix as server-sent events."""

    async def watch(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Access-Control-Allow-Origin": "*",
                # A proxy in front must not hold the events back.
                "X-Accel-Buffering": "no",
            }
        )
        await response.prepare(request)
        name = request.query.get("l", "")
        start = await starts.get(name, SPECTRUM_START_WAIT) if name else None
        queue, backlog = spectrum.subscribe(
            None if start is None else start - SPECTRUM_BACKLOG_MARGIN
        )
        try:
            await response.write(
                b"event: start\ndata: "
                + json.dumps({"position": start}).encode()
                + b"\n\n"
            )
            frames = backlog
            while True:
                if frames:
                    text = "".join(f"data: {f.to_json()}\n\n" for f in frames)
                    await response.write(text.encode())
                try:
                    async with asyncio.timeout(SPECTRUM_KEEPALIVE_SECONDS):
                        frames = await queue.get()
                except TimeoutError:
                    frames = []
                    await response.write(b": keepalive\n\n")
        except ConnectionError:
            pass
        finally:
            spectrum.unsubscribe(queue)
        return response

    app.router.add_get("/api/spectrum", watch)


def make_app(
    hub: Broadcaster,
    radio: Radio | None = None,
    backends: MultiBackend | None = None,
    lossless: Broadcaster | None = None,
    spectrum: SpectrumFeed | None = None,
    *,
    ogg: Broadcaster | None = None,
    page: bool = True,
) -> web.Application:
    app = web.Application()
    starts = StartPositions()
    app.router.add_get(
        "/stream",
        _stream_handler(hub, "audio/mpeg", titles=True, starts=starts),
    )
    if lossless is not None:
        app.router.add_get(
            "/stream.flac",
            _stream_handler(
                lossless, "audio/flac", titles=False, starts=starts
            ),
        )
    if ogg is not None:
        # The same FLAC in an Ogg container: Safari plays a live stream of
        # that, and doesn't play one of bare FLAC.
        app.router.add_get(
            "/stream.ogg",
            _stream_handler(ogg, "audio/ogg", titles=False, starts=starts),
        )
    if spectrum is not None:
        _add_spectrum_api(app, spectrum, starts)
    _add_web_page(app, serve=page)
    if radio is not None:
        add_api(app, radio, backends)
    return app


MP3_ARGS = ("-c:a", "libmp3lame", "-b:a", MP3_BITRATE, "-f", "mp3")
# 16 bit with a triangular dither: what the CD does with a finer mix.
FLAC_ARGS = (
    "-af",
    "aresample=dither_method=triangular",
    "-sample_fmt",
    "s16",
    "-c:a",
    "flac",
    "-f",
    "flac",
)


# The same FLAC in Ogg pages, which are made as often as the muxer allows.
OGG_ARGS = (
    "-af",
    "aresample=dither_method=triangular",
    "-sample_fmt",
    "s16",
    "-c:a",
    "flac",
    "-page_duration",
    "100000",
    "-f",
    "ogg",
)


async def _start_encoder(
    output_args: tuple[str, ...],
) -> asyncio.subprocess.Process:
    """Start ffmpeg: the mix in as raw float PCM, the encoded stream out."""
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
        *output_args,
        "-flush_packets",
        "1",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    if proc.stdin is None or proc.stdout is None:
        raise RuntimeError("ffmpeg has no pipes")
    return proc


async def _forward(stdout: asyncio.StreamReader, hub: Broadcaster) -> None:
    """Publish what an encoder writes; a FLAC stream's header is kept apart."""
    pending = b""
    if hub.header_length is not None:
        while (length := hub.header_length(pending)) is None:
            chunk = await stdout.read(4096)
            if not chunk:
                return
            pending += chunk
        hub.header, pending = pending[:length], pending[length:]
        if pending:
            hub.publish(pending)
    while chunk := await stdout.read(4096):
        hub.publish(chunk)


async def _start_encoders(
    hub: Broadcaster,
    lossless: Broadcaster | None,
    ogg: Broadcaster | None = None,
) -> list[tuple[asyncio.subprocess.Process, Broadcaster]]:
    wanted: list[tuple[tuple[str, ...], Broadcaster]] = [(MP3_ARGS, hub)]
    if lossless is not None:
        wanted.append((FLAC_ARGS, lossless))
    if ogg is not None:
        wanted.append((OGG_ARGS, ogg))
    encoders: list[tuple[asyncio.subprocess.Process, Broadcaster]] = []
    try:
        for args, stream in wanted:
            encoders.append((await _start_encoder(args), stream))
    except BaseException:
        await _stop_encoders(encoders)
        raise
    return encoders


async def _stop_encoders(
    encoders: list[tuple[asyncio.subprocess.Process, Broadcaster]],
) -> None:
    for proc, _ in encoders:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()


def _note_position(
    pcm: Pcm,
    analyzer: SpectrumAnalyzer,
    hubs: list[Broadcaster],
    spectrum: SpectrumFeed | None,
) -> None:
    """Tell the streams how far the mix is fed, and the page its bands."""
    frames = analyzer.feed(pcm)
    for stream in hubs:
        stream.fed = analyzer.position
    if spectrum is not None:
        spectrum.publish(frames)


async def _hold(radio: DeckSource, hubs: list[Broadcaster]) -> bool:
    """Wait while the mix is paused or nobody listens; say if it waited."""
    held = False
    while radio.user_paused or not any(h.has_listeners for h in hubs):
        if not held:
            radio.hold(held=True)
            held = True
        if radio.user_paused:
            await radio.wait_resumed()
        else:
            await hubs[0].wait_for_a_listener(*hubs[1:])
    if held:
        radio.hold(held=False)
    return held


async def _encode_loop(
    radio: DeckSource,
    hub: Broadcaster,
    effects: float,
    lossless: Broadcaster | None = None,
    spectrum: SpectrumFeed | None = None,
    *,
    ogg: Broadcaster | None = None,
) -> None:
    """Mix, encode and publish in real time while somebody listens."""
    hubs = [h for h in (hub, lossless, ogg) if h is not None]
    analyzer = SpectrumAnalyzer()
    # The positions in the mix start over with it.
    for stream in hubs:
        stream.reset_position()
    if spectrum is not None:
        spectrum.reset()
    encoders = await _start_encoders(hub, lossless, ogg)
    forwarders = [
        create_task(_forward(proc.stdout, target), name="dj-forward")
        for proc, target in encoders
        if proc.stdout is not None
    ]
    loop = asyncio.get_running_loop()
    started = loop.time()
    produced = 0.0
    try:
        async for pcm, title in mix(radio, effects):
            if await _hold(radio, hubs):
                started, produced = loop.time(), 0.0
            hub.title = title
            # Before the encoders get it: what they write next is of this.
            _note_position(pcm, analyzer, hubs, spectrum)
            data = np.ascontiguousarray(pcm).tobytes()
            stdins = [p.stdin for p, _ in encoders if p.stdin is not None]
            for stdin in stdins:
                stdin.write(data)
            for stdin in stdins:
                await stdin.drain()
            produced += len(pcm) / SAMPLE_RATE
            ahead = produced - (loop.time() - started) - PACING_LEAD_SECONDS
            if ahead > 0:
                await sleep(ahead)
    finally:
        for forwarder in forwarders:
            forwarder.cancel()
        await _stop_encoders(encoders)


async def encode_forever(
    radio: DeckSource,
    hub: Broadcaster,
    effects: float,
    lossless: Broadcaster | None = None,
    spectrum: SpectrumFeed | None = None,
    *,
    ogg: Broadcaster | None = None,
) -> None:
    """Keep the mix going: a failure ends only the mix it happened in."""
    while True:
        try:
            await _encode_loop(radio, hub, effects, lossless, spectrum, ogg=ogg)
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
    tracks_dir: Path | None = None,
    page: bool = True,
    ready: Event | None = None,
) -> None:
    """Serve the stream and the API, then mix until cancelled.

    The server is up before the app is authorized, so that /api/auth can
    tell where to do it. With a cache and a `tracks_dir`, the tracks loaded
    and waiting are kept there across restarts; without, they are in a
    directory that goes with the run. Without `page` the web page is not
    served, whether the build has it or not.
    """
    keep = cache is not None and tracks_dir is not None
    if keep and tracks_dir is not None:
        await asyncio.to_thread(tracks_dir.mkdir, parents=True, exist_ok=True)
    workdir_context = (
        contextlib.nullcontext(tracks_dir)
        if keep
        else tempfile.TemporaryDirectory(prefix="yamusic-dj-")
    )
    with workdir_context as workdir:
        radio = Radio(
            backend,
            Path(str(workdir)),
            station_id=station_id,
            pool_size=pool_size,
            mood_arc=mood_arc,
            cache=cache,
            keep_playlist=keep,
        )
        hub = Broadcaster()
        lossless = Broadcaster(
            bytes_per_second=FLAC_BYTES_PER_SECOND,
            frame_start=flac_start,
            header_length=flac_header_length,
            lag_seconds=FLAC_LAG_SECONDS,
        )
        ogg = Broadcaster(
            bytes_per_second=FLAC_BYTES_PER_SECOND,
            frame_start=ogg_start,
            header_length=ogg_header_length,
            lag_seconds=OGG_LAG_SECONDS,
        )
        spectrum = SpectrumFeed()
        runner = web.AppRunner(
            make_app(
                hub,
                radio,
                backend if isinstance(backend, MultiBackend) else None,
                lossless,
                spectrum,
                ogg=ogg,
                page=page,
            ),
            access_log=None,
        )
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        log.info(
            "Streaming at http://%s:%d/stream (MP3 %s), /stream.flac, "
            "/stream.ogg",
            host,
            port,
            MP3_BITRATE,
        )
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
            try:
                radio.restore_playlist()
            except Exception:
                log.exception("Could not take up the playlist of the last run")
            if mood_arc and radio.mood_arc_saved is None:
                await radio.start_mood_arc()
            log.info(
                "Preparing %d tracks from %s...", pool_size, radio.station_id
            )
            await _first_tracks(radio)
            refill = create_task(radio.keep_filled(), name="dj-refill")
            try:
                await encode_forever(
                    radio,
                    hub,
                    EFFECT_LEVELS[effects],
                    lossless,
                    spectrum,
                    ogg=ogg,
                )
            finally:
                refill.cancel()
                with contextlib.suppress(CancelledError):
                    await refill
        finally:
            # The mood is a setting of the account: leave it as it was.
            with contextlib.suppress(Exception):
                await radio.restore_settings()
            await runner.cleanup()
