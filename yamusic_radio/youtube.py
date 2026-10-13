"""A music backend that plays YouTube Music.

`ytmusicapi` finds the tracks (a radio from a song, a playlist, a search,
the liked songs) and `yt-dlp` downloads their audio: ytmusicapi tells about
the tracks but does not give their audio. Both are optional
(`pip install ".[youtube]"`) and are imported when they are first needed, so
that the radio runs without them.

A station is one of:

- `charts` or `charts:XX` (XX is a country code): the tracks of the top
  playlist of YouTube Music, the global one by default;
- `radio:VIDEO_ID`: the radio of a song;
- `playlist:PLAYLIST_ID`: the tracks of a playlist;
- `search:WORDS`: the songs found for the words;
- `liked`: the liked songs of the account (needs `--youtube-auth`).

Signing in is not needed for the first four. A file with the request headers
of a browser session (`ytmusicapi browser`) gives `liked`, and the music of
the account.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import TYPE_CHECKING, Any

from yamusic_radio.backend import AuthError, AuthPrompt, BackendError, Track

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from yamusic_radio.backend import Feedback

log = logging.getLogger(__name__)

DEFAULT_STATION = "charts"
BATCH_SIZE = 5
# The list of a station is asked for again when it is this old (a radio is
# asked for every time: it gives other songs each time).
REFRESH_SECONDS = 600.0
# Mixes, sets and audiobooks come up in a search: they are not played.
MAX_TRACK_SECONDS = 15 * 60
MIN_TRACK_SECONDS = 30
# h:mm:ss at most.
MAX_LENGTH_PARTS = 3
# The file the audio is written to has this extension; ffmpeg reads the
# content, not the name, so whatever the audio really is, it is decoded.
CODEC = "m4a"
AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio"
# YouTube wants a JavaScript runtime to give some of the audio; yt-dlp takes
# the first of these that is installed.
JS_RUNTIMES: dict[str, dict[str, str]] = {"deno": {}, "node": {}, "bun": {}}
INSTALL_HINT = 'install them with: pip install ".[youtube]"'


# The client of ytmusicapi (`YTMusic`); the calls the backend makes are
# get_watch_playlist, get_playlist, get_charts, search and get_liked_songs.
YouTubeClient = Any


def make_client(auth: Path | None) -> YouTubeClient:
    """Make the ytmusicapi client, signed in with the headers file if any."""
    try:
        from ytmusicapi import YTMusic  # noqa: PLC0415
    except ImportError as err:
        raise BackendError(
            f"ytmusicapi is not installed: {INSTALL_HINT}"
        ) from err
    return YTMusic(None if auth is None else str(auth))


def download_audio(video_id: str, path: Path) -> None:
    """Download the best audio of a video to `path` with yt-dlp."""
    try:
        import yt_dlp  # noqa: PLC0415
    except ImportError as err:
        raise BackendError(f"yt-dlp is not installed: {INSTALL_HINT}") from err
    path.parent.mkdir(parents=True, exist_ok=True)
    options: dict[str, Any] = {
        "format": AUDIO_FORMAT,
        # The name is the path as it is: no % of it is a template.
        "outtmpl": str(path).replace("%", "%%"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "overwrites": True,
        "retries": 3,
        "js_runtimes": JS_RUNTIMES,
    }
    url = f"https://music.youtube.com/watch?v={video_id}"
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([url])
    except yt_dlp.utils.YoutubeDLError as err:
        raise BackendError(f"cannot download {video_id}: {err}") from err


def parse_length(text: object) -> int:
    """Read "3:07" or "1:02:03" as seconds; 0 when it isn't one."""
    if not isinstance(text, str):
        return 0
    parts = text.strip().split(":")
    if len(parts) > MAX_LENGTH_PARTS or not all(p.isdigit() for p in parts):
        return 0
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds


def seconds_of(item: dict[str, Any]) -> int:
    """Return the length of a track item, 0 when it isn't told."""
    found = item.get("duration_seconds")
    if isinstance(found, int):
        return found
    return parse_length(item.get("length") or item.get("duration"))


def track_of(item: dict[str, Any]) -> Track | None:
    """Make a track of an item of ytmusicapi, None if it can't be played."""
    video_id = item.get("videoId")
    title = item.get("title")
    if not isinstance(video_id, str) or not isinstance(title, str):
        return None
    seconds = seconds_of(item)
    if seconds and not MIN_TRACK_SECONDS <= seconds <= MAX_TRACK_SECONDS:
        return None
    artists = tuple(
        a["name"]
        for a in item.get("artists") or ()
        if isinstance(a, dict) and isinstance(a.get("name"), str)
    )
    thumbnails = item.get("thumbnails") or item.get("thumbnail") or ()
    cover = ""
    if thumbnails and isinstance(thumbnails[-1], dict):
        cover = str(thumbnails[-1].get("url") or "")
    return Track(
        track_id=video_id,
        title=title,
        artists=artists or ("Unknown artist",),
        codec=CODEC,
        cover=cover,
    )


