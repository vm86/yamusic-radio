# ruff: noqa: S101
from __future__ import annotations

import json
import stat
from typing import TYPE_CHECKING

import pytest

from yamusic_radio import backend
from yamusic_radio.ya_player import (
    SETTINGS_MODE,
    ExtendTrack,
    YandexMusicPlayer,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_settings_are_created_with_their_directory_for_the_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_auth(_: YandexMusicPlayer) -> None:
        return None

    monkeypatch.setattr(YandexMusicPlayer, "_init_token", no_auth)
    path = tmp_path / "missing" / "dir" / "settings.json"

    player = YandexMusicPlayer(path, None)  # type: ignore[arg-type]
    player.config["token"] = "secret"  # noqa: S105
    player.save_settings()

    assert json.loads(path.read_text())["token"] == "secret"  # noqa: S105
    assert stat.S_IMODE(path.stat().st_mode) == SETTINGS_MODE
    assert list(path.parent.iterdir()) == [path]  # no temp file left
    await player.aclose()


@pytest.mark.asyncio
async def test_defaults_are_not_shared_between_players(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_auth(_: YandexMusicPlayer) -> None:
        return None

    monkeypatch.setattr(YandexMusicPlayer, "_init_token", no_auth)

    first = YandexMusicPlayer(tmp_path / "a.json", None)  # type: ignore[arg-type]
    first.config["token"] = "secret"  # noqa: S105
    second = YandexMusicPlayer(tmp_path / "b.json", None)  # type: ignore[arg-type]

    assert second.config["token"] is None
    await first.aclose()
    await second.aclose()


def test_cover_gets_https_and_keeps_the_size_placeholder() -> None:
    track = ExtendTrack(id="a", cover_uri=None, og_image=None)
    assert track.cover == ""  # a track without a cover

    track.cover_uri = "avatars.yandex.net/get-music-content/1/abc/%%"
    assert (
        track.cover == "https://avatars.yandex.net/get-music-content/1/abc/%%"
    )

    track.cover_uri = None
    track.og_image = "https://avatars.yandex.net/get-music-content/2/x/%%"
    assert track.cover == track.og_image


def test_a_yandex_track_becomes_a_backend_track() -> None:
    track = ExtendTrack(id="7", title="Song", cover_uri="c/%%")
    track.codec = "flac-mp4"
    track.batch_id = "b1"

    assert track.to_backend_track() == backend.Track(
        track_id="7",
        title="Song",
        artists=(),
        codec="flac-mp4",
        cover="https://c/%%",
        batch_id="b1",
    )
