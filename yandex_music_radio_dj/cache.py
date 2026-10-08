"""Disk cache of what the radio has learned about tracks.

Only metadata is stored, never audio: the analysis of a track (so a track
that comes up again is not decoded and analyzed once more) and when it was
last played (so a restart doesn't repeat what was just heard).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
import time
from pathlib import Path

from yandex_music_radio_dj.dj_mix import TrackAnalysis

log = logging.getLogger(__name__)

DEFAULT_CACHE = Path.home() / ".cache/yandex-music-radio-dj.db"
# Rows kept; the ones not used for the longest time go first.
MAX_ROWS = 20000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    track_id TEXT NOT NULL,
    codec TEXT NOT NULL,
    title TEXT NOT NULL,
    analysis TEXT NOT NULL,
    played_at REAL,
    used_at REAL NOT NULL,
    PRIMARY KEY (track_id, codec)
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
            return TrackAnalysis(**json.loads(row[0]))
        except (TypeError, ValueError):
            # Written by another version of the program: analyze again.
            log.warning("Ignoring cached analysis of %s", track_id)
            return None

    def put_analysis(
        self,
        track_id: str,
        codec: str,
        title: str,
        analysis: TrackAnalysis,
    ) -> None:
        now = time.time()
        self._db.execute(
            "INSERT INTO tracks (track_id, codec, title, analysis, used_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (track_id, codec) DO UPDATE "
            "SET title = excluded.title, analysis = excluded.analysis, "
            "used_at = excluded.used_at",
            (
                track_id,
                codec,
                title,
                json.dumps(dataclasses.asdict(analysis)),
                now,
            ),
        )
        self._prune()
        self._db.commit()

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
