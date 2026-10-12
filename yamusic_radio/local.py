"""A music backend that plays the audio files of a folder.

The folder is chosen on the page (or with `--local-dir`) and kept in the
settings, so a restart plays from it again. Its tracks are taken at random,
a few at a time, and handed to the radio as files: linked, not copied, so a
library costs no disk. The tags (title, artist, genre) are read with ffmpeg
from the tracks that are asked for, never from the whole library.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import re
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

from yamusic_radio.backend import (
    AuthError,
    AuthField,
    AuthPrompt,
    BackendError,
    Track,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from yamusic_radio.backend import Feedback

log = logging.getLogger(__name__)

# What counts as music (the extension is also what ffmpeg is given).
AUDIO_SUFFIXES = frozenset(
    {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".aiff", ".wma"}
)
# The station that takes tracks from the whole folder; any other is a
# subfolder of it.
ALL_STATION = "shuffle"
BATCH_SIZE = 5
# The folder is looked through again when its list is this old.
RESCAN_SECONDS = 600.0
PROBES_AT_ONCE = 4
UNKNOWN_ARTIST = "Unknown artist"
DIRECTORY = "directory"

_ESCAPED = re.compile(r"\\(.)")


def scan(root: Path) -> dict[str, Path]:
    """Find the audio files under root, by an id made of where they are."""
    found: dict[str, Path] = {}
    for where, directories, names in os.walk(root):
        # What is hidden is not music (macOS leaves "._track.mp3" files).
        directories[:] = [d for d in directories if not d.startswith(".")]
        for name in names:
            path = Path(where, name)
            if (
                name.startswith(".")
                or path.suffix.lower() not in AUDIO_SUFFIXES
            ):
                continue
            found[track_id(path.relative_to(root))] = path
    return found


def track_id(relative: Path) -> str:
    """Make an id that stays the same for a file, and is safe as a name."""
    return hashlib.sha1(
        relative.as_posix().encode(), usedforsecurity=False
    ).hexdigest()[:16]


def parse_tags(text: str) -> dict[str, str]:
    """Read what `ffmpeg -f ffmetadata` writes: key=value, escaped."""
    tags: dict[str, str] = {}
    for line in text.splitlines()[1:]:
        key, found, value = line.partition("=")
        if found and not line.startswith(";"):
            tags[key.strip().lower()] = _ESCAPED.sub(r"\1", value).strip()
    return tags


def _folder(typed: str) -> Path | None:
    """Return the folder that was typed, None if it is not one."""
    directory = Path(typed).expanduser()
    return directory.resolve() if directory.is_dir() else None


def _link_or_copy(source: Path, path: Path) -> None:
    path.unlink(missing_ok=True)
    try:
        path.symlink_to(source)
    except OSError:
        shutil.copyfile(source, path)


class LocalBackend:
    """Plays the audio files under a directory."""

    supports_moods = False
    default_station = ALL_STATION

    def __init__(
        self,
        directory: Path | None = None,
        on_directory: Callable[[str], None] | None = None,
        *,
        batch_size: int = BATCH_SIZE,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._directory = directory
        self._on_directory = on_directory
        self._batch_size = batch_size
        self._rng = rng or random.Random()  # noqa: S311
        self._clock = clock
        self._files: dict[str, Path] | None = None
        self._scanned_at = 0.0
        self._changed = asyncio.Event()
        self._probes = asyncio.Semaphore(PROBES_AT_ONCE)

    # What is kept in the settings, and shown for changing.

    @property
    def settings(self) -> dict[str, str]:
        return {DIRECTORY: str(self._directory or "")}

    @property
    def setting_fields(self) -> tuple[AuthField, ...]:
        return (AuthField(DIRECTORY, "Folder with the music"),)

    # Signing in is choosing the folder.

    @property
    def authorized(self) -> bool:
        return self._directory is not None and self._directory.is_dir()

    @property
    def auth_prompt(self) -> AuthPrompt:
        return AuthPrompt(fields=self.setting_fields)

    async def wait_authorized(self) -> None:
        while not self.authorized:
            self._changed.clear()
            await self._changed.wait()

    def start_auth(self) -> None:
        """Nothing goes on by itself: the folder is typed in."""

    async def authorize(self, values: dict[str, str], /) -> None:
        """Choose the folder (again, too); keep it in the settings."""
        typed = values.get(DIRECTORY, "").strip()
        if not typed:
            raise AuthError("type the folder with the music")
        directory = await asyncio.to_thread(_folder, typed)
        if directory is None:
            raise AuthError(f"{typed} is not a folder")
        self._directory = directory
        self._files = None
        if self._on_directory is not None:
            self._on_directory(str(directory))
        self._changed.set()
        log.info("Local music folder: %s", directory)

    # The tracks.

    async def _library(self) -> dict[str, Path]:
        """List the files of the folder, again when the list is old."""
        directory = self._directory
        if directory is None:
            raise BackendError("no folder with music is chosen")
        stale = self._clock() - self._scanned_at >= RESCAN_SECONDS
        if self._files is None or stale:
            try:
                self._files = await asyncio.to_thread(scan, directory)
            except OSError as err:
                raise BackendError(f"cannot read {directory}: {err}") from err
            self._scanned_at = self._clock()
            log.info("%d audio files in %s", len(self._files), directory)
        return self._files

    async def _tags(self, path: Path) -> dict[str, str]:
        async with self._probes:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
                "-vn", "-an", "-f", "ffmetadata", "-",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )  # fmt: skip
            out, _ = await proc.communicate()
        return parse_tags(out.decode(errors="replace"))

    async def _track(self, root: Path, file_id: str, path: Path) -> Track:
        tags = await self._tags(path)
        artist = tags.get("album_artist") or tags.get("artist")
        folder = path.parent.name if path.parent != root else ""
        return Track(
            track_id=file_id,
            title=tags.get("title") or path.stem,
            artists=(artist or folder or UNKNOWN_ARTIST,),
            codec=path.suffix.lower().lstrip("."),
            genre=tags.get("genre", "").lower(),
        )

    async def station_batch(
        self,
        station_id: str,
        /,
        *,
        exclude_track_ids: set[str],
    ) -> list[Track]:
        """Return a few tracks at random, unheard ones if there are any."""
        files = await self._library()
        root = self._directory
        if root is None or not files:
            raise BackendError(f"no audio files in {root}")
        ids = list(files)
        if station_id not in ("", ALL_STATION):
            sub = Path(station_id)
            ids = [i for i in ids if files[i].is_relative_to(root / sub)]
            if not ids:
                raise BackendError(f"no audio files in {root / sub}")
        fresh = [i for i in ids if i not in exclude_track_ids]
        # Nothing new: the folder has been played through, so play it again.
        chosen = self._rng.sample(
            fresh or ids, min(self._batch_size, len(fresh or ids))
        )
        found = await asyncio.gather(
            *(self._track(root, i, files[i]) for i in chosen),
            return_exceptions=True,
        )
        tracks = [t for t in found if isinstance(t, Track)]
        if not tracks:
            raise BackendError("none of the tracks could be read")
        return tracks

    async def download(self, track: Track, path: Path, /) -> None:
        """Link the file to path (copy it where it can't be linked)."""
        files = await self._library()
        source = files.get(track.track_id)
        if source is None or not source.is_file():
            raise BackendError(f"{track.title} is not in the folder any more")
        try:
            await asyncio.to_thread(_link_or_copy, source, path)
        except OSError as err:
            raise BackendError(f"cannot read {source}: {err}") from err

    async def feedback_track(
        self,
        track: Track,
        feedback: Feedback,
        station_id: str,
        total_played_seconds: float,
        /,
    ) -> None:
        """Tell nobody: a folder has none to be told."""

    async def station_settings(
        self, _station_id: str, /
    ) -> tuple[str, str, str]:
        return "all", "default", "any"

    async def set_station_settings(
        self,
        station_id: str,
        mood_energy: str,
        diversity: str,
        language: str,
        /,
    ) -> None:
        """Change nothing: a folder has no settings (see supports_moods)."""

    async def aclose(self) -> None:
        """Nothing runs in the background."""
