from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from asyncio import CancelledError, Event, Semaphore, Task, gather, sleep
from asyncio.tasks import create_task
from contextlib import suppress
from typing import TYPE_CHECKING, Any, ClassVar

from aiohttp import ClientError
from yandex_music import (
    ClientAsync,
    DeviceCode,
    Track,
    YandexMusicObject,
)
from yandex_music.exceptions import YandexMusicError
from yandex_music.utils import model
from yandex_music.utils.sign_request import (
    DEFAULT_SIGN_KEY,
)

from yamusic_radio import backend
from yamusic_radio.request import (
    ClientRequest,
    retry_on_rate_limit,
    where,
)

if TYPE_CHECKING:
    from pathlib import Path

    from aiohttp import ClientSession
    from yandex_music import StationTracksResult

log = logging.getLogger(__name__)


# "lossless" is served as FLAC (and AAC) in an MP4 container: the plain flac
# and aac codecs are not offered for it, so the *-mp4 ones must be requested.
CODECS = "flac,aac,he-aac,mp3"
# Only the owner may read the settings: they hold the OAuth token.
SETTINGS_MODE = 0o600
# "My wave".
DEFAULT_STATION = "user:onyourwave"
LOSSLESS_CODECS = "flac,flac-mp4,aac,aac-mp4,he-aac,he-aac-mp4,mp3"


@model
class ExtendTrack(Track):
    station_id: str = ""
    batch_id: str = ""
    codec: str = ""
    bitrate_in_kbps: int = 0
    quality: str = ""

    @classmethod
    def from_track(cls, track: Track) -> ExtendTrack:
        return cls(
            **{
                key: track.__dict__[key]
                for key in track.__dict__
                if key in cls.__dataclass_fields__
            },
        )

    @property
    def genre(self) -> str:
        for album in self.albums:
            if album.genre:
                return str(album.genre).strip()
        return ""

    @property
    def cover(self) -> str:
        """The cover's URL with %% where the size goes, or "".

        Yandex serves a cover in any size ("100x100", "400x400", ...) from
        one address; the page picks the size it needs.
        """
        uri = self.cover_uri or self.og_image
        if not isinstance(uri, str) or not uri:
            return ""
        return uri if uri.startswith("http") else f"https://{uri}"

    def to_backend_track(self, mood: str = "") -> backend.Track:
        return backend.Track(
            track_id=self.track_id,
            title=self.title or "",
            artists=tuple(self.artists_name()),
            codec=self.codec,
            bitrate_in_kbps=self.bitrate_in_kbps,
            genre=self.genre,
            cover=self.cover,
            batch_id=self.batch_id,
            mood=mood,
        )


@model
class DownloadInfo(YandexMusicObject):
    quality: str
    codec: str
    urls: list[str]
    url: str
    bitrate: int
    track_id: str
    transport: str
    gain: bool
    real_id: str
    size: int = 0

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> DownloadInfo:
        # api.music.yandex.net/get-file-info returns fields in camelCase
        # (not snake_case) and without size -- map them by hand.
        return cls(
            quality=data["quality"],
            codec=data["codec"],
            urls=data["urls"],
            url=data["url"],
            bitrate=data["bitrate"],
            track_id=data["trackId"],
            transport=data["transport"],
            gain=data["gain"],
            real_id=data["realId"],
        )


# What the service may do wrong; the rest are our own mistakes.
SERVICE_ERRORS = (YandexMusicError, ClientError, TimeoutError, RuntimeError)
FEEDBACK_TYPES = {
    backend.Feedback.STARTED: "trackStarted",
    backend.Feedback.FINISHED: "trackFinished",
    backend.Feedback.SKIPPED: "skip",
}


def has_token(settings_path: Path) -> bool:
    """Say whether the settings file already holds an OAuth token."""
    try:
        return bool(json.loads(settings_path.read_text()).get("token"))
    except (OSError, ValueError, AttributeError):
        return False