class YouTubeMusicBackend:
    """Plays what YouTube Music finds, downloaded with yt-dlp."""

    supports_moods = False
    default_station = DEFAULT_STATION

    def __init__(
        self,
        auth: Path | None = None,
        *,
        client: Callable[[Path | None], YouTubeClient] = make_client,
        download: Callable[[str, Path], None] = download_audio,
        batch_size: int = BATCH_SIZE,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._auth = auth
        self._make_client = client
        self._download = download
        self._batch_size = batch_size
        self._rng = rng or random.Random()  # noqa: S311
        self._clock = clock
        self._client: YouTubeClient | None = None
        # station -> (when it was asked for, its tracks)
        self._lists: dict[str, tuple[float, list[Track]]] = {}

    # Nothing to sign in to: the public part works as it is.

    @property
    def authorized(self) -> bool:
        return True

    @property
    def auth_prompt(self) -> AuthPrompt:
        return AuthPrompt()

    async def wait_authorized(self) -> None:
        """Return at once: the backend needs no sign-in."""

    def start_auth(self) -> None:
        """Nothing goes on by itself."""

    async def authorize(self, values: dict[str, str], /) -> None:  # noqa: ARG002
        """Sign in: not a thing here, the headers file is a command line."""
        raise AuthError("YouTube Music is signed in with --youtube-auth")

    # The tracks.

    def _ytmusic(self) -> YouTubeClient:
        if self._client is None:
            self._client = self._make_client(self._auth)
        return self._client

    def _fetch(self, station: str) -> list[Track]:
        """Ask YouTube Music for the tracks of a station (blocks)."""
        kind, _, argument = station.partition(":")
        client = self._ytmusic()
        items: list[dict[str, Any]]
        if kind == "radio" and argument:
            watch = client.get_watch_playlist(
                videoId=argument, radio=True, limit=50
            )
            items = list(watch.get("tracks") or ())
        elif kind == "playlist" and argument:
            items = list(
                client.get_playlist(playlistId=argument, limit=200).get(
                    "tracks"
                )
                or ()
            )
        elif kind == "search" and argument:
            items = client.search(argument, filter="songs", limit=40)
        elif kind == "liked":
            if self._auth is None:
                raise BackendError("liked songs need --youtube-auth")
            items = list(client.get_liked_songs(limit=200).get("tracks") or ())
        elif kind == "charts":
            items = self._chart(client, argument or "ZZ")
        else:
            raise BackendError(f"unknown YouTube Music station {station!r}")
        tracks = [t for item in items if (t := track_of(item)) is not None]
        if not tracks:
            raise BackendError(f"no tracks in the station {station!r}")
        return tracks

    @staticmethod
    def _chart(client: YouTubeClient, country: str) -> list[dict[str, Any]]:
        """List the tracks of the first chart playlist of a country."""
        charts = client.get_charts(country)
        videos = charts.get("videos")
        entries = videos.get("items") if isinstance(videos, dict) else videos
        for entry in entries or ():
            playlist_id = entry.get("playlistId") if entry else None
            if isinstance(playlist_id, str):
                found = client.get_playlist(
                    playlistId=playlist_id, limit=200
                ).get("tracks")
                return list(found or ())
        raise BackendError(f"no chart for {country}")

    async def _tracks(self, station: str) -> list[Track]:
        asked, found = self._lists.get(station, (0.0, []))
        # A radio gives other songs every time it is asked.
        current = bool(found) and self._clock() - asked < REFRESH_SECONDS
        if station.startswith("radio:") or not current:
            try:
                found = await asyncio.to_thread(self._fetch, station)
            except BackendError:
                raise
            except Exception as err:
                raise BackendError(
                    f"YouTube Music failed for {station!r}: {err}"
                ) from err
            self._lists[station] = (self._clock(), found)
        return found

    async def station_batch(
        self,
        station_id: str,
        /,
        *,
        exclude_track_ids: set[str],
    ) -> list[Track]:
        """Return a few tracks of the station at random, unheard ones first."""
        station = station_id or DEFAULT_STATION
        found = await self._tracks(station)
        fresh = [t for t in found if t.track_id not in exclude_track_ids]
        # Nothing new: the list has been played through, so play it again.
        pool = fresh or found
        return self._rng.sample(pool, min(self._batch_size, len(pool)))

    def _download_checked(self, track: Track, path: Path) -> None:
        """Download the audio and see that a file is there (blocks)."""
        self._download(track.track_id, path)
        if not path.is_file() or path.stat().st_size == 0:
            raise BackendError(f"{track.title} was not downloaded")

    async def download(self, track: Track, path: Path, /) -> None:
        """Download the audio of the track to path with yt-dlp."""
        try:
            await asyncio.to_thread(self._download_checked, track, path)
        except BackendError:
            raise
        except Exception as err:
            raise BackendError(f"cannot download {track.title}: {err}") from err

    async def feedback_track(
        self,
        track: Track,
        feedback: Feedback,
        station_id: str,
        total_played_seconds: float,
        /,
    ) -> None:
        """Tell nobody: the radio does not touch the history of the account."""

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
        """Change nothing: no settings (see supports_moods)."""

    async def aclose(self) -> None:
        """Nothing runs in the background."""
