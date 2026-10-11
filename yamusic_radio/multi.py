"""Playing from several music backends at once.

`MultiBackend` is a `MusicBackend` made of the backends that were turned
on. The radio asks it for batches of tracks and gets them from each backend
in turn, so the pool holds tracks of all of them and the usual choice of
the next track picks the best fit among them. A backend that fails is left
alone for a while (longer each time) and the others carry on.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from yamusic_radio.backend import AuthError, AuthPrompt, BackendError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from yamusic_radio.backend import Feedback, MusicBackend, Track

log = logging.getLogger(__name__)

# A backend that failed this many times in a row is left alone...
FAILURES_TO_REST = 2
# ...for this long, doubling with each failure after that, up to the limit.
REST_SECONDS = 30.0
MAX_REST_SECONDS = 600.0


@dataclass(frozen=True, slots=True)
class BackendSpec:
    """A music backend that can be turned on."""

    id: str
    name: str
    create: Callable[[], MusicBackend]
    # Whether it has what it needs to play at once (a saved token, say):
    # such backends are turned on without being asked.
    configured: Callable[[], bool] = lambda: False
    # The ids of its tracks are used as they are, not with the backend's id
    # in front (for the backend that was there first: what the cache
    # remembers of its tracks stays valid).
    plain_ids: bool = False


@dataclass(slots=True)
class _Running:
    spec: BackendSpec
    backend: MusicBackend
    station: str
    failures: int = 0
    rest_until: float = 0.0


class MultiBackend:
    """A `MusicBackend` that is all of the backends turned on."""

    def __init__(
        self,
        specs: Sequence[BackendSpec],
        *,
        enabled: Sequence[str] = (),
        stations: Mapping[str, str] | None = None,
        on_change: Callable[[list[str]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._specs = {spec.id: spec for spec in specs}
        self._stations = dict(stations or {})
        self._on_change = on_change
        self._clock = clock
        self._running: dict[str, _Running] = {}
        self._last = ""
        # Set when a backend is turned on or off.
        self._changed_event = asyncio.Event()
        for spec in specs:
            if spec.id in enabled or spec.configured():
                self._start(spec.id)

    def _start(self, backend_id: str) -> None:
        spec = self._specs[backend_id]
        backend = spec.create()
        station = self._stations.get(backend_id) or backend.default_station
        self._running[backend_id] = _Running(spec, backend, station)
        if not backend.authorized:
            backend.start_auth()
        self._changed_event.set()
        log.info("Music backend on: %s", spec.name)

    def _changed(self) -> None:
        if self._on_change is not None:
            self._on_change(list(self._running))

    async def enable(self, backend_id: str) -> None:
        """Turn the backend on, and start signing in if it needs that."""
        if backend_id not in self._specs:
            raise ValueError(f"unknown music backend {backend_id!r}")
        if backend_id in self._running:
            return
        self._start(backend_id)
        self._changed()

    async def disable(self, backend_id: str) -> None:
        """Turn the backend off. Tracks it gave already stay in the pool."""
        if backend_id not in self._specs:
            raise ValueError(f"unknown music backend {backend_id!r}")
        running = self._running.pop(backend_id, None)
        if running is None:
            return
        await running.backend.aclose()
        self._changed_event.set()
        self._changed()
        log.info("Music backend off: %s", running.spec.name)

    def status(self) -> list[dict[str, object]]:
        """Describe every backend: for the page that turns them on and off."""
        now = self._clock()
        result: list[dict[str, object]] = []
        for spec in self._specs.values():
            running = self._running.get(spec.id)
            info: dict[str, object] = {
                "id": spec.id,
                "name": spec.name,
                "enabled": running is not None,
                "authorized": False,
                "available": False,
                "url": None,
                "code": None,
                "fields": [],
            }
            if running is not None:
                authorized = running.backend.authorized
                prompt = running.backend.auth_prompt
                info["authorized"] = authorized
                info["available"] = authorized and now >= running.rest_until
                if not authorized:
                    info["url"] = prompt.url
                    info["code"] = prompt.code
                    info["fields"] = [
                        dataclasses.asdict(f) for f in prompt.fields
                    ]
            result.append(info)
        return result

    # Which backends can give tracks, and the failures that take them out.

    def _usable(self) -> list[_Running]:
        """List the backends to ask, the next one in turn first."""
        now = self._clock()
        signed_in = [r for r in self._running.values() if r.backend.authorized]
        rested = [r for r in signed_in if now >= r.rest_until]
        # All are resting: try the one that rests the shortest, rather than
        # sit idle.
        chosen = rested or sorted(signed_in, key=lambda r: r.rest_until)[:1]
        ids = [r.spec.id for r in chosen]
        if self._last in ids:
            turn = ids.index(self._last) + 1
            chosen = chosen[turn:] + chosen[:turn]
        return chosen

    def _failed(self, running: _Running, err: Exception) -> None:
        running.failures += 1
        log.warning(
            "%s failed (%d in a row): %r",
            running.spec.name,
            running.failures,
            err,
        )
        if running.failures >= FAILURES_TO_REST:
            extra = running.failures - FAILURES_TO_REST
            rest = min(REST_SECONDS * 2**extra, MAX_REST_SECONDS)
            running.rest_until = self._clock() + rest
            log.warning("%s rests for %.0f s", running.spec.name, rest)

    @staticmethod
    def _worked(running: _Running) -> None:
        if running.failures:
            log.info("%s works again", running.spec.name)
        running.failures = 0
        running.rest_until = 0.0

    def _owner(self, track: Track) -> _Running:
        running = self._running.get(track.source)
        if running is None:
            raise RuntimeError(f"the backend of {track.track_id} is off")
        return running

    @staticmethod
    def _native(track: Track) -> Track:
        """Give the track back the way its backend made it."""
        return dataclasses.replace(
            track,
            track_id=track.native_id or track.track_id,
            source="",
            native_id="",
        )

    # The `MusicBackend` calls.

    @property
    def authorized(self) -> bool:
        return any(r.backend.authorized for r in self._running.values())

    @property
    def auth_prompt(self) -> AuthPrompt:
        for running in self._running.values():
            if not running.backend.authorized:
                return running.backend.auth_prompt
        return AuthPrompt()

    @property
    def default_station(self) -> str:
        return ", ".join(
            f"{r.spec.id}: {r.station}" for r in self._running.values()
        )

    @property
    def supports_moods(self) -> bool:
        return any(self._steering())

    def _steering(self) -> list[_Running]:
        return [
            r
            for r in self._running.values()
            if r.backend.authorized and r.backend.supports_moods
        ]

    def start_auth(self) -> None:
        for running in self._running.values():
            if not running.backend.authorized:
                running.backend.start_auth()

    async def wait_authorized(self) -> None:
        while not self.authorized:
            self._changed_event.clear()
            waits: list[asyncio.Task[object]] = [
                asyncio.create_task(r.backend.wait_authorized())
                for r in self._running.values()
            ]
            waits.append(asyncio.create_task(self._changed_event.wait()))
            try:
                await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for wait in waits:
                    wait.cancel()
                await asyncio.gather(*waits, return_exceptions=True)

    async def authorize(self, values: dict[str, str], /) -> None:
        for running in self._running.values():
            if not running.backend.authorized:
                await running.backend.authorize(values)
                return
        raise AuthError("nothing to sign in to")

    async def authorize_backend(
        self, backend_id: str, values: dict[str, str]
    ) -> None:
        """Sign in to one of the backends that are on."""
        running = self._running.get(backend_id)
        if running is None:
            raise AuthError(f"music backend {backend_id!r} is not on")
        await running.backend.authorize(values)

    async def station_batch(
        self,
        _station_id: str,
        /,
        *,
        exclude_track_ids: set[str],
    ) -> list[Track]:
        """Return a batch from the next backend that can give one."""
        last_error: Exception | None = None
        for running in self._usable():
            try:
                batch = await running.backend.station_batch(
                    running.station, exclude_track_ids=exclude_track_ids
                )
            except BackendError as err:
                self._failed(running, err)
                last_error = err
                continue
            self._worked(running)
            self._last = running.spec.id
            return [self._stamp(running, track) for track in batch]
        raise RuntimeError("no music backend can give tracks") from last_error

    @staticmethod
    def _stamp(running: _Running, track: Track) -> Track:
        track_id = (
            track.track_id
            if running.spec.plain_ids
            else f"{running.spec.id}:{track.track_id}"
        )
        return dataclasses.replace(
            track,
            track_id=track_id,
            native_id=track.track_id,
            source=running.spec.id,
        )

    async def download(self, track: Track, path: Path) -> None:
        running = self._owner(track)
        try:
            await running.backend.download(self._native(track), path)
        except BackendError as err:
            self._failed(running, err)
            raise
        self._worked(running)

    async def feedback_track(
        self,
        track: Track,
        feedback: Feedback,
        _station_id: str,
        total_played_seconds: float,
        /,
    ) -> None:
        running = self._owner(track)
        await running.backend.feedback_track(
            self._native(track),
            feedback,
            running.station,
            total_played_seconds,
        )

    async def station_settings(
        self, _station_id: str, /
    ) -> tuple[str, str, str]:
        steering = self._steering()
        if not steering:
            return "all", "default", "any"
        return await steering[0].backend.station_settings(steering[0].station)

    async def set_station_settings(
        self,
        _station_id: str,
        mood_energy: str,
        diversity: str,
        language: str,
        /,
    ) -> None:
        errors: list[Exception] = []
        steering = self._steering()
        for running in steering:
            try:
                await running.backend.set_station_settings(
                    running.station, mood_energy, diversity, language
                )
            except BackendError as err:
                log.warning("%s: no new station settings", running.spec.name)
                errors.append(err)
        if steering and len(errors) == len(steering):
            raise errors[0]

    async def aclose(self) -> None:
        for running in self._running.values():
            await running.backend.aclose()
