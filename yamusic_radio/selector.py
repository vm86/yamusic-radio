"""Choosing the music backend while the radio runs.

The radio is up before it is signed in anywhere. `BackendSelector` stands
in for the backend until one is chosen (by the command line, by the last
run, or by the user on the page), and then hands every call to it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from yamusic_radio.backend import AuthError, AuthPrompt

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from yamusic_radio.backend import Feedback, MusicBackend, Track

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BackendSpec:
    """A music backend that can be chosen."""

    id: str
    name: str
    create: Callable[[], MusicBackend]
    # Whether it has what it needs to play at once (a saved token, say):
    # the first such backend is taken when none was chosen.
    configured: Callable[[], bool] = lambda: False


class BackendSelector:
    """A `MusicBackend` that is whichever of the known ones was chosen."""

    def __init__(
        self,
        specs: Sequence[BackendSpec],
        *,
        initial: str | None = None,
        on_select: Callable[[str], None] | None = None,
    ) -> None:
        self._specs = {spec.id: spec for spec in specs}
        self._on_select = on_select
        self._current: MusicBackend | None = None
        self._selected: str | None = None
        if initial not in self._specs:
            initial = next((s.id for s in specs if s.configured()), None)
        if initial is not None:
            self._open(initial)

    def _open(self, backend_id: str) -> None:
        backend = self._specs[backend_id].create()
        self._current = backend
        self._selected = backend_id
        if not backend.authorized:
            backend.start_auth()
        log.info("Music backend: %s", self._specs[backend_id].name)

    @property
    def selected(self) -> str | None:
        return self._selected

    @property
    def choices(self) -> list[tuple[str, str]]:
        """The (id, name) of every backend that can be chosen."""
        return [(spec.id, spec.name) for spec in self._specs.values()]

    async def select(self, backend_id: str) -> None:
        """Choose the backend; only before one has been signed in to."""
        if backend_id not in self._specs:
            raise ValueError(f"unknown music backend {backend_id!r}")
        if backend_id == self._selected:
            return
        if self.authorized:
            raise AuthError("already signed in; restart to change the backend")
        if self._current is not None:
            await self._current.aclose()
        self._open(backend_id)
        if self._on_select is not None:
            self._on_select(backend_id)

    def _backend(self) -> MusicBackend:
        if self._current is None:
            raise RuntimeError("no music backend is chosen")
        return self._current

    @property
    def authorized(self) -> bool:
        return self._current is not None and self._current.authorized

    @property
    def auth_prompt(self) -> AuthPrompt:
        if self._current is None:
            return AuthPrompt()
        return self._current.auth_prompt

    @property
    def default_station(self) -> str:
        return self._current.default_station if self._current else ""

    @property
    def supports_moods(self) -> bool:
        return self._current is not None and self._current.supports_moods

    def start_auth(self) -> None:
        if self._current is not None:
            self._current.start_auth()

    async def authorize(self, values: dict[str, str]) -> None:
        await self._backend().authorize(values)

    async def station_batch(
        self, station_id: str, *, exclude_track_ids: set[str]
    ) -> list[Track]:
        return await self._backend().station_batch(
            station_id, exclude_track_ids=exclude_track_ids
        )

    async def download(self, track: Track, path: Path) -> None:
        await self._backend().download(track, path)

    async def feedback_track(
        self,
        track: Track,
        feedback: Feedback,
        station_id: str,
        total_played_seconds: float,
    ) -> None:
        await self._backend().feedback_track(
            track, feedback, station_id, total_played_seconds
        )

    async def station_settings(self, station_id: str) -> tuple[str, str, str]:
        return await self._backend().station_settings(station_id)

    async def set_station_settings(
        self,
        station_id: str,
        mood_energy: str,
        diversity: str,
        language: str,
    ) -> None:
        await self._backend().set_station_settings(
            station_id, mood_energy, diversity, language
        )

    async def aclose(self) -> None:
        if self._current is not None:
            await self._current.aclose()
