from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from asyncio import CancelledError, Semaphore, gather, sleep
from asyncio.tasks import create_task
from contextlib import suppress
from typing import TYPE_CHECKING, Any, ClassVar

from yandex_music import (
    ClientAsync,
    DeviceCode,
    Track,
    YandexMusicObject,
)
from yandex_music.utils import model
from yandex_music.utils.sign_request import (
    DEFAULT_SIGN_KEY,
)

from yandex_music_radio_dj.request import ClientRequest, retry_on_rate_limit

if TYPE_CHECKING:
    from pathlib import Path

    from aiohttp import ClientSession
    from yandex_music import StationTracksResult

log = logging.getLogger(__name__)


# "lossless" is served as FLAC (and AAC) in an MP4 container: the plain flac
# and aac codecs are not offered for it, so the *-mp4 ones must be requested.
CODECS = "flac,aac,he-aac,mp3"
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


class YandexMusicPlayer(ClientAsync):
    _default_settings: ClassVar[dict[str, Any]] = {
        "token": None,
        "last_track": None,
        "from_id": f"music-{uuid.uuid4()}",
        "station_id": "user:onyourwave",
        "quality": "lossless",
        "blacklist": [],
    }

    # Yandex answers 429 above a few simultaneous get-file-info requests;
    # callers (batch loading, track creation, reads) share these slots.
    DOWNLOAD_INFO_SLOTS: ClassVar[int] = 6

    def __init__(
        self,
        settings_path: Path,
        client_session: ClientSession,
    ) -> None:
        self.__last_track: str = ""
        self.__last_station_id: tuple[str, str] = ("", "")
        self.__settings: dict[str, Any] = {}
        self.__auth_url: str | None = None
        self.__user_code: str | None = None
        self.__info_slots = Semaphore(self.DOWNLOAD_INFO_SLOTS)

        self.__settings_path = settings_path
        try:
            self.__settings = {
                **self._default_settings,
                **json.loads(self.__settings_path.read_text()),
            }
            self.__last_track = self.__settings["last_track"]
        except FileNotFoundError:
            self.__settings = self._default_settings
            self.save_settings()

        super().__init__(
            self.__settings["token"],
            request=ClientRequest(client_session),
        )

        self.__init_task = None
        if not self.__settings["token"]:
            self.__init_task = create_task(
                self._init_token(),
                name="init-token",
            )

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

    @property
    def auth_url(self) -> str | None:
        """Auth page URL, while there's no token yet."""
        return self.__auth_url

    @property
    def user_code(self) -> str | None:
        """Code for manual entry on the auth page."""
        return self.__user_code

    def save_settings(self) -> None:
        self.__settings_path.write_text(json.dumps(self.__settings))

    @property
    def config(self) -> dict[str, Any]:
        return self.__settings

    @property
    def is_init(self) -> bool:
        return self.__settings["token"] is not None

    @property
    def _from_id(self) -> str:
        result: str = self.__settings["from_id"]
        return result

    async def station_batch(
        self,
        station_id: str,
        *,
        exclude_track_ids: set[str],
    ) -> list[ExtendTrack]:
        """One batch of station tracks, ready to be downloaded."""
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
            if track.genre in self.config["blacklist"]:
                continue
            track.station_id = station_id
            track.batch_id = station_tracks.batch_id
            candidates.append(track)

        await self._feedback_batch_edges(station_id, station_tracks)

        loaded = await gather(
            *(self._choose_best_dowanload_info(t) for t in candidates)
        )
        return [t for t, ok in zip(candidates, loaded, strict=True) if ok]

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

    async def _feedback_batch_edges(
        self, station_id: str, station_tracks: StationTracksResult
    ) -> None:
        first_track = station_tracks.sequence[0].track
        if first_track is not None:
            await self.feedback_track(
                first_track.track_id,
                "trackStarted",
                station_id,
                station_tracks.batch_id,
                0,
            )

        last_track = station_tracks.sequence[-1].track
        if last_track is not None:
            await self.feedback_track(
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

        async def request() -> Any:  # noqa: ANN401
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

    async def get_download_links(
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

    async def feedback_track(
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
