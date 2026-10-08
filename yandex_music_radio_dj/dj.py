"""DJ radio: an endless beat-matched mix of "My Wave" over HTTP.

Before it starts streaming, the radio downloads a pool of tracks from the
station and analyzes their tempo. From then on it always picks the track
that fits the one playing best (tempo, loudness), speeds it up or slows it
down to the same tempo and blends it in over 16 beats. Meanwhile new tracks
keep arriving in the background. The result is encoded to MP3 and served as
an Icecast-style stream (with ICY titles) to any number of listeners.

Needs numpy and the ffmpeg binary.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import tempfile
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
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from aiohttp import ClientError, ClientSession, web

from yandex_music_radio_dj import dj_mix
from yandex_music_radio_dj.dj_mix import (
    CHANNELS,
    SAMPLE_RATE,
    Pcm,
    TrackAnalysis,
)
from yandex_music_radio_dj.request import retry_on_rate_limit

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from yandex_music_radio_dj.ya_player import ExtendTrack, YandexMusicPlayer

log = logging.getLogger(__name__)

DEFAULT_STATION = "user:onyourwave"
DEFAULT_PORT = 8000
DEFAULT_POOL_SIZE = 12
EFFECT_LEVELS = {"off": 0.0, "light": 0.6, "full": 1.0}
DEFAULT_EFFECTS = "light"

# Tracks downloaded and analyzed at the same time.
PREPARE_CONCURRENCY = 3
# Give up filling the pool after this many batches without a usable track:
# the station only repeats itself.
MAX_EMPTY_BATCHES = 5
# When no track in the pool fits the one playing, load this many more...
EXTRA_TRACKS = 3
# ...up to this many, and take the best there is after that.
MAX_EXTRA_TRACKS = 6
# While the station gives nothing new, look again this often.
REPLENISH_RETRY_SECONDS = 120
# Played tracks that can be repeated, oldest forgotten first.
MAX_PLAYED_REMEMBERED = 500
# The next track is chosen and decoded this long before the blend.
LOOKAHEAD_SECONDS = 40
# The mix is produced in pieces of this length.
CHUNK_SECONDS = 0.25
# How far ahead of the listeners the mix may run.
PACING_LEAD_SECONDS = 2.0

MP3_BITRATE = "192k"
ICY_METAINT = 16000
ICY_NAME = "Yandex Music DJ"
CLIENT_QUEUE_SIZE = 256


def is_available() -> bool:
    return shutil.which("ffmpeg") is not None


@dataclass(slots=True)
class Candidate:
    """A downloaded and analyzed track waiting for its turn."""

    title: str
    path: Path
    analysis: TrackAnalysis
    artists: frozenset[str] = frozenset()
    track: ExtendTrack | None = None
    # The station mood the track was fetched with.
    mood: str = ""


@dataclass(slots=True)
class Played:
    """What is kept of a track once it has been played, to repeat it."""

    track: ExtendTrack
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
    track: ExtendTrack | None = None


async def decode_file(path: Path, speed: float = 1.0) -> Pcm:
    """Decode to float32 44.1 kHz stereo, optionally sped up or slowed."""
    command = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path)]
    if speed != 1.0:
        command += ["-af", f"atempo={speed:.5f}"]
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
        player: YandexMusicPlayer,
        session: ClientSession,
        workdir: Path,
        *,
        station_id: str = DEFAULT_STATION,
        pool_size: int = DEFAULT_POOL_SIZE,
        mood_arc: bool = False,
    ) -> None:
        self._player = player
        self._session = session
        self._workdir = workdir
        self._station_id = station_id
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
        self._mood = ""
        self._saved_settings: tuple[str, str, str] | None = None
        self._playing_id = ""
        self._seen: set[str] = set()
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

    async def start_mood_arc(self) -> None:
        """Remember the station's settings, to put them back at the end."""
        self._saved_settings = await self._player.station_settings(
            self._station_id
        )
        self._mood = self._saved_settings[0]

    async def restore_settings(self) -> None:
        """Put the station's settings back as they were."""
        if (
            self._saved_settings is None
            or self._mood == self._saved_settings[0]
        ):
            return
        await self._player.set_station_settings(
            self._station_id, *self._saved_settings
        )
        self._mood = self._saved_settings[0]

    async def _set_mood(self, mood: str) -> None:
        if self._saved_settings is None or mood == self._mood:
            return
        _, diversity, language = self._saved_settings
        await self._player.set_station_settings(
            self._station_id, mood, diversity, language
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
            if if_empty and self._pool:
                return 1
            before = len(self._pool)
            empty_batches = 0
            if self._mood_arc:
                # Tracks loaded now are played about when the pool has been
                # worked through: aim for the mood wanted then.
                mood = mood or dj_mix.mood_for(self._position + len(self._pool))
                await self._set_mood(mood)
            else:
                mood = ""
            while (wanted := count - (len(self._pool) - before)) > 0:
                batch = await self._player.station_batch(
                    self._station_id, exclude_track_ids=self._seen
                )
                fresh = [t for t in batch if t.track_id not in self._seen]
                fresh = fresh[:wanted]
                self._seen.update(t.track_id for t in fresh)
                added = len(self._pool)
                await gather(*(self._prepare(t, mood) for t in fresh))
                if len(self._pool) == added:
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
            if self._mood_arc:
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

    async def _prepare(self, track: ExtendTrack, mood: str = "") -> None:
        async with self._slots:
            title = f"{', '.join(track.artists_name())} - {track.title}"
            path = self._workdir / f"{track.track_id}.{track.codec}"
            try:
                await self._download(track, path)
                pcm = await decode_file(path)
                analysis = await asyncio.to_thread(dj_mix.analyze, pcm)
            except CancelledError:
                raise
            except Exception:
                log.exception("Skip %s", title)
                path.unlink(missing_ok=True)
                return
            del pcm
        log.info(
            "Ready: %s (%.1f BPM, %.1f dB)",
            title,
            analysis.bpm,
            analysis.loudness_db,
        )
        self._pool.append(
            Candidate(
                title,
                path,
                analysis,
                frozenset(track.artists_name()),
                track,
                mood,
            )
        )
        self._added.set()

    async def _download(self, track: ExtendTrack, path: Path) -> None:
        links = await self._player.get_download_links(
            track.track_id, track.codec, track.bitrate_in_kbps
        )
        if not links:
            raise RuntimeError("no download links")

        async def request() -> None:
            with path.open("wb") as out:
                async with self._session.get(links[0]) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.content.iter_chunked(1 << 16):
                        out.write(chunk)

        try:
            await retry_on_rate_limit(request)
        except ClientError as err:
            raise RuntimeError(f"download failed: {err!r}") from err

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
            )
        raise RuntimeError("No tracks to play")

    def _mark_played(self, candidate: Candidate) -> None:
        self._history.append(candidate.artists)
        self._position += 1
        track = candidate.track
        if track is None:
            return
        self._playing_id = track.track_id
        self._played[track.track_id] = Played(
            track, candidate.title, candidate.analysis, candidate.artists
        )
        self._played.move_to_end(track.track_id)
        while len(self._played) > MAX_PLAYED_REMEMBERED:
            self._played.popitem(last=False)

    async def next_deck(self, current: TrackAnalysis | None) -> Deck:
        """Choose, tempo-match and decode the track to play after current."""
        candidate = await self._pick(current)
        self._mark_played(candidate)
        self._consumed.set()

        speed = 0.0
        if current is not None:
            speed = dj_mix.transition_speed(current.bpm, candidate.analysis)
        try:
            pcm = await decode_file(candidate.path, speed or 1.0)
        finally:
            candidate.path.unlink(missing_ok=True)
        analysis = dj_mix.stretched(candidate.analysis, speed or 1.0)
        log.info(
            "Next: %s (%.1f BPM%s)",
            candidate.title,
            analysis.bpm,
            ", beat-matched" if speed else "",
        )
        return Deck(
            candidate.title,
            pcm,
            analysis,
            beat_matched=bool(speed),
            track=candidate.track,
        )

    def started(self, deck: Deck) -> None:
        """Tell the station the track has begun to be heard."""
        self._send_feedback(deck, "trackStarted", 0.0)

    def finished(self, deck: Deck) -> None:
        """Tell the station the track has been heard to the end."""
        seconds = (deck.analysis.end - deck.analysis.start) / SAMPLE_RATE
        self._send_feedback(deck, "trackFinished", seconds)

    def _send_feedback(self, deck: Deck, kind: str, seconds: float) -> None:
        track = deck.track
        if track is None or not track.batch_id:
            return
        task = create_task(
            self._player.feedback_track(
                track.track_id,
                kind,
                self._station_id,
                track.batch_id,
                seconds,
            )
        )
        self._feedback_tasks.add(task)
        task.add_done_callback(self._feedback_tasks.discard)


