from __future__ import annotations

import random
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from yamusic_radio import local
from yamusic_radio.backend import (
    AuthError,
    BackendError,
    Configurable,
    Track,
)
from yamusic_radio.multi import BackendSpec, MultiBackend

if TYPE_CHECKING:
    from pathlib import Path

    from yamusic_radio.cache import TrackCache

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="no ffmpeg"
)


def make_audio(path: Path, **tags: str) -> Path:
    """Write half a second of a tone with these tags."""
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = [
        arg for k, v in tags.items() for arg in ("-metadata", f"{k}={v}")
    ]
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.5",
            *metadata,
            "-y",
            str(path),
        ],
        check=True,
    )
    return path


@pytest.fixture
def music(tmp_path: Path) -> Path:
    """Make a folder of tracks, some in folders, and what is not music."""
    root = tmp_path / "music"
    make_audio(root / "a.mp3", title="Alpha", artist="Ann", genre="House")
    make_audio(root / "b.flac", title="Beta", artist="Bob")
    make_audio(root / "Jazz" / "Trio" / "c.mp3")
    make_audio(root / "Jazz" / "d.mp3", title="Delta", artist="Dee")
    (root / "notes.txt").write_text("not music")
    (root / "cover.jpg").write_bytes(b"x")
    (root / ".hidden").mkdir()
    (root / ".hidden" / "e.mp3").write_bytes(b"x")
    (root / "._a.mp3").write_bytes(b"x")  # what macOS leaves behind
    return root


def signed_in(backend: local.LocalBackend) -> bool:
    return backend.authorized


def resolved(path: Path) -> str:
    return str(path.resolve())


def test_the_tags_are_read_from_what_ffmpeg_writes() -> None:
    text = (
        ";FFMETADATA1\nTITLE=Fish \\= Chips\nartist=A\\;B\n"
        "Genre=Drum 'n' Bass\n"
    )

    assert local.parse_tags(text) == {
        "title": "Fish = Chips",
        "artist": "A;B",
        "genre": "Drum 'n' Bass",
    }
    assert local.parse_tags("") == {}


@needs_ffmpeg
def test_the_audio_files_are_found_under_the_folder(music: Path) -> None:
    found = local.scan(music)

    assert sorted(p.relative_to(music).as_posix() for p in found.values()) == [
        "Jazz/Trio/c.mp3",
        "Jazz/d.mp3",
        "a.mp3",
        "b.flac",
    ]
    # An id is made of where the file is: the same one every time, and safe
    # to name a file by.
    assert local.scan(music).keys() == found.keys()
    assert all(len(i) == 16 and i.isalnum() for i in found)


@needs_ffmpeg
@pytest.mark.asyncio
async def test_a_batch_has_the_tracks_with_their_tags(music: Path) -> None:
    backend = local.LocalBackend(music, batch_size=10, rng=random.Random(1))  # noqa: S311
    assert backend.authorized

    batch = await backend.station_batch("shuffle", exclude_track_ids=set())

    by_title = {t.title: t for t in batch}
    assert set(by_title) == {"Alpha", "Beta", "c", "Delta"}
    alpha = by_title["Alpha"]
    assert (alpha.artists, alpha.genre, alpha.codec) == (
        ("Ann",),
        "house",
        "mp3",
    )
    assert by_title["Beta"].codec == "flac"
    # No tag for the artist: the folder it is in, and for a file in the
    # folder itself a name that says so.
    assert by_title["c"].artists == ("Trio",)
    no_tags = make_audio(music / "f.mp3")
    again = local.LocalBackend(music, batch_size=10)
    titles = {
        t.title: t.artists
        for t in await again.station_batch("shuffle", exclude_track_ids=set())
    }
    assert titles[no_tags.stem] == (local.UNKNOWN_ARTIST,)


@needs_ffmpeg
@pytest.mark.asyncio
async def test_the_tracks_that_were_heard_come_last(music: Path) -> None:
    backend = local.LocalBackend(music, batch_size=2, rng=random.Random(2))  # noqa: S311
    heard: set[str] = set()

    for _ in range(2):  # four tracks, two at a time
        batch = await backend.station_batch("shuffle", exclude_track_ids=heard)
        assert not {t.track_id for t in batch} & heard
        heard |= {t.track_id for t in batch}
    assert len(heard) == 4

    # Everything was heard: it goes round again.
    again = await backend.station_batch("shuffle", exclude_track_ids=heard)
    assert len(again) == 2
    assert {t.track_id for t in again} <= heard


@needs_ffmpeg
@pytest.mark.asyncio
async def test_a_station_is_a_subfolder(music: Path) -> None:
    backend = local.LocalBackend(music, batch_size=10)

    batch = await backend.station_batch("Jazz", exclude_track_ids=set())

    assert {t.title for t in batch} == {"c", "Delta"}
    with pytest.raises(BackendError, match="no audio files"):
        await backend.station_batch("Rock", exclude_track_ids=set())


@pytest.mark.asyncio
async def test_a_folder_with_no_music_is_a_failure_of_the_backend(
    tmp_path: Path,
) -> None:
    (tmp_path / "x.txt").write_text("no")
    backend = local.LocalBackend(tmp_path)

    with pytest.raises(BackendError, match="no audio files"):
        await backend.station_batch("shuffle", exclude_track_ids=set())
    with pytest.raises(BackendError, match="no folder"):
        await local.LocalBackend().station_batch("", exclude_track_ids=set())


