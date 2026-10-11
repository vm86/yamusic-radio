"""What the radio needs from a music service, and nothing Yandex-specific.

The DJ (dj.py) only talks to a `MusicBackend`: it asks for batches of tracks
from a "station", downloads them to files, and tells the backend what was
heard. Yandex Music is one implementation (ya_player.py); another service
or a local collection only has to provide the same few calls, and
`create_backend()` in main.py is the one place that picks it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import Path


class Feedback(StrEnum):
    """What happened to a track, as the backend is told about it."""

    STARTED = "started"
    FINISHED = "finished"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class Track:
    """A track of a backend, ready to be downloaded.

    `track_id` is unique within the backend. `codec` (the file extension
    that ffmpeg is given) and `bitrate_in_kbps` describe what `download`
    will write and are part of the key of the analysis cache. `batch_id`
    is opaque: the backend gets it back with the feedback.
    """

    track_id: str
    title: str
    artists: tuple[str, ...]
    codec: str
    bitrate_in_kbps: int = 0
    genre: str = ""
    # The URL of the cover with %% where the size ("400x400") goes, or "".
    cover: str = ""
    batch_id: str = ""
    # The mood the backend knows the track by (one of dj_mix.ALL_MOODS), or
    # "" when it doesn't: the radio then judges by the sound of the track.
    mood: str = ""


class MusicBackend(Protocol):
    """A source of tracks for the radio."""

    @property
    def authorized(self) -> bool:
        """Whether the backend can give tracks (it has a token, say)."""
        ...

    @property
    def auth_url(self) -> str | None:
        """Where the user authorizes the app, while it isn't."""
        ...

    @property
    def auth_code(self) -> str | None:
        """The code to enter on that page."""
        ...

    @property
    def supports_moods(self) -> bool:
        """Whether `set_station_settings` can steer the mood of a station.

        Without it the radio finds the mood of each track by itself.
        """
        ...

    async def station_batch(
        self,
        station_id: str,
        *,
        exclude_track_ids: set[str],
    ) -> list[Track]:
        """Return the next tracks of the station, ready to be downloaded.

        Tracks of `exclude_track_ids` may be left out. May repeat tracks it
        gave before when it has nothing else.
        """
        ...

    async def download(self, track: Track, path: Path) -> None:
        """Write the audio of the track to `path`; raise if it can't."""
        ...

    async def feedback_track(
        self,
        track: Track,
        feedback: Feedback,
        station_id: str,
        total_played_seconds: float,
    ) -> None:
        """Tell the backend that the track was started, finished or skipped."""
        ...

    async def station_settings(self, station_id: str) -> tuple[str, str, str]:
        """Return the station's (mood_energy, diversity, language)."""
        ...

    async def set_station_settings(
        self,
        station_id: str,
        mood_energy: str,
        diversity: str,
        language: str,
    ) -> None:
        """Change the station's settings (only if `supports_moods`)."""
        ...

    async def aclose(self) -> None:
        """Stop what the backend runs in the background."""
        ...
