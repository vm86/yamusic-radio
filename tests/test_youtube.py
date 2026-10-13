from __future__ import annotations

import random
from typing import TYPE_CHECKING, Any

import pytest

from yamusic_radio import youtube
from yamusic_radio.backend import AuthError, BackendError, Feedback, Track
from yamusic_radio.multi import BackendSpec, MultiBackend

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def song(
    n: int, length: str = "3:07", artists: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    """Make an item the way ytmusicapi gives it."""
    return {
        "videoId": f"v{n}",
        "title": f"Song {n}",
        "artists": [{"name": "Artist", "id": "a"}]
        if artists is None
        else artists,
        "length": length,
        "thumbnail": [{"url": f"https://img/{n}", "width": 60}],
    }


class FakeClient:
    """Stands for ytmusicapi.YTMusic: it records what it was asked."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.radio: list[dict[str, Any]] = [song(n) for n in range(10)]
        self.playlist: list[dict[str, Any]] = [song(n) for n in range(20, 30)]
        self.found: list[dict[str, Any]] = [song(n) for n in range(40, 50)]

    def get_watch_playlist(self, **kw: object) -> dict[str, Any]:
        self.calls.append(("radio", kw))
        return {"tracks": self.radio}

    def get_playlist(self, **kw: object) -> dict[str, Any]:
        self.calls.append(("playlist", kw))
        return {"tracks": self.playlist}

    def get_charts(self, country: str) -> dict[str, Any]:
        self.calls.append(("charts", {"country": country}))
        return {"videos": [{"title": "Top", "playlistId": "PLtop"}]}

    def search(self, query: str, **kw: object) -> list[dict[str, Any]]:
        self.calls.append(("search", {"query": query, **kw}))
        return self.found

    def get_liked_songs(self, **kw: object) -> dict[str, Any]:
        self.calls.append(("liked", kw))
        return {"tracks": [song(7)]}


def make_backend(
    client: FakeClient | None = None,
    auth: Path | None = None,
    *,
    download: Callable[[str, Path], None] | None = None,
    batch_size: int = youtube.BATCH_SIZE,
) -> tuple[youtube.YouTubeMusicBackend, FakeClient, list[float]]:
    fake = client or FakeClient()
    now = [0.0]
    backend = youtube.YouTubeMusicBackend(
        auth,
        client=lambda _auth: fake,
        download=download or (lambda _id, _path: None),
        batch_size=batch_size,
        rng=random.Random(1),  # noqa: S311
        clock=lambda: now[0],
    )
    return backend, fake, now


def test_a_song_becomes_a_track() -> None:
    track = youtube.track_of(song(3))

    assert track == Track(
        track_id="v3",
        title="Song 3",
        artists=("Artist",),
        codec="m4a",
        cover="https://img/3",
    )


def test_what_is_not_music_is_left_out() -> None:
    assert youtube.track_of({"title": "no id"}) is None
    assert youtube.track_of(song(1, "58:12")) is None  # a mix
    assert youtube.track_of(song(1, "0:10")) is None  # a jingle
    unknown = youtube.track_of(song(1, artists=[]))
    assert unknown is not None
    assert unknown.artists == ("Unknown artist",)
    # A search item tells the length in seconds, a watch item as text.
    assert youtube.track_of({**song(1), "duration_seconds": 5000}) is None
    assert youtube.track_of({"videoId": "x", "title": "t"}) is not None


def test_lengths_are_read() -> None:
    assert youtube.parse_length("3:07") == 187
    assert youtube.parse_length("1:02:03") == 3723
    assert youtube.parse_length("") == 0
    assert youtube.parse_length("a:b") == 0
    assert youtube.parse_length(None) == 0


@pytest.mark.asyncio
async def test_each_kind_of_station_asks_for_its_own_list() -> None:
    backend, fake, _ = make_backend()

    for station, call, expect in (
        ("radio:abc", "radio", {"videoId": "abc", "radio": True}),
        ("playlist:PL1", "playlist", {"playlistId": "PL1"}),
        ("search:deep house", "search", {"query": "deep house"}),
        ("charts:RU", "charts", {"country": "RU"}),
    ):
        batch = await backend.station_batch(station, exclude_track_ids=set())
        assert batch, station
        name, kw = fake.calls[-1] if call != "charts" else fake.calls[-2]
        assert name == call
        assert expect.items() <= kw.items()
    # The chart is the first playlist of the chart.
    assert fake.calls[-1] == (
        "playlist",
        {"playlistId": "PLtop", "limit": 200},
    )
    # By default, the global chart.
    await make_backend()[0].station_batch("", exclude_track_ids=set())


@pytest.mark.asyncio
async def test_the_default_station_is_the_global_chart() -> None:
    backend, fake, _ = make_backend()

    await backend.station_batch("", exclude_track_ids=set())

    assert fake.calls[0] == ("charts", {"country": "ZZ"})


@pytest.mark.asyncio
async def test_liked_songs_need_the_headers_file(tmp_path: Path) -> None:
    backend, _, _ = make_backend()
    with pytest.raises(BackendError, match="--youtube-auth"):
        await backend.station_batch("liked", exclude_track_ids=set())

    headers = tmp_path / "browser.json"
    signed, fake, _ = make_backend(auth=headers)
    batch = await signed.station_batch("liked", exclude_track_ids=set())
    assert [t.track_id for t in batch] == ["v7"]
    assert fake.calls[-1][0] == "liked"


@pytest.mark.asyncio
async def test_a_batch_is_a_few_unheard_tracks_at_random() -> None:
    backend, _, _ = make_backend(batch_size=3)
    heard = {f"v{n}" for n in range(20, 27)}

    batch = await backend.station_batch("playlist:PL1", exclude_track_ids=heard)

    assert len(batch) == 3
    assert {t.track_id for t in batch} <= {"v27", "v28", "v29"}

    # Everything was heard: the list is played again.
    again = await backend.station_batch(
        "playlist:PL1",
        exclude_track_ids={f"v{n}" for n in range(20, 30)},
    )
    assert len(again) == 3


@pytest.mark.asyncio
async def test_a_list_is_kept_for_a_while_but_a_radio_is_asked_every_time() -> (
    None
):
    backend, fake, now = make_backend()

    await backend.station_batch("playlist:PL1", exclude_track_ids=set())
    await backend.station_batch("playlist:PL1", exclude_track_ids=set())
    await backend.station_batch("radio:a", exclude_track_ids=set())
    await backend.station_batch("radio:a", exclude_track_ids=set())
    assert [c[0] for c in fake.calls].count("playlist") == 1
    assert [c[0] for c in fake.calls].count("radio") == 2

    now[0] = youtube.REFRESH_SECONDS + 1
    await backend.station_batch("playlist:PL1", exclude_track_ids=set())
    assert [c[0] for c in fake.calls].count("playlist") == 2


@pytest.mark.asyncio
async def test_failures_are_a_backend_error() -> None:
    backend, fake, _ = make_backend()
    fake.found = [song(1, "58:12")]  # nothing playable

    with pytest.raises(BackendError, match="no tracks"):
        await backend.station_batch("search:mix", exclude_track_ids=set())
    with pytest.raises(BackendError, match="unknown"):
        await backend.station_batch("nonsense", exclude_track_ids=set())

    def broken(**_: object) -> dict[str, Any]:
        raise ConnectionError("offline")

    fake.get_watch_playlist = broken  # type: ignore[method-assign]
    with pytest.raises(BackendError, match="offline"):
        await backend.station_batch("radio:a", exclude_track_ids=set())


@pytest.mark.asyncio
async def test_the_audio_is_downloaded_to_the_path(tmp_path: Path) -> None:
    path = tmp_path / "v3.m4a"
    seen: list[tuple[str, Path]] = []

    def download(video_id: str, target: Path) -> None:
        seen.append((video_id, target))
        target.write_bytes(b"audio")

    backend, _, _ = make_backend(download=download)
    track = youtube.track_of(song(3))
    assert track is not None

    await backend.download(track, path)

    assert seen == [("v3", path)]
    assert path.read_bytes() == b"audio"


@pytest.mark.asyncio
async def test_a_download_that_leaves_nothing_fails(tmp_path: Path) -> None:
    backend, _, _ = make_backend()  # downloads nothing
    track = youtube.track_of(song(3))
    assert track is not None

    with pytest.raises(BackendError, match="not downloaded"):
        await backend.download(track, tmp_path / "v3.m4a")

    def broken(_id: str, _path: Path) -> None:
        raise OSError("disk full")

    failing, _, _ = make_backend(download=broken)
    with pytest.raises(BackendError, match="disk full"):
        await failing.download(track, tmp_path / "v3.m4a")


@pytest.mark.asyncio
async def test_it_needs_no_sign_in_and_is_no_station_of_moods() -> None:
    backend, _, _ = make_backend()

    assert backend.authorized
    assert backend.auth_prompt.fields == ()
    assert not backend.supports_moods
    await backend.wait_authorized()
    with pytest.raises(AuthError):
        await backend.authorize({})
    await backend.feedback_track(
        youtube.track_of(song(1)),  # type: ignore[arg-type]
        Feedback.STARTED,
        "charts",
        0.0,
    )
    assert await backend.station_settings("charts") == (
        "all",
        "default",
        "any",
    )


def test_without_the_libraries_it_says_how_to_install_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import sys  # noqa: PLC0415

    monkeypatch.setitem(sys.modules, "ytmusicapi", None)
    monkeypatch.setitem(sys.modules, "yt_dlp", None)

    with pytest.raises(BackendError, match=r"pip install"):
        youtube.make_client(None)
    with pytest.raises(BackendError, match=r"pip install"):
        youtube.download_audio("v1", tmp_path / "a.m4a")


@pytest.mark.asyncio
async def test_it_is_a_backend_of_the_multi_backend() -> None:
    spec = BackendSpec(
        "youtube",
        "YouTube Music",
        lambda: make_backend()[0],
        lambda: False,
    )
    multi = MultiBackend([spec], enabled=["youtube"])

    batch = await multi.station_batch("", exclude_track_ids=set())

    assert batch
    assert all(t.track_id.startswith("youtube:") for t in batch)
    assert all(t.source == "youtube" for t in batch)
