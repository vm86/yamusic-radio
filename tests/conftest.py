from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from yandex_music_radio_dj.cache import TrackCache

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def track_cache(tmp_path: Path) -> Iterator[TrackCache]:
    cache = TrackCache(tmp_path / "cache.db")
    yield cache
    cache.close()
