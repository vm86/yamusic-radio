# ruff: noqa: S101, PLR2004
from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING

from yandex_music_radio_dj.cache import MAX_ROWS, TrackCache
from yandex_music_radio_dj.dj_mix import TrackAnalysis

if TYPE_CHECKING:
    from pathlib import Path

ANALYSIS = TrackAnalysis(
    bpm=124.0,
    head_beat=100.5,
    tail_beat=9000.25,
    start=10,
    end=20000,
    loudness_db=-9.5,
    intro_end=500,
    outro_start=15000,
)


def test_analysis_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "cache.db"
    cache = TrackCache(path)
    cache.put_analysis("1", "mp3", "A - B", ANALYSIS)
    cache.close()

    reopened = TrackCache(path)
    assert reopened.get_analysis("1", "mp3") == ANALYSIS
    assert reopened.get_analysis("1", "flac") is None
    assert reopened.get_analysis("2", "mp3") is None
    reopened.close()


def test_unreadable_analysis_is_ignored(track_cache: TrackCache) -> None:
    cache = track_cache
    cache.put_analysis("1", "mp3", "A - B", ANALYSIS)
    cache._db.execute("UPDATE tracks SET analysis = '{\"bpm\": 1}'")  # noqa: SLF001

    assert cache.get_analysis("1", "mp3") is None


def test_recently_played_is_newest_first_and_limited(
    track_cache: TrackCache,
) -> None:
    cache = track_cache
    for track_id in "abc":
        cache.put_analysis(track_id, "mp3", track_id, ANALYSIS)
    cache.put_analysis("unplayed", "mp3", "x", ANALYSIS)
    for offset, track_id in enumerate("abc"):
        cache.mark_played(track_id, "mp3")
        cache._db.execute(  # noqa: SLF001
            "UPDATE tracks SET played_at = ? WHERE track_id = ?",
            (1000 + offset, track_id),
        )

    assert cache.recently_played(2) == {"b", "c"}
    assert cache.recently_played(10) == {"a", "b", "c"}


def test_prune_drops_least_recently_used(
    track_cache: TrackCache, monkeypatch: object
) -> None:
    cache = track_cache
    import yandex_music_radio_dj.cache as module  # noqa: PLC0415

    monkeypatch.setattr(module, "MAX_ROWS", 3)  # type: ignore[attr-defined]
    for i in range(5):
        cache.put_analysis(str(i), "mp3", str(i), ANALYSIS)
        cache._db.execute(  # noqa: SLF001
            "UPDATE tracks SET used_at = ? WHERE track_id = ?", (i, str(i))
        )

    cache.put_analysis("5", "mp3", "5", ANALYSIS)

    assert cache.get_analysis("0", "mp3") is None
    assert cache.get_analysis("5", "mp3") == ANALYSIS
    assert MAX_ROWS > 3


def test_analysis_without_key_is_analyzed_again(
    track_cache: TrackCache,
) -> None:
    cache = track_cache
    cache.put_analysis("1", "mp3", "A - B", ANALYSIS)
    old = {k: v for k, v in dataclasses.asdict(ANALYSIS).items() if k != "key"}
    cache._db.execute("UPDATE tracks SET analysis = ?", (json.dumps(old),))  # noqa: SLF001

    assert cache.get_analysis("1", "mp3") is None
