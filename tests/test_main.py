from __future__ import annotations

import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path
from typing import TYPE_CHECKING

from yamusic_radio import main

if TYPE_CHECKING:
    import pytest

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


def test_the_page_can_be_left_out_from_the_command_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["yamusic-dj"])
    assert main.parse_args().no_web is False

    monkeypatch.setattr(sys, "argv", ["yamusic-dj", "--no-web"])
    assert main.parse_args().no_web is True


ROOT = Path(__file__).parent.parent


def build(tmp_path: Path, name: str, *, backend_only: bool) -> Path:
    """Run the build of the package's modules and data; return the result."""
    lib = tmp_path / name
    env = {**os.environ, "YAMUSIC_BACKEND_ONLY": "1" if backend_only else ""}
    subprocess.run(
        [sys.executable, "setup.py", "-q", "build_py", "--build-lib", str(lib)],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
    )
    return lib / "yamusic_radio"


def test_a_backend_only_build_has_no_page_and_the_rest_is_there(
    tmp_path: Path,
) -> None:
    full = build(tmp_path, "full", backend_only=False)
    backend = build(tmp_path, "backend", backend_only=True)

    assert (full / "web" / "index.html").is_file()
    assert any((full / "web" / "assets").iterdir())
    assert not (backend / "web").exists()
    # What the radio is made of is there either way, with its service file.
    for name in ("dj.py", "main.py", "local.py", "contrib/yamusic-dj.service"):
        assert (backend / name).is_file()
        assert (full / name).is_file()
    # "0" and "no" are no, as for any switch of the kind.
    off = build(tmp_path, "off", backend_only=False)
    assert (off / "web" / "index.html").is_file()
