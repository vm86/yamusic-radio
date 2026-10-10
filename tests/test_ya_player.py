# ruff: noqa: S101
from __future__ import annotations

import json
import stat
from typing import TYPE_CHECKING

import pytest

from yamusic_radio.ya_player import SETTINGS_MODE, YandexMusicPlayer

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
