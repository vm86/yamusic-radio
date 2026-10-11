from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING

import numpy as np
import pytest
from aiohttp.test_utils import TestClient, TestServer

from yamusic_radio import dj, spectrum
from yamusic_radio.dj_mix import SAMPLE_RATE

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


def tone(hz: float, seconds: float, amplitude: float = 0.5) -> np.ndarray:
    t = np.arange(int(SAMPLE_RATE * seconds)) / SAMPLE_RATE
    mono = amplitude * np.sin(2 * np.pi * hz * t)
    return np.stack([mono, mono], axis=1).astype(np.float32)


def test_a_tone_lights_the_band_it_is_in() -> None:
    analyzer = spectrum.SpectrumAnalyzer()
    edges = np.geomspace(spectrum.LOW_HZ, spectrum.HIGH_HZ, spectrum.BANDS + 1)

    for hz in (100.0, 1000.0, 8000.0):
        frames = analyzer.feed(tone(hz, 0.5))
        bands = np.array(frames[-1].bands)
        expected = int(np.searchsorted(edges, hz)) - 1
        assert len(bands) == spectrum.BANDS
        assert int(bands.argmax()) == expected, hz
        assert bands.max() > 0.5
        assert (bands >= 0).all()
        assert (bands <= 1).all()


def test_silence_is_zero() -> None:
    frames = spectrum.SpectrumAnalyzer().feed(
        np.zeros((SAMPLE_RATE // 4, 2), dtype=np.float32)
    )

    assert frames
    assert all(max(f.bands) == 0.0 for f in frames)


def test_the_frames_are_the_same_however_the_mix_is_cut() -> None:
    mix = tone(440.0, 1.0) + tone(3000.0, 1.0, 0.2)
    whole = spectrum.SpectrumAnalyzer().feed(mix)

    cut = spectrum.SpectrumAnalyzer()
    pieces: list[spectrum.Frame] = []
    for start in range(0, len(mix), 11025):  # as the mix comes: 0.25 s
        pieces += cut.feed(mix[start : start + 11025])

    assert [f.t for f in pieces] == pytest.approx(
        [f.t for f in whole[: len(pieces)]]
    )
    assert [f.bands for f in pieces] == [f.bands for f in whole[: len(pieces)]]
    assert len(whole) - len(pieces) <= 1
    # ~30 a second, positions in seconds of the mix, from the window's middle.
    gaps = np.diff([f.t for f in pieces])
    assert gaps == pytest.approx(spectrum.HOP / SAMPLE_RATE)
    assert pieces[0].t == pytest.approx(spectrum.WINDOW / 2 / SAMPLE_RATE)
    assert cut.position == pytest.approx(1.0, abs=1 / SAMPLE_RATE)


def test_a_frame_is_short_json() -> None:
    frame = spectrum.Frame(1.23456, (0.1, 0.25555))

    assert json.loads(frame.to_json()) == {"t": 1.235, "b": [0.1, 0.256]}


def test_the_feed_gives_the_backlog_from_a_position_and_then_the_new_ones() -> (
    None
):
    feed = spectrum.SpectrumFeed()
    feed.publish([spectrum.Frame(t, (0.0,)) for t in (1.0, 2.0, 3.0)])

    queue, backlog = feed.subscribe(since=2.0)
    assert [f.t for f in backlog] == [2.0, 3.0]
    feed.publish([spectrum.Frame(4.0, (0.0,))])
    assert [f.t for f in queue.get_nowait()] == [4.0]

    assert feed.subscribe()[1] == []  # no position: only the new ones
    feed.unsubscribe(queue)
    feed.publish([spectrum.Frame(5.0, (0.0,))])
    assert queue.empty()

    feed.reset()
    assert feed.subscribe(since=0.0)[1] == []


def test_a_page_that_cannot_keep_up_loses_the_oldest_frames() -> None:
    feed = spectrum.SpectrumFeed()
    queue, _ = feed.subscribe()

    for i in range(spectrum.CLIENT_QUEUE_SIZE + 5):
        feed.publish([spectrum.Frame(float(i), (0.0,))])

    assert queue.qsize() == spectrum.CLIENT_QUEUE_SIZE
    assert queue.get_nowait()[0].t == 5.0


@pytest.mark.asyncio
async def test_the_start_of_a_listener_can_be_waited_for() -> None:
    starts = spectrum.StartPositions()
    assert await starts.get("nobody", 0.05) is None

    waiting = asyncio.create_task(starts.get("page", 2.0))
    await asyncio.sleep(0.01)
    starts.set("page", 12.5)

    assert await waiting == 12.5
    assert await starts.get("page", 0.0) == 12.5  # and it is kept

    for i in range(spectrum.STARTS_KEPT + 3):
        starts.set(f"n{i}", float(i))
    assert await starts.get("page", 0.0) is None  # the oldest are forgotten


def whole(_data: bytes) -> int:
    """Start a listener at the first byte: the bytes here are not frames."""
    return 0


def fed_hub(*, chunks: int = 10) -> dj.Broadcaster:
    """Make a stream of 1000 bytes a second, published 500 bytes by 0.5 s."""
    hub = dj.Broadcaster(
        2.0, bytes_per_second=1000, frame_start=whole, lag_seconds=0.0
    )
    for i in range(1, chunks + 1):
        hub.fed = i * 0.5
        hub.publish(bytes(500))
    return hub


def test_a_listener_is_told_where_in_the_mix_it_starts() -> None:
    hub = fed_hub()  # 5 s of the mix are published

    queue, position = hub.subscribe_at()

    assert sum(len(c) for c in [queue.get_nowait()]) == 2000  # 2 s: the preroll
    assert position == pytest.approx(3.0)  # the head is at 5 s

    # Nothing published yet: the first listener starts at the beginning.
    assert dj.Broadcaster().subscribe_at()[1] == 0.0
    # The mix starting over forgets where it was.
    hub.reset_position()
    assert hub.subscribe_at()[1] is None


def test_a_stream_that_is_not_steady_is_measured_lately() -> None:
    hub = dj.Broadcaster(
        2.0, bytes_per_second=1000, frame_start=whole, lag_seconds=0.0
    )
    for i in range(1, 11):  # twice what it says: 2000 bytes a second
        hub.fed = i * 0.5
        hub.publish(bytes(1000))

    _, position = hub.subscribe_at()

    # The preroll is 2000 bytes: what the nominal rate calls 2 s, but 1 s.
    assert position == pytest.approx(5.0 - 1.0, abs=0.05)


@pytest.mark.asyncio
async def test_the_spectrum_is_served_from_where_the_page_starts() -> None:
    hub = fed_hub()
    feed = spectrum.SpectrumFeed()
    feed.publish([spectrum.Frame(t / 30, (t / 300,) * 2) for t in range(180)])
    app = dj.make_app(hub, spectrum=feed)

    async with TestClient(TestServer(app)) as client:
        stream = await client.get("/stream?l=page1")
        assert stream.status == 200

        events = await client.get("/api/spectrum?l=page1")
        assert events.content_type == "text/event-stream"
        assert events.headers["Access-Control-Allow-Origin"] == "*"
        first = await asyncio.wait_for(events.content.readline(), 2)
        assert first == b"event: start\n"
        start = json.loads((await events.content.readline())[6:])
        assert start == {"position": pytest.approx(3.0)}
        await events.content.readline()

        # The backlog begins a little before the start: 2.5 s of the mix.
        line = await asyncio.wait_for(events.content.readline(), 2)
        frame = json.loads(line[6:])
        assert 2.5 <= frame["t"] < 2.5 + 2 / 30
        assert len(frame["b"]) == 2
        await events.content.readline()

        # And what is made after that comes, as it is made.
        for _ in range(180 - 75 - 1):
            await events.content.readline()
            await events.content.readline()
        feed.publish([spectrum.Frame(99.0, (0.5, 0.5))])
        line = await asyncio.wait_for(events.content.readline(), 2)
        assert json.loads(line[6:])["t"] == 99.0
        events.close()
        stream.close()

        # A page that didn't name itself gets the new frames, no position.
        anonymous = await client.get("/api/spectrum")
        assert await anonymous.content.readline() == b"event: start\n"
        assert json.loads((await anonymous.content.readline())[6:]) == {
            "position": None
        }
        anonymous.close()


@pytest.mark.asyncio
async def test_the_mix_is_analyzed_as_it_is_encoded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_dj import FakeBackend, make_radio  # noqa: PLC0415

    async def mix(*_: object) -> AsyncIterator[tuple[np.ndarray, str]]:
        for i in range(10_000):
            yield tone(1000.0, 0.25), f"t{i}"

    monkeypatch.setattr(dj, "mix", mix)
    radio = make_radio(tmp_path, monkeypatch, FakeBackend([]), {})
    hub, feed = dj.Broadcaster(), spectrum.SpectrumFeed()
    queue, _ = feed.subscribe()
    listener = hub.subscribe()
    task = asyncio.create_task(
        dj._encode_loop(radio, hub, 0.0, None, feed)  # noqa: SLF001
    )
    try:
        frames = await asyncio.wait_for(queue.get(), 5)
        await asyncio.wait_for(listener.get(), 5)
        assert frames[0].t == pytest.approx(spectrum.WINDOW / 2 / SAMPLE_RATE)
        assert hub.fed >= frames[-1].t
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
