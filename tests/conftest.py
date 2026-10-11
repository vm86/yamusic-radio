from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from yamusic_radio.cache import TrackCache

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def cache_path(tmp_path: Path) -> Path:
    return tmp_path / "cache.db"


@pytest.fixture
def track_cache(cache_path: Path) -> Iterator[TrackCache]:
    cache = TrackCache(cache_path)
    yield cache
    cache.close()
