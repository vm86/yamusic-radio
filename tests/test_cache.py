# ruff: noqa: S101, PLR2004
from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

from yandex_music_radio_dj.cache import MAX_ROWS, TrackCache
from yandex_music_radio_dj.dj_mix import TrackAnalysis, key_detector

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


def test_analysis_with_an_unknown_key_detector_is_analyzed_again(
    track_cache: TrackCache,
) -> None:
    cache = track_cache
    cache.put_analysis("1", "mp3", "A - B", ANALYSIS)
    stored = json.loads(
        cache._db.execute("SELECT analysis FROM tracks").fetchone()[0]  # noqa: SLF001
    )
    assert stored["key_by"] == key_detector()

    for key_by in (None, "something else"):
        stored["key_by"] = key_by
        cache._db.execute(  # noqa: SLF001
            "UPDATE tracks SET analysis = ?", (json.dumps(stored),)
        )
        assert cache.get_analysis("1", "mp3") is None


def test_genre_is_stored_and_updated(track_cache: TrackCache) -> None:
    cache = track_cache
    assert cache.get_genre("1", "mp3") is None

    cache.put_analysis("1", "mp3", "A - B", ANALYSIS, "house")
    assert cache.get_genre("1", "mp3") == "house"

    cache.set_genre("1", "mp3", "techno")
    assert cache.get_genre("1", "mp3") == "techno"
    assert cache.get_genre("1", "flac") is None


def test_cache_without_the_genre_column_is_upgraded(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE tracks (track_id TEXT NOT NULL, codec TEXT NOT NULL,"
        " title TEXT NOT NULL, analysis TEXT NOT NULL, played_at REAL,"
        " used_at REAL NOT NULL, PRIMARY KEY (track_id, codec));"
        "INSERT INTO tracks VALUES ('1', 'mp3', 'A - B', '{}', NULL, 1.0);"
    )
    db.commit()
    db.close()

    cache = TrackCache(path)

    assert cache.get_genre("1", "mp3") == ""
    cache.close()


def test_key_pairs_from_an_older_cache_are_not_counted(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE transitions (at REAL NOT NULL, kind TEXT NOT NULL,"
        " beat_matched INTEGER NOT NULL, key_relation TEXT NOT NULL,"
        " from_track TEXT NOT NULL, to_track TEXT NOT NULL);"
        "INSERT INTO transitions"
        " VALUES (1.0, 'bass swap', 1, 'same', 'a', 'b');"
    )
    db.commit()
    db.close()

    cache = TrackCache(path)
    cache.add_transition(
        "bass swap",
        beat_matched=True,
        key_relation="neighbour",
        from_track="b",
        to_track="c",
        from_key="8A",
        to_key="9A",
    )

    assert cache.transition_stats()["total"] == 2  # both are counted
    assert cache.key_pairs() == [("8A", "9A", 1)]  # only one has its keys
    cache.close()