type Piece = tuple[Pcm, str]


def _pieces(pcm: Pcm, title: str) -> list[Piece]:
    size = round(CHUNK_SECONDS * SAMPLE_RATE)
    return [(pcm[i : i + size], title) for i in range(0, len(pcm), size)]


async def mix(radio: Radio, effects: float = 0.0) -> AsyncIterator[Piece]:
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
            # knowing the next track.
            solo_end = max(position, current.analysis.end - lookahead)
            for piece in _pieces(current.pcm[position:solo_end], current.title):
                yield piece
            position = solo_end

            following = await following_task
            blend_start, following_start, length = dj_mix.plan_blend(
                current.analysis,
                following.analysis,
                beat_matched=following.beat_matched,
            )
            blend_start = max(blend_start, position)
            length = max(
                0,
                min(
                    length,
                    current.analysis.end - blend_start,
                    len(following.pcm) - following_start,
                ),
            )

            for piece in _pieces(
                current.pcm[position:blend_start], current.title
            ):
                yield piece
            kind = dj_mix.choose_transition(
                current.analysis,
                following.analysis,
                beat_matched=following.beat_matched,
            )
            log.info("Transition: %s", kind.value)
            # The filtering takes a while, don't stall the event loop with it.
            blended = await asyncio.to_thread(
                dj_mix.blend,
                kind,
                current.pcm[blend_start : blend_start + length],
                following.pcm[following_start : following_start + length],
                current.analysis.beat_period if following.beat_matched else 0,
                effects=effects,
            )
            radio.started(following)
            half = length // 2
            for piece in _pieces(blended[:half], current.title):
                yield piece
            for piece in _pieces(blended[half:], following.title):
                yield piece

            radio.finished(current)
            position = following_start + length
            current = following
    finally:
        if following_task is not None:
            following_task.cancel()


