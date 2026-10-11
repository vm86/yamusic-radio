"""The spectrum of the mix, a few bands many times a second, for the page.

The radio mixes the sound itself, so it can tell how loud each band is at
every moment. The page can't analyze the stream it plays (that needs CORS,
and it hears the sound ~3 s after the mix made it), so each value comes with
its position in the stream, and the page shows it when its player gets there.
"""

from __future__ import annotations

import contextlib
import json
from asyncio import Event, Queue, QueueFull, timeout
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from yamusic_radio.dj_mix import SAMPLE_RATE

if TYPE_CHECKING:
    import numpy.typing as npt

BANDS = 12
WINDOW = 2048
# ~30 values a second.
HOP = SAMPLE_RATE // 30
LOW_HZ = 30.0
HIGH_HZ = 16000.0
# A band is 0 at this level (dBFS) and 1 at full scale.
FLOOR_DB = -60.0
# Music falls off towards the treble: lift it so that the right bars move.
TILT_DB_PER_OCTAVE = 3.0
TILT_FROM_HZ = 500.0
# The frames kept for the listeners that join: more than the preroll and how
# far ahead of the listeners the mix runs.
HISTORY_FRAMES = SAMPLE_RATE // HOP * 12
CLIENT_QUEUE_SIZE = 64
STARTS_KEPT = 256


@dataclass(frozen=True)
class Frame:
    """The bands at a position in the stream (seconds of the mix)."""

    t: float
    bands: tuple[float, ...]

    def to_json(self) -> str:
        return json.dumps(
            {"t": round(self.t, 3), "b": [round(v, 3) for v in self.bands]},
            separators=(",", ":"),
        )


class SpectrumAnalyzer:
    """Cuts the mix into windows, as it is fed, and gives their bands."""

    def __init__(self) -> None:
        self._buffer: npt.NDArray[np.float32] = np.zeros(0, dtype=np.float32)
        # The index of the first sample of the buffer in the mix.
        self._origin = 0
        self._window = np.hanning(WINDOW).astype(np.float32)
        freqs = np.fft.rfftfreq(WINDOW, 1 / SAMPLE_RATE)
        edges = np.geomspace(LOW_HZ, HIGH_HZ, BANDS + 1)
        first = np.searchsorted(freqs, edges[:-1])
        last = np.maximum(np.searchsorted(freqs, edges[1:]), first + 1)
        self._ranges = list(zip(first.tolist(), last.tolist(), strict=True))
        centers = np.sqrt(edges[:-1] * edges[1:])
        self._tilt = TILT_DB_PER_OCTAVE * np.log2(centers / TILT_FROM_HZ)

    @property
    def position(self) -> float:
        """Seconds of the mix that were fed."""
        return (self._origin + len(self._buffer)) / SAMPLE_RATE

    def feed(self, pcm: npt.NDArray[np.float32]) -> list[Frame]:
        """Take the next piece of the mix, return the bands of its windows."""
        mono = pcm.mean(axis=1, dtype=np.float32)
        self._buffer = np.concatenate([self._buffer, mono])
        if len(self._buffer) < WINDOW:
            return []
        windows = np.lib.stride_tricks.sliding_window_view(
            self._buffer, WINDOW
        )[::HOP]
        # A sine of amplitude 1 gives 1 here (the Hann window passes half).
        amplitude = np.abs(np.fft.rfft(windows * self._window, axis=1)) / (
            WINDOW / 4
        )
        peaks = np.stack(
            [amplitude[:, lo:hi].max(axis=1) for lo, hi in self._ranges],
            axis=1,
        )
        db = 20 * np.log10(np.maximum(peaks, 1e-6)) + self._tilt
        levels = np.clip((db - FLOOR_DB) / -FLOOR_DB, 0.0, 1.0)
        frames = [
            Frame(
                (self._origin + i * HOP + WINDOW / 2) / SAMPLE_RATE,
                tuple(float(v) for v in row),
            )
            for i, row in enumerate(levels)
        ]
        used = len(windows) * HOP
        self._buffer = self._buffer[used:]
        self._origin += used
        return frames


class SpectrumFeed:
    """Hands the frames to the pages that watch them."""

    def __init__(self) -> None:
        self._frames: deque[Frame] = deque(maxlen=HISTORY_FRAMES)
        self._clients: set[Queue[list[Frame]]] = set()

    def reset(self) -> None:
        """Forget the frames: the mix starts over, its positions with it."""
        self._frames.clear()

    def publish(self, frames: list[Frame]) -> None:
        if not frames:
            return
        self._frames.extend(frames)
        for queue in self._clients:
            # A page that can't keep up loses the oldest frames.
            with contextlib.suppress(QueueFull):
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait(frames)

    def subscribe(
        self, since: float | None = None
    ) -> tuple[Queue[list[Frame]], list[Frame]]:
        """Watch the frames: those from `since` on, then the new ones."""
        queue: Queue[list[Frame]] = Queue(CLIENT_QUEUE_SIZE)
        self._clients.add(queue)
        if since is None:
            return queue, []
        return queue, [f for f in self._frames if f.t >= since]

    def unsubscribe(self, queue: Queue[list[Frame]]) -> None:
        self._clients.discard(queue)


class StartPositions:
    """Where in the stream each listener's player starts.

    The page can't read the headers of the stream its <audio> plays, so it
    names itself in the address and asks here (with the same name).
    """

    def __init__(self) -> None:
        self._starts: OrderedDict[str, float] = OrderedDict()
        self._asked: dict[str, Event] = {}

    def set(self, name: str, position: float) -> None:
        self._starts[name] = position
        self._starts.move_to_end(name)
        while len(self._starts) > STARTS_KEPT:
            self._starts.popitem(last=False)
        if (event := self._asked.pop(name, None)) is not None:
            event.set()

    async def get(self, name: str, wait: float) -> float | None:
        """Return the start of a listener, waiting for it to connect."""
        if name not in self._starts:
            event = self._asked.setdefault(name, Event())
            try:
                async with timeout(wait):
                    await event.wait()
            except TimeoutError:
                self._asked.pop(name, None)
        return self._starts.get(name)
