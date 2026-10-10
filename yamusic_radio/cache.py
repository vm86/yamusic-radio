"""Disk cache of what the radio has learned about tracks.

Only metadata is stored, never audio: the analysis of a track (so a track
that comes up again is not decoded and analyzed once more), its genre and
when it was last played (so a restart doesn't repeat what was just heard).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
import time
from pathlib import Path

from yamusic_radio.dj_mix import TrackAnalysis, key_detector

log = logging.getLogger(__name__)

DEFAULT_CACHE = Path.home() / ".cache/yamusic-radio.db"
# Rows kept; the ones not used for the longest time go first.
MAX_ROWS = 20000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    track_id TEXT NOT NULL,
    codec TEXT NOT NULL,
    title TEXT NOT NULL,
    analysis TEXT NOT NULL,
    genre TEXT NOT NULL DEFAULT '',
    played_at REAL,
    used_at REAL NOT NULL,
    PRIMARY KEY (track_id, codec)
);
CREATE TABLE IF NOT EXISTS transitions (
    at REAL NOT NULL,
    kind TEXT NOT NULL,
    beat_matched INTEGER NOT NULL,
    key_relation TEXT NOT NULL,
    from_track TEXT NOT NULL,
    to_track TEXT NOT NULL,
    from_key TEXT,
    to_key TEXT,
    effect TEXT
);
CREATE INDEX IF NOT EXISTS tracks_played ON tracks (played_at);
CREATE INDEX IF NOT EXISTS tracks_used ON tracks (used_at);
"""