class YandexMusicPlayer(ClientAsync):
    """The Yandex Music implementation of `backend.MusicBackend`."""

    _default_settings: ClassVar[dict[str, Any]] = {
        "token": None,
        "from_id": f"music-{uuid.uuid4()}",
        "quality": "lossless",
    }

    # Yandex answers 429 above a few simultaneous get-file-info requests;
    # callers (batch loading, track creation, reads) share these slots.
    DOWNLOAD_INFO_SLOTS: ClassVar[int] = 6

    def __init__(
        self,
        settings_path: Path,
        client_session: ClientSession,
    ) -> None:
        self.__last_track: str | None = None
        self.__last_station_id: tuple[str, str] = ("", "")
        self.__settings: dict[str, Any] = {}
        self.__auth_url: str | None = None
        self.__user_code: str | None = None
        # The mood this process set the station to, None before that.
        self.__mood: str | None = None
        self.__info_slots = Semaphore(self.DOWNLOAD_INFO_SLOTS)
        self.__session = client_session

        self.__settings_path = settings_path
        try:
            self.__settings = {
                **self._default_settings,
                **json.loads(self.__settings_path.read_text()),
            }
        except FileNotFoundError:
            self.__settings = {**self._default_settings}
            self.save_settings()

        super().__init__(
            self.__settings["token"],
            request=ClientRequest(client_session),
        )

        self.__init_task: Task[None] | None = None
        self.__signed_in = Event()
        if self.__settings["token"]:
            self.__signed_in.set()

    def _on_device_code(self, code: DeviceCode) -> None:
        self.__auth_url = code.verification_url
        self.__user_code = code.user_code
        log.info(
            "Auth: open %s and enter code %s",
            code.verification_url,
            code.user_code,
        )

    async def _init_token(self) -> None:
        try:
            token = await self.device_auth(on_code=self._on_device_code)
            self.__settings["token"] = token.access_token
            self.__signed_in.set()
            self.__auth_url = None
            self.__user_code = None
            self.save_settings()
            log.info("Token saved.")
        except CancelledError:
            raise
        except Exception:
            log.exception("Auth failed, retry in 30s.")
            await sleep(30)
            self.__init_task = create_task(
                self._init_token(), name="init-token"
            )
            return
        if self.__init_task is not None:
            init_task = self.__init_task
            self.__init_task = None
            init_task.cancel()

    async def aclose(self) -> None:
        if self.__init_task is None:
            return
        init_task = self.__init_task
        self.__init_task = None
        init_task.cancel()
        with suppress(CancelledError):
            await init_task

    def start_auth(self) -> None:
        """Ask Yandex for a code to enter on its page, until it is entered."""
        if self.authorized or self.__init_task is not None:
            return
        self.__init_task = create_task(self._init_token(), name="init-token")

    async def wait_authorized(self) -> None:
        await self.__signed_in.wait()

    async def authorize(self, _values: dict[str, str], /) -> None:
        raise backend.AuthError("Yandex signs in by the code on its page")

    @property
    def auth_prompt(self) -> backend.AuthPrompt:
        """The auth page and the code for it, while there's no token yet."""
        return backend.AuthPrompt(self.__auth_url, self.__user_code)

    @property
    def default_station(self) -> str:
        return DEFAULT_STATION

    def save_settings(self) -> None:
        """Write the settings, which hold the token, for the owner only."""
        path = self.__settings_path
        path.parent.mkdir(parents=True, exist_ok=True)
        # A new file, so that the token is never readable by others, not
        # even for a moment, and a crash can't leave half of it behind.
        temp = path.with_name(f"{path.name}.tmp")
        descriptor = os.open(
            temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, SETTINGS_MODE
        )
        with os.fdopen(descriptor, "w") as file:
            file.write(json.dumps(self.__settings))
        temp.replace(path)

    @property
    def config(self) -> dict[str, Any]:
        return self.__settings

    @property
    def authorized(self) -> bool:
        return self.__settings["token"] is not None

    @property
    def supports_moods(self) -> bool:
        return True

    @property
    def _from_id(self) -> str:
        result: str = self.__settings["from_id"]
        return result

    async def station_batch(
        self,
        station_id: str,
        /,
        *,
        exclude_track_ids: set[str],
    ) -> list[backend.Track]:
        """One batch of station tracks, ready to be downloaded."""
        try:
            return await self._station_batch(station_id, exclude_track_ids)
        except SERVICE_ERRORS as err:
            raise backend.BackendError(str(err)) from err

    async def _station_batch(
        self, station_id: str, exclude_track_ids: set[str]
    ) -> list[backend.Track]:
        station_tracks = await self.rotor_station_tracks(
            station=station_id,
            queue=self.__last_track,
        )
        if station_tracks is None:
            raise RuntimeError(f"No tracks for station {station_id}")

        if self.__last_station_id != (station_id, station_tracks.batch_id):
            self.__last_station_id = (station_id, station_tracks.batch_id)
            await self.rotor_station_feedback_radio_started(
                station_id,
                self._from_id,
                station_tracks.batch_id,
                time.time(),
            )

        candidates = []
        for sequence_track in station_tracks.sequence:
            if sequence_track.track is None:
                continue
            track = ExtendTrack.from_track(sequence_track.track)
            if track.track_id in exclude_track_ids or not track.available:
                continue
            track.station_id = station_id
            track.batch_id = station_tracks.batch_id
            candidates.append(track)

        await self._feedback_batch_edges(station_id, station_tracks)

        loaded = await gather(
            *(self._choose_best_dowanload_info(t) for t in candidates)
        )
        # Tracks come with the mood the station was set to when they came.
        mood = "" if self.__mood in (None, "all") else str(self.__mood)
        return [
            t.to_backend_track(mood)
            for t, ok in zip(candidates, loaded, strict=True)
            if ok
        ]

    async def station_settings(self, station_id: str) -> tuple[str, str, str]:
        """Return the station's (mood_energy, diversity, language)."""
        results = await self.rotor_station_info(station_id)
        current = results[0].settings2 if results else None
        if current is None:
            return "all", "default", "any"
        return (
            current.mood_energy or "all",
            current.diversity or "default",
            current.language or "any",
        )

    async def set_station_settings(
        self,
        station_id: str,
        mood_energy: str,
        diversity: str,
        language: str,
    ) -> None:
        """Change the station's settings. They are kept on the account."""
        log.info(
            "Station %s: mood %s, tracks %s, language %s",
            station_id,
            mood_energy,
            diversity,
            language,
        )
        if not await self.rotor_station_settings2(
            station_id, mood_energy, diversity, language
        ):
            log.warning("The station did not accept the new settings")
            return
        self.__mood = mood_energy

    async def _feedback_batch_edges(
        self, station_id: str, station_tracks: StationTracksResult
    ) -> None:
        first_track = station_tracks.sequence[0].track
        if first_track is not None:
            await self._send_feedback(
                first_track.track_id,
                "trackStarted",
                station_id,
                station_tracks.batch_id,
                0,
            )

        last_track = station_tracks.sequence[-1].track
        if last_track is not None:
            await self._send_feedback(
                last_track.track_id,
                "trackFinished",
                station_id,
                station_tracks.batch_id,
                (last_track.duration_ms or 0) / 1000,
            )
            self.__last_track = last_track.track_id

    async def _get_download_info(self, track_id: str) -> DownloadInfo | None:
        # https://github.com/MarshalX/yandex-music-api/issues/656#issuecomment-2466722441
        timestamp = int(time.time())
        params = {
            "ts": timestamp,
            "trackId": track_id,
            "quality": self.__settings["quality"],
            "codecs": (
                LOSSLESS_CODECS
                if self.__settings["quality"] == "lossless"
                else CODECS
            ),
            "transports": "raw",
        }
        res = "".join(str(e) for e in params.values()).replace(",", "")
        hmac_sign = hmac.new(
            DEFAULT_SIGN_KEY.encode(),
            res.encode(),
            hashlib.sha256,
        )
        sign = base64.b64encode(hmac_sign.digest()).decode()[:-1]
        params["sign"] = sign

        async def request() -> object:
            async with self.__info_slots:
                return await self._request.get(
                    "https://api.music.yandex.net/get-file-info",
                    params=params,
                )

        resp = await retry_on_rate_limit(request)
        info = resp.get("downloadInfo") if isinstance(resp, dict) else None
        if not isinstance(info, dict):
            log.warning(
                "Track %s: no downloadInfo in response: %r", track_id, resp
            )
            return None
        return DownloadInfo.from_json(info)

    async def _choose_best_dowanload_info(self, track: ExtendTrack) -> bool:
        download_info = await self._get_download_info(str(track.id))
        if download_info is None:
            return False

        track.codec = download_info.codec
        track.bitrate_in_kbps = download_info.bitrate
        track.quality = download_info.quality
        return True

    async def _get_download_links(
        self,
        track_id: str,
        codec: str,
        bitrate_in_kbps: int,
    ) -> list[str] | None:
        download_info = await self._get_download_info(track_id)
        if download_info is None:
            return None
        log.debug("Track %s, download info: %r", track_id, download_info)
        if bitrate_in_kbps != download_info.bitrate:
            log.warning(
                "Track %s, bitrate not match: %d != %d",
                track_id,
                bitrate_in_kbps,
                download_info.bitrate,
            )
            return None
        if codec != download_info.codec:
            log.warning(
                "Track %s, codec not match: %s != %s",
                track_id,
                codec,
                download_info.codec,
            )
            return None

        return download_info.urls

    async def download(self, track: backend.Track, path: Path, /) -> None:
        try:
            await self._download(track, path)
        except SERVICE_ERRORS as err:
            raise backend.BackendError(str(err)) from err

    async def _download(self, track: backend.Track, path: Path) -> None:
        links = await self._get_download_links(
            track.track_id, track.codec, track.bitrate_in_kbps
        )
        if not links:
            raise RuntimeError(f"no download links for {track.track_id}")

        async def request() -> None:
            with path.open("wb") as out:
                async with self.__session.get(links[0]) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.content.iter_chunked(1 << 16):
                        out.write(chunk)

        try:
            await retry_on_rate_limit(request)
        except (ClientError, TimeoutError) as err:
            raise backend.BackendError(
                f"download of {track.track_id} from {where(links[0])}: {err!r}"
            ) from err

    async def feedback_track(
        self,
        track: backend.Track,
        feedback: backend.Feedback,
        station_id: str,
        total_played_seconds: float,
    ) -> None:
        if not track.batch_id:
            return
        await self._send_feedback(
            track.track_id,
            FEEDBACK_TYPES[feedback],
            station_id,
            track.batch_id,
            total_played_seconds,
        )

    async def _send_feedback(
        self,
        track_id: str,
        feedback: str,
        station_id: str,
        batch_id: str,
        total_played_seconds: float,
    ) -> None:
        # trackStarted, trackFinished, skip.
        log.debug(
            "Feedback send, track id: %s, feedback: %s, "
            "station id: %s, batch_id: %s, seconds %s",
            track_id,
            feedback,
            station_id,
            batch_id,
            total_played_seconds,
        )
        try:
            await self.rotor_station_feedback(
                station=station_id,
                type_=feedback,
                timestamp=time.time(),
                from_=self._from_id,
                batch_id=batch_id,
                total_played_seconds=total_played_seconds,
                track_id=track_id,
            )
        except Exception:
            log.exception("Error send feedback:")