@needs_ffmpeg
@pytest.mark.asyncio
async def test_a_track_is_linked_not_copied_and_its_file_is_safe(
    music: Path, tmp_path: Path
) -> None:
    backend = local.LocalBackend(music, batch_size=10)
    track = next(
        t
        for t in await backend.station_batch("shuffle", exclude_track_ids=set())
        if t.title == "Alpha"
    )
    path = tmp_path / "work" / f"{track.track_id}.mp3"
    path.parent.mkdir()

    await backend.download(track, path)

    assert path.is_symlink()
    assert path.read_bytes() == (music / "a.mp3").read_bytes()
    await backend.download(track, path)  # again: the same
    path.unlink()  # what the radio does with it when it is heard
    assert (music / "a.mp3").exists()

    gone = Track("0" * 16, "Gone", ("x",), "mp3")
    with pytest.raises(BackendError, match="not in the folder"):
        await backend.download(gone, path)


@pytest.mark.asyncio
async def test_the_folder_is_chosen_and_kept(tmp_path: Path) -> None:
    kept: list[str] = []
    backend = local.LocalBackend(on_directory=kept.append)
    assert not signed_in(backend)
    assert [f.name for f in backend.auth_prompt.fields] == ["directory"]
    assert backend.settings == {"directory": ""}
    assert isinstance(backend, Configurable)

    for typed, message in (
        ({}, "type the folder"),
        ({"directory": "  "}, "type the folder"),
        ({"directory": str(tmp_path / "nowhere")}, "is not a folder"),
    ):
        with pytest.raises(AuthError, match=message):
            await backend.authorize(typed)
    assert len(kept) == 0

    await backend.authorize({"directory": f" {tmp_path} "})

    assert signed_in(backend)
    assert kept == [resolved(tmp_path)]
    assert backend.settings == {"directory": resolved(tmp_path)}
    # And it can be chosen again.
    other = tmp_path / "other"
    other.mkdir()
    await backend.authorize({"directory": str(other)})
    assert kept[-1] == resolved(other)


@pytest.mark.asyncio
async def test_a_folder_that_goes_away_makes_the_backend_unauthorized(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "music"
    folder.mkdir()
    backend = local.LocalBackend(folder)
    assert backend.authorized

    folder.rmdir()

    assert not backend.authorized


@pytest.mark.asyncio
async def test_it_waits_for_the_folder(tmp_path: Path) -> None:
    import asyncio  # noqa: PLC0415

    backend = local.LocalBackend()
    waiting = asyncio.create_task(backend.wait_authorized())
    await asyncio.sleep(0.01)
    assert not waiting.done()

    await backend.authorize({"directory": str(tmp_path)})

    await asyncio.wait_for(waiting, 1)


@needs_ffmpeg
@pytest.mark.asyncio
async def test_among_the_backends_it_is_told_by_its_stamp(music: Path) -> None:
    backend = local.LocalBackend(music, batch_size=10)
    multi = MultiBackend(
        [BackendSpec("local", "Local folder", lambda: backend)],
        enabled=["local"],
    )

    first = await multi.station_batch("", exclude_track_ids=set())

    assert all(t.track_id.startswith("local:") for t in first)
    assert all(t.source == "local" and t.native_id for t in first)
    # The radio knows the tracks by the stamped ids; the backend is told the
    # ones it knows, and leaves them out.
    seen = {t.track_id for t in first[:3]}
    second = await multi.station_batch("", exclude_track_ids=seen)
    assert not {t.track_id for t in second} & seen
    info = multi.status()[0]
    assert info["settings"] == {"directory": str(music)}
    assert info["setting_fields"] == [
        {"name": "directory", "label": "Folder with the music", "secret": False}
    ]


@pytest.mark.asyncio
async def test_the_folder_is_in_the_settings_of_the_next_run(
    tmp_path: Path, track_cache: TrackCache
) -> None:
    from argparse import Namespace  # noqa: PLC0415

    from aiohttp import ClientSession  # noqa: PLC0415

    from yamusic_radio import main  # noqa: PLC0415

    folder = tmp_path / "music"
    folder.mkdir()
    options = Namespace(local_dir=None, config=tmp_path / "none.json")
    async with ClientSession() as session:
        specs = main.backend_specs(options, session, track_cache)
        by_id = {spec.id: spec for spec in specs}
        assert not by_id["local"].configured()  # no folder yet

        backend = by_id["local"].create()
        assert isinstance(backend, local.LocalBackend)
        await backend.authorize({"directory": str(folder)})
        assert track_cache.get_setting("local_directory") == resolved(folder)

        # The next run: the folder is there, so the backend is on by itself.
        specs = main.backend_specs(options, session, track_cache)
        assert {s.id: s for s in specs}["local"].configured()

        # What the command line says wins, and is kept.
        other = tmp_path / "other"
        other.mkdir()
        options.local_dir = other
        specs = main.backend_specs(options, session, track_cache)
        created = {s.id: s for s in specs}["local"].create()
        assert isinstance(created, local.LocalBackend)
        assert created.settings == {"directory": str(other)}
