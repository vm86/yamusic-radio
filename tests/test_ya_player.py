from __future__ import annotations

import json
import stat
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from aiohttp import ClientSession

from yamusic_radio import backend
from yamusic_radio.ya_player import (
    SETTINGS_MODE,
    ExtendTrack,
    YandexMusicPlayer,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


SAVED_VALUE = "fake-token"


@pytest_asyncio.fixture
async def session() -> AsyncIterator[ClientSession]:
    async with ClientSession() as client_session:
        yield client_session


@pytest.mark.asyncio
async def test_settings_are_created_with_their_directory_for_the_owner_only(
    tmp_path: Path, session: ClientSession
) -> None:
    path = tmp_path / "missing" / "dir" / "settings.json"

    player = YandexMusicPlayer(path, session)
    player.config["token"] = SAVED_VALUE
    player.save_settings()

    assert json.loads(path.read_text())["token"] == SAVED_VALUE
    assert stat.S_IMODE(path.stat().st_mode) == SETTINGS_MODE
    assert list(path.parent.iterdir()) == [path]  # no temp file left


@pytest.mark.asyncio
async def test_defaults_are_not_shared_between_players(
    tmp_path: Path, session: ClientSession
) -> None:
    first = YandexMusicPlayer(tmp_path / "a.json", session)
    first.config["token"] = SAVED_VALUE
    second = YandexMusicPlayer(tmp_path / "b.json", session)

    assert second.config["token"] is None
    assert not second.authorized


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
