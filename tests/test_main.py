from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from typing import TYPE_CHECKING

from yamusic_radio import main

if TYPE_CHECKING:
    from yamusic_radio.cache import TrackCache


def options(**given: object) -> Namespace:
    values: dict[str, object] = {
        "cache": Path("/data/cache.db"),
        "tracks_dir": None,
        "no_keep_tracks": False,
    }
    return Namespace(**{**values, **given})


def test_the_tracks_are_kept_next_to_the_cache_unless_told_otherwise(
    track_cache: TrackCache,
) -> None:
    assert main._tracks_dir(options(), track_cache) == Path(  # noqa: SLF001
        "/data/cache-tracks"
    )
    chosen = Path("/data/tracks")
    assert main._tracks_dir(options(tracks_dir=chosen), track_cache) == chosen  # noqa: SLF001


def test_no_tracks_are_kept_without_a_cache_or_when_it_is_refused(
    track_cache: TrackCache,
) -> None:
    assert main._tracks_dir(options(), None) is None  # noqa: SLF001
    assert main._tracks_dir(options(no_keep_tracks=True), track_cache) is None  # noqa: SLF001