class Broadcaster:
    """Fans the encoded stream out to the connected listeners."""

    def __init__(self) -> None:
        self.title = ""
        self._clients: set[Queue[bytes]] = set()
        self._first_listener = Event()

    @property
    def has_listeners(self) -> bool:
        return bool(self._clients)

    async def wait_for_listeners(self) -> None:
        while not self._clients:
            self._first_listener.clear()
            await self._first_listener.wait()

    def subscribe(self) -> Queue[bytes]:
        queue: Queue[bytes] = Queue(CLIENT_QUEUE_SIZE)
        self._clients.add(queue)
        self._first_listener.set()
        return queue

    def unsubscribe(self, queue: Queue[bytes]) -> None:
        self._clients.discard(queue)

    def publish(self, chunk: bytes) -> None:
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


def make_app(hub: Broadcaster) -> web.Application:
    async def index(_: web.Request) -> web.Response:
        return web.Response(
            text=f"{ICY_NAME}: stream is at /stream\nNow playing: {hub.title}\n"
        )

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
    app.router.add_get("/", index)
    app.router.add_get("/stream", stream)
    return app


async def _encode_loop(radio: Radio, hub: Broadcaster, effects: float) -> None:
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
    assert proc.stdin is not None  # noqa: S101
    assert proc.stdout is not None  # noqa: S101
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


async def serve(
    player: YandexMusicPlayer,
    session: ClientSession,
    *,
    host: str,
    port: int,
    station_id: str = DEFAULT_STATION,
    pool_size: int = DEFAULT_POOL_SIZE,
    effects: str = DEFAULT_EFFECTS,
    mood_arc: bool = False,
    ready: Event | None = None,
) -> None:
    """Prepare the first tracks, then stream until cancelled."""
    # Waiting for the user to authorize the app.
    while not player.is_init:  # noqa: ASYNC110
        await sleep(2)

    with tempfile.TemporaryDirectory(prefix="yamusic-dj-") as workdir:
        radio = Radio(
            player,
            session,
            Path(workdir),
            station_id=station_id,
            pool_size=pool_size,
            mood_arc=mood_arc,
        )
        try:
            if mood_arc:
                await radio.start_mood_arc()
            log.info("Preparing %d tracks from %s...", pool_size, station_id)
            await radio.fill()
            if not radio.pool:
                raise RuntimeError("The station gave no playable tracks")
            await _stream(radio, host, port, effects, ready)
        finally:
            # The mood is a setting of the account: leave it as it was.
            with contextlib.suppress(Exception):
                await radio.restore_settings()


async def _stream(
    radio: Radio,
    host: str,
    port: int,
    effects: str,
    ready: Event | None,
) -> None:
    hub = Broadcaster()
    runner = web.AppRunner(make_app(hub), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    log.info("Streaming at http://%s:%d/stream", host, port)
    if ready is not None:
        ready.set()

    refill = create_task(radio.keep_filled(), name="dj-refill")
    try:
        await _encode_loop(radio, hub, EFFECT_LEVELS[effects])
    finally:
        refill.cancel()
        with contextlib.suppress(CancelledError):
            await refill
        await runner.cleanup()