class TrackCache:
    """SQLite store of track analyses and play times."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.executescript(_SCHEMA)
        columns = {
            row[1] for row in self._db.execute("PRAGMA table_info(tracks)")
        }
        transition_columns = {
            row[1] for row in self._db.execute("PRAGMA table_info(transitions)")
        }
        for column in ("from_key", "to_key", "effect"):
            if column not in transition_columns:
                # Made before the keys (or the effects) of the transitions
                # were kept: those rows have none (NULL), unlike a key that
                # was not found, or a blend without effects.
                self._db.execute(
                    f"ALTER TABLE transitions ADD COLUMN {column} TEXT"
                )
        if "genre" not in columns:
            # A cache made before the genre was kept.
            self._db.execute(
                "ALTER TABLE tracks ADD COLUMN genre TEXT NOT NULL DEFAULT ''"
            )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def get_analysis(self, track_id: str, codec: str) -> TrackAnalysis | None:
        row = self._db.execute(
            "SELECT analysis FROM tracks WHERE track_id = ? AND codec = ?",
            (track_id, codec),
        ).fetchone()
        if row is None:
            return None
        try:
            fields = json.loads(row[0])
            if fields.pop("key_by", None) != key_detector():
                # The key was not analyzed, or by something worse: analyze
                # the track again.
                return None
            analysis = TrackAnalysis(**fields)
        except (TypeError, ValueError):
            # Written by another version of the program: analyze again.
            log.warning("Ignoring cached analysis of %s", track_id)
            return None
        if analysis.key < 0 and key_detector() == "essentia":
            # No key was found, perhaps by an older detector: try again.
            return None
        return analysis

    def put_analysis(
        self,
        track_id: str,
        codec: str,
        title: str,
        analysis: TrackAnalysis,
        genre: str = "",
    ) -> None:
        now = time.time()
        self._db.execute(
            "INSERT INTO tracks (track_id, codec, title, analysis, genre, "
            "used_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (track_id, codec) DO UPDATE "
            "SET title = excluded.title, analysis = excluded.analysis, "
            "genre = excluded.genre, used_at = excluded.used_at",
            (
                track_id,
                codec,
                title,
                json.dumps(
                    {**dataclasses.asdict(analysis), "key_by": key_detector()}
                ),
                genre,
                now,
            ),
        )
        self._prune()
        self._db.commit()

    def set_genre(self, track_id: str, codec: str, genre: str) -> None:
        """Keep the genre of a track that is already in the cache."""
        self._db.execute(
            "UPDATE tracks SET genre = ? WHERE track_id = ? AND codec = ?",
            (genre, track_id, codec),
        )
        self._db.commit()

    def get_genre(self, track_id: str, codec: str) -> str | None:
        """Return the genre kept for a track, None if it isn't cached."""
        row = self._db.execute(
            "SELECT genre FROM tracks WHERE track_id = ? AND codec = ?",
            (track_id, codec),
        ).fetchone()
        return None if row is None else str(row[0])

    def add_transition(
        self,
        kind: str,
        *,
        beat_matched: bool,
        key_relation: str,
        from_track: str,
        to_track: str,
        from_key: str = "",
        to_key: str = "",
        effect: str = "none",
    ) -> None:
        """Remember how one track was blended into the next.

        The keys are Camelot codes, empty when a key was not found.
        """
        self._db.execute(
            "INSERT INTO transitions (at, kind, beat_matched, key_relation, "
            "from_track, to_track, from_key, to_key, effect) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                time.time(),
                kind,
                int(beat_matched),
                key_relation,
                from_track,
                to_track,
                from_key,
                to_key,
                effect,
            ),
        )
        self._db.commit()

    def key_pairs(self) -> list[tuple[str, str, int]]:
        """Return every key change there was, the most common first."""
        rows = self._db.execute(
            "SELECT from_key, to_key, COUNT(*) AS n FROM transitions "
            "WHERE from_key IS NOT NULL AND to_key IS NOT NULL "
            "GROUP BY from_key, to_key ORDER BY n DESC, from_key, to_key"
        ).fetchall()
        return [(str(a), str(b), int(n)) for a, b, n in rows]

    def transition_stats(self) -> dict[str, object]:
        """Count the transitions ever made by kind and by key change."""

        def count(column: str) -> dict[str, int]:
            # What was made before the column was kept is "unknown".
            rows = self._db.execute(
                f"SELECT COALESCE({column}, 'unknown'), COUNT(*) "  # noqa: S608
                f"FROM transitions "
                f"GROUP BY COALESCE({column}, 'unknown') "
                f"ORDER BY COUNT(*) DESC"
            ).fetchall()
            return {str(name): int(number) for name, number in rows}

        total, matched = self._db.execute(
            "SELECT COUNT(*), COALESCE(SUM(beat_matched), 0) FROM transitions"
        ).fetchone()
        return {
            "total": int(total),
            "beat_matched": int(matched),
            "by_type": count("kind"),
            "by_key": count("key_relation"),
            "by_effect": count("effect"),
        }

    def known_genres(self) -> list[tuple[str, int]]:
        """Return the genres of the cached tracks, the most common first."""
        rows = self._db.execute(
            "SELECT genre, COUNT(DISTINCT track_id) AS n FROM tracks "
            "WHERE genre != '' GROUP BY genre ORDER BY n DESC, genre"
        ).fetchall()
        return [(str(genre), int(count)) for genre, count in rows]

    def mark_played(self, track_id: str, codec: str) -> None:
        now = time.time()
        self._db.execute(
            "UPDATE tracks SET played_at = ?, used_at = ? "
            "WHERE track_id = ? AND codec = ?",
            (now, now, track_id, codec),
        )
        self._db.commit()

    def recently_played(self, limit: int) -> set[str]:
        """Return ids of the `limit` tracks played most recently."""
        rows = self._db.execute(
            "SELECT DISTINCT track_id FROM tracks WHERE played_at IS NOT NULL "
            "ORDER BY played_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return {row[0] for row in rows}

    def _prune(self) -> None:
        self._db.execute(
            "DELETE FROM tracks WHERE rowid IN (SELECT rowid FROM tracks "
            "ORDER BY used_at DESC LIMIT -1 OFFSET ?)",
            (MAX_ROWS,),
        )
