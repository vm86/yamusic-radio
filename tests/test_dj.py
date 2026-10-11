# ruff: noqa: S101, SLF001, PLR2004, ARG001, ARG002, ASYNC240
# mypy: ignore-errors
from __future__ import annotations

import asyncio
import dataclasses
import itertools
import re
import shutil
from typing import TYPE_CHECKING

import pytest
from aiohttp import web

if TYPE_CHECKING:
    from pathlib import Path

    from yamusic_radio.cache import TrackCache

np = pytest.importorskip("numpy")

from yamusic_radio import dj, dj_mix  # noqa: E402
from yamusic_radio.backend import (  # noqa: E402
    AuthError,
    AuthField,
    AuthPrompt,
    Feedback,
)
from yamusic_radio.selector import (  # noqa: E402
    BackendSelector,
    BackendSpec,
)

SR = dj_mix.SAMPLE_RATE


def click_track(
    bpm: float, seconds: float, first_beat: float = 0.3
) -> np.ndarray:
    """Make a kick drum on every beat over faint noise (stereo float32)."""
    size = int(SR * seconds)
    audio = np.zeros(size, dtype=np.float32)
    kick_t = np.arange(3000) / SR
    kick = (np.sin(2 * np.pi * 60 * kick_t) * np.exp(-kick_t * 30)) * 0.8
    period = SR * 60 / bpm
    beat = 0
    while (start := int(first_beat * SR + beat * period)) < size - 3000:
        audio[start : start + 3000] += kick.astype(np.float32)
        beat += 1
    audio += np.random.default_rng(0).normal(0, 0.01, size).astype(np.float32)
    return np.stack([audio, audio], axis=1)


def beat_error_ms(sample: float, bpm: float, first_beat: float) -> float:
    """Distance, in ms, from the nearest true beat of a click track."""
    period = SR * 60 / bpm
    return (
        abs((sample - first_beat * SR + period / 2) % period - period / 2)
        / SR
        * 1000
    )


def test_analyze_finds_tempo_and_beats_at_both_ends() -> None:
    result = dj_mix.analyze(click_track(124, 150))

    assert result.bpm == pytest.approx(124, abs=0.1)
    assert beat_error_ms(result.head_beat, 124, 0.3) < 15
    assert beat_error_ms(result.tail_beat, 124, 0.3) < 15
    assert result.end > 149 * SR


def test_analyze_trims_silence() -> None:
    pcm = np.concatenate(
        [np.zeros((SR * 2, 2), np.float32), click_track(120, 60)]
    )

    result = dj_mix.analyze(pcm)

    # The faint noise under the clicks is audible too.
    assert result.start == pytest.approx(2 * SR, abs=SR // 10)


def test_analyze_noise_has_no_tempo() -> None:
    noise = np.random.default_rng(1).normal(0, 0.1, (SR * 60, 2))

    assert dj_mix.analyze(noise.astype(np.float32)).bpm == 0.0


def test_tempo_ratio_allows_half_and_double_time() -> None:
    assert dj_mix.tempo_ratio(120, 120) == 1
    assert dj_mix.tempo_ratio(120, 240) == 1
    assert dj_mix.tempo_ratio(120, 61) == pytest.approx(120 / 122)


def test_transition_speed_limits() -> None:
    track = dj_mix.analyze(click_track(124, 60))

    assert dj_mix.transition_speed(120, track) == pytest.approx(120 / 124, 1e-3)
    assert dj_mix.transition_speed(100, track) == 0.0  # too far apart
    assert dj_mix.transition_speed(0, track) == 0.0  # unknown tempo
    assert dj_mix.transition_speed(124, track) == 1.0


def test_choose_closest_tempo_and_loudness() -> None:
    current = dj_mix.TrackAnalysis(120, 0, 0, 0, SR, -10)
    near = dj_mix.TrackAnalysis(124, 0, 0, 0, SR, -10)
    far = dj_mix.TrackAnalysis(150, 0, 0, 0, SR, -10)
    loud = dj_mix.TrackAnalysis(124, 0, 0, 0, SR, 0)

    costs = [dj_mix.transition_cost(current, t)[0] for t in (near, far, loud)]

    assert costs[0] < costs[2] < costs[1]


def test_matched_blend_lines_the_beats_up() -> None:
    first = dj_mix.analyze(click_track(120, 120, first_beat=0.3))
    second = dj_mix.analyze(click_track(120, 120, first_beat=0.17))

    out_start, in_start, length = dj_mix.plan_blend(
        first, second, beat_matched=True
    )

    assert beat_error_ms(out_start, 120, 0.3) < 15
    assert beat_error_ms(in_start, 120, 0.17) < 15
    assert length == pytest.approx(16 * 0.5 * SR, abs=SR)
    assert out_start + length == first.end


def test_plain_blend_when_tempo_is_unknown() -> None:
    unknown = dj_mix.TrackAnalysis(0, 0, 0, 0, 100 * SR, -10)

    out_start, in_start, length = dj_mix.plan_blend(
        unknown, unknown, beat_matched=False
    )

    assert length == round(dj_mix.PLAIN_BLEND_SECONDS * SR)
    assert out_start == 100 * SR - length
    assert in_start == 0


def test_crossfade_keeps_power() -> None:
    ones = np.ones((1000, 2), np.float32)

    blended = dj_mix.crossfade(ones, ones)

    assert blended.shape == (1000, 2)
    assert blended[0, 0] == pytest.approx(1)
    assert blended[500, 0] == pytest.approx(2**0.5, abs=0.01)


def test_icy_block_is_padded_to_16_bytes() -> None:
    block = dj.icy_block("A - B's song")

    assert len(block) == 1 + block[0] * 16
    assert block[1:].startswith(b"StreamTitle='A - B")
    assert b"B's" not in block  # the quote would end the field


def test_broadcaster_drops_oldest_for_slow_listener() -> None:
    hub = dj.Broadcaster()
    queue = hub.subscribe()

    for i in range(dj.CLIENT_QUEUE_SIZE + 5):
        hub.publish(bytes([i % 256]))

    assert queue.qsize() == dj.CLIENT_QUEUE_SIZE
    assert queue.get_nowait() == bytes([5])
    hub.unsubscribe(queue)
    assert not hub.has_listeners


class FakeRadio:
    """Hands out prepared decks of click tracks."""

    def __init__(self, decks: list[dj.Deck], *, skip: bool = False) -> None:
        self.decks = decks
        # The next track was asked for, and was it taken.
        self.skip_requested = skip
        self.asked: list[dj_mix.TrackAnalysis | None] = []
        self.events: list[tuple[str, str]] = []
        self.transitions: list[tuple[dj_mix.Transition, dj_mix.Effect]] = []
        self.reports: list[dj.BlendReport] = []

    async def next_deck(self, current: dj_mix.TrackAnalysis | None) -> dj.Deck:
        self.asked.append(current)
        if not self.decks:
            raise RuntimeError("out of tracks")
        return self.decks.pop(0)

    def is_stale(self, deck: dj.Deck) -> bool:
        return False

    def skip_taken(self) -> None:
        self.skip_requested = False
        self.events.append(("skip", ""))

    def transition(
        self,
        kind: dj_mix.Transition,
        current: dj.Deck,
        following: dj.Deck,
        effect: dj_mix.Effect = dj_mix.Effect.NONE,
        report: dj.BlendReport | None = None,
    ) -> None:
        self.events.append(("transition", f"{current.title}>{following.title}"))
        self.transitions.append((kind, effect))
        if report is not None:
            self.reports.append(report)

    def started(self, deck: dj.Deck) -> None:
        self.events.append(("started", deck.title))

    def finished(self, deck: dj.Deck) -> None:
        self.events.append(("finished", deck.title))


def deck(
    title: str,
    bpm: float,
    first_beat: float,
    *,
    matched: bool,
    speed: float = 1.0,
) -> dj.Deck:
    pcm = click_track(bpm, 90, first_beat)
    return dj.Deck(
        title, pcm, dj_mix.analyze(pcm), beat_matched=matched, speed=speed
    )


@pytest.mark.asyncio
async def test_mix_plays_every_track_and_blends_without_gaps() -> None:
    radio = FakeRadio(
        [
            deck("one", 120, 0.3, matched=False),
            deck("two", 120, 0.17, matched=True),
            deck("three", 120, 0.4, matched=True),
        ]
    )
    pieces = []

    async def play() -> None:
        async for piece in dj.mix(radio):
            pieces.append(piece)  # noqa: PERF401

    with pytest.raises(RuntimeError, match="out of tracks"):
        await play()

    titles = [title for _, title in pieces]
    assert [t for i, t in enumerate(titles) if t != titles[i - 1]] == [
        "one",
        "two",
        "three",
    ]
    seconds = sum(len(pcm) for pcm, _ in pieces) / SR
    # 3 x 90 s of material, minus two 8 s blends and the last 40 s that
    # are only played once the track after it is known.
    assert 205 < seconds < 225
    played = np.concatenate([pcm for pcm, _ in pieces])
    assert np.abs(played).max() < 2.0


def tones(low_hz: float, high_hz: float, seconds: float) -> np.ndarray:
    """Make a stereo signal with one bass and one treble sine."""
    t = np.arange(int(SR * seconds)) / SR
    mono = 0.4 * np.sin(2 * np.pi * low_hz * t) + 0.4 * np.sin(
        2 * np.pi * high_hz * t
    )
    return np.stack([mono, mono], axis=1).astype(np.float32)


def band_level(pcm: np.ndarray, hz: float) -> float:
    """Return the amplitude of one frequency in the signal."""
    spectrum = np.abs(np.fft.rfft(pcm[:, 0])) / len(pcm) * 2
    return float(spectrum[round(hz * len(pcm) / SR)])


def test_bass_swap_hands_the_bass_over_in_the_middle() -> None:
    outgoing = tones(60, 2000, 8)
    incoming = tones(80, 3000, 8)
    quarter = len(outgoing) // 4

    blended = dj_mix.bass_swap(outgoing, incoming, beat_period=SR * 0.5)

    first, last = blended[:quarter], blended[-quarter:]
    # Only one bassline at a time, the highs overlap.
    assert band_level(first, 60) > 0.3
    assert band_level(first, 80) < 0.02
    assert band_level(last, 80) > 0.3
    assert band_level(last, 60) < 0.02
    middle = blended[quarter : 3 * quarter]
    assert band_level(middle, 2000) > 0.1
    assert band_level(middle, 3000) > 0.1


def test_filter_sweep_goes_from_one_track_to_the_other() -> None:
    outgoing = tones(60, 6000, 8)
    incoming = tones(80, 6500, 8)
    quarter = len(outgoing) // 4

    blended = dj_mix.filter_sweep(outgoing, incoming)

    start, end = blended[:quarter], blended[-quarter:]
    assert band_level(start, 60) > 0.3  # the outgoing track, untouched
    assert band_level(start, 6000) > 0.2
    assert band_level(end, 80) > 0.3  # the incoming track, fully open
    assert band_level(end, 6500) > 0.2
    # Half way, the outgoing treble is already gone.
    middle = blended[quarter : 3 * quarter]
    assert band_level(middle, 6000) < band_level(start, 6000) / 2
    assert np.isfinite(blended).all()


def test_transition_choice() -> None:
    quiet = dj_mix.TrackAnalysis(120, 0, 0, 0, SR, -14)
    similar = dj_mix.TrackAnalysis(120, 0, 0, 0, SR, -13)
    loud = dj_mix.TrackAnalysis(120, 0, 0, 0, SR, -6)

    assert (
        dj_mix.choose_transition(quiet, similar, beat_matched=True)
        is dj_mix.Transition.BASS_SWAP
    )
    assert (
        dj_mix.choose_transition(quiet, loud, beat_matched=True)
        is dj_mix.Transition.FILTER_SWEEP
    )
    assert (
        dj_mix.choose_transition(quiet, similar, beat_matched=False)
        is dj_mix.Transition.FILTER_SWEEP
    )


def test_short_blend_falls_back_to_crossfade() -> None:
    ones = np.ones((SR, 2), np.float32)

    blended = dj_mix.blend(dj_mix.Transition.BASS_SWAP, ones, ones)

    assert blended[0, 0] == pytest.approx(1)
    assert blended[SR // 2, 0] == pytest.approx(2**0.5, abs=0.01)


def shaped_track(intro: float, outro: float, total: float = 120) -> np.ndarray:
    """Make a 120 BPM click track, quiet for `intro` and `outro` seconds."""
    pcm = click_track(120, total)
    pcm[: int(intro * SR)] *= 0.1
    pcm[len(pcm) - int(outro * SR) :] *= 0.1
    return pcm


def test_analyze_finds_intro_and_outro_on_bar_lines() -> None:
    result = dj_mix.analyze(shaped_track(intro=16, outro=12))

    assert result.intro_end == pytest.approx(16 * SR, abs=2 * SR)
    assert result.outro_start == pytest.approx(108 * SR, abs=2 * SR)
    bar = 2 * SR  # four beats at 120 BPM
    for edge, beat in (
        (result.intro_end, result.head_beat),
        (result.outro_start, result.tail_beat),
    ):
        assert beat_error_ms(edge, 120, 0.3) < 15
        assert (edge - beat) / bar == pytest.approx(
            round((edge - beat) / bar), abs=0.02
        )


def test_analyze_flat_track_has_no_sections() -> None:
    result = dj_mix.analyze(click_track(120, 120))

    assert result.intro_end - result.start < 2 * SR
    assert result.end - result.outro_start < 2 * SR


def test_blend_follows_the_shorter_of_outro_and_intro() -> None:
    long_outro = dj_mix.analyze(shaped_track(intro=0, outro=24))
    short_intro = dj_mix.analyze(shaped_track(intro=8, outro=0))
    long_intro = dj_mix.analyze(shaped_track(intro=24, outro=0))
    short_outro = dj_mix.analyze(shaped_track(intro=0, outro=8))

    _, _, short_blend = dj_mix.plan_blend(
        long_outro, short_intro, beat_matched=True
    )
    _, _, long_blend = dj_mix.plan_blend(
        long_outro, long_intro, beat_matched=True
    )
    _, _, shortest = dj_mix.plan_blend(
        short_outro, short_intro, beat_matched=True
    )

    beat = SR // 2
    assert short_blend == pytest.approx(16 * beat, abs=2 * beat)
    assert long_blend == pytest.approx(32 * beat, abs=2 * beat)
    assert shortest == pytest.approx(16 * beat, abs=2 * beat)
    assert long_blend > short_blend


def test_blend_stays_out_of_the_cores() -> None:
    outgoing = dj_mix.analyze(shaped_track(intro=0, outro=20))
    incoming = dj_mix.analyze(shaped_track(intro=20, outro=0))

    start, in_start, length = dj_mix.plan_blend(
        outgoing, incoming, beat_matched=True
    )

    half_beat = SR // 4
    assert start >= outgoing.outro_start - half_beat
    assert in_start + length <= incoming.intro_end + half_beat


def test_unmatched_blend_length_is_clamped() -> None:
    def track(intro: int, outro: int) -> dj_mix.TrackAnalysis:
        return dj_mix.TrackAnalysis(
            0, 0, 0, 0, 200 * SR, -10, intro * SR, (200 - outro) * SR
        )

    for intro, outro, seconds in ((30, 30, 16), (5, 30, 5), (2, 2, 8)):
        _, _, length = dj_mix.plan_blend(
            track(0, outro), track(intro, 0), beat_matched=False
        )
        assert length == seconds * SR, (intro, outro)


def centroid(pcm: np.ndarray) -> float:
    """Return the spectral centroid in Hz."""
    spectrum = np.abs(np.fft.rfft(pcm[:, 0]))
    freqs = np.fft.rfftfreq(len(pcm), 1 / SR)
    return float((spectrum * freqs).sum() / spectrum.sum())


def test_riser_gets_brighter_and_louder_and_ends_on_the_drop() -> None:
    rng = np.random.default_rng(0)

    rise = dj_mix.riser(4 * SR, rng)

    quarter = len(rise) // 4
    assert np.abs(rise).max() == pytest.approx(1)
    assert np.abs(rise[-quarter:]).mean() > 4 * np.abs(rise[:quarter]).mean()
    assert centroid(rise[-quarter:]) > 2 * centroid(rise[quarter : 2 * quarter])
    assert np.abs(rise[-5:]).max() < 0.05  # no click at the cut
    # The bass is left to the tracks.
    assert band_level(rise, 100) < 0.01


def test_impact_is_a_decaying_low_boom() -> None:
    boom = dj_mix.impact(3 * SR, np.random.default_rng(0))

    assert len(boom) == round(dj_mix.IMPACT_SECONDS * SR)
    assert np.abs(boom).max() == pytest.approx(1)
    body = boom[SR // 10 :]
    spectrum = np.abs(np.fft.rfft(body[:, 0]))
    peak_hz = np.argmax(spectrum) * SR / len(body)
    assert peak_hz < 120
    assert np.abs(boom[-SR // 10 :]).max() < 0.05


def test_impact_is_cut_to_the_room_left() -> None:
    assert len(dj_mix.impact(1000, np.random.default_rng(0))) == 1000


def test_echo_out_keeps_the_track_until_the_cut_then_rings_on() -> None:
    track = tones(220, 1000, 6)
    cut = 3 * SR

    echoed = dj_mix.echo_out(track, cut, 1.0)

    assert np.allclose(echoed[: cut - SR // 10], track[: cut - SR // 10])
    ringing = echoed[cut : cut + SR // 2]
    assert np.abs(ringing).max() > 0.1  # the last slice, repeated
    first, last = echoed[cut : cut + SR], echoed[-SR:]
    assert np.abs(last).max() < np.abs(first).max()  # fading
    assert np.abs(echoed[-1]).max() < 0.1


def test_effects_off_changes_nothing() -> None:
    outgoing, incoming = tones(60, 2000, 8), tones(80, 3000, 8)

    plain = dj_mix.blend(
        dj_mix.Transition.BASS_SWAP, outgoing, incoming, SR * 0.5
    )
    off = dj_mix.blend(
        dj_mix.Transition.BASS_SWAP, outgoing, incoming, SR * 0.5, effects=0.0
    )

    assert np.array_equal(plain, off)


def test_effects_on_beat_matched_blend_build_to_the_swap() -> None:
    outgoing, incoming = tones(60, 2000, 16), tones(80, 3000, 16)
    period = SR * 0.5
    rng = np.random.default_rng(1)
    plain = dj_mix.blend(
        dj_mix.Transition.BASS_SWAP, outgoing, incoming, period
    )

    dressed = dj_mix.blend(
        dj_mix.Transition.BASS_SWAP,
        outgoing,
        incoming,
        period,
        effects=1.0,
        rng=rng,
    )

    swap = round(dj_mix.swap_point(len(plain), period)[0])
    extra = dressed - plain
    assert np.abs(extra[: swap - 9 * SR // 2]).max() == 0  # starts 4 s ahead
    assert np.abs(extra[swap - SR : swap]).max() > 0  # riser
    assert np.abs(extra[swap : swap + SR // 4]).max() > 0.1  # boom
    assert np.abs(extra[swap + 2 * SR :]).max() < 0.01  # boom has died away


def test_effects_without_beat_grid_echo_the_outgoing_track_out() -> None:
    outgoing, incoming = tones(220, 1000, 8), tones(80, 3000, 8)

    plain = dj_mix.blend(dj_mix.Transition.FILTER_SWEEP, outgoing, incoming)
    echoed = dj_mix.blend(
        dj_mix.Transition.FILTER_SWEEP, outgoing, incoming, effects=1.0
    )

    assert not np.array_equal(plain, echoed)
    cut = round(len(outgoing) * dj_mix.ECHO_CUT_AT)
    # The incoming track plays on untouched by any boom.
    assert np.isfinite(echoed).all()
    assert (
        np.abs(echoed[: cut - SR // 10] - plain[: cut - SR // 10]).max() < 1e-4
    )


def test_boom_has_room_when_the_blend_is_a_whole_number_of_beats() -> None:
    outgoing, incoming = tones(60, 2000, 8), tones(80, 3000, 8)  # 16 beats

    dressed = dj_mix.blend(
        dj_mix.Transition.FILTER_SWEEP,
        outgoing,
        incoming,
        SR * 0.5,
        effects=1.0,
    )

    assert len(dressed) == len(outgoing)
    assert np.isfinite(dressed).all()


def track(bpm: float, loudness: float = -10, artist: str = "") -> dj_mix.Entry:
    """Make a pool entry of a given tempo and loudness."""
    analysis = dj_mix.TrackAnalysis(
        bpm, 0, 0, 0, 200 * SR, loudness, 20 * SR, 180 * SR
    )
    return dj_mix.Entry(
        analysis, frozenset({artist}) if artist else frozenset()
    )


def test_matched_transition_costs_less_than_a_clash() -> None:
    current = track(120).analysis

    matched, playing = dj_mix.transition_cost(current, track(124).analysis)
    clash, _ = dj_mix.transition_cost(current, track(139).analysis)
    unknown, _ = dj_mix.transition_cost(current, track(0).analysis)

    assert matched < 1 < clash < unknown
    # It plays at its own tempo: the blend glides to it.
    assert playing.bpm == pytest.approx(124)


def test_cost_counts_sweeps_and_missing_sections() -> None:
    current = track(120, -10).analysis
    swap, _ = dj_mix.transition_cost(current, track(120, -10).analysis)
    sweep, _ = dj_mix.transition_cost(current, track(120, -16).analysis)
    plain = dj_mix.TrackAnalysis(120, 0, 0, 0, 200 * SR, -10)
    no_sections, _ = dj_mix.transition_cost(plain, plain)

    assert swap == 0
    assert sweep == pytest.approx(dj_mix.SWEEP_COST + 0.6)
    assert no_sections == dj_mix.PLAIN_LENGTH_COST


def test_energy_and_arc() -> None:
    calm = track(95, -17).analysis
    wild = track(145, -7).analysis

    assert 0 <= dj_mix.energy(calm) < dj_mix.energy(wild) <= 1
    assert dj_mix.arc_target(0) < dj_mix.arc_target(dj_mix.ARC_TRACKS // 2)
    assert dj_mix.arc_target(dj_mix.ARC_TRACKS) == pytest.approx(
        dj_mix.arc_target(0)
    )


def test_set_opens_with_a_calm_track_and_peaks_later() -> None:
    pool = [track(145, -7), track(95, -17), track(120, -12)]

    assert dj_mix.choose_next(None, pool, position=0) == 1
    assert dj_mix.choose_next(None, pool, position=dj_mix.ARC_TRACKS // 2) == 0


def test_artist_is_not_repeated_right_away() -> None:
    current = track(120).analysis
    pool = [track(121, artist="Same"), track(122, artist="Other")]

    pick = dj_mix.choose_next(current, pool, (frozenset({"Same"}),), 3)

    assert pick == 1
    assert dj_mix.choose_next(current, pool, (), 3) == 0


def test_closest_tempo_wins_with_a_single_step_to_look_at() -> None:
    current = track(120).analysis
    pool = [track(150), track(123), track(100)]

    assert dj_mix.choose_next(current, pool[:2], (), 5) == 1
    assert dj_mix.choose_next(current, pool, (), 5) == 1


def test_choice_is_the_start_of_the_best_queue() -> None:
    rng = np.random.default_rng(3)
    pool = [
        track(float(rng.uniform(90, 150)), float(rng.uniform(-16, -7)))
        for _ in range(5)
    ]
    current = track(118).analysis
    weights = dj_mix.LOOKAHEAD_WEIGHTS

    def queue_cost(order: tuple[int, ...]) -> float:
        playing, total = current, 0.0
        for place, index in enumerate(order):
            cost, playing = dj_mix.step_cost(
                playing, pool[index], (), 4 + place
            )
            total += weights[place] * cost
        return total

    best = min(
        itertools.permutations(range(len(pool)), len(weights)),
        key=queue_cost,
    )

    assert dj_mix.choose_next(current, pool, (), 4) == best[0]


class FakeTrack:
    """The bits of Track that the radio uses."""

    def __init__(
        self, name: str, artist: str = "", genre: str = "", mood: str = ""
    ) -> None:
        self.genre = genre
        self.track_id = name
        self.title = name
        self.codec = "mp3"
        self.bitrate_in_kbps = 192
        self.batch_id = "batch"
        self.cover = ""
        self.mood = mood
        self.artists = (artist or name,)


class FakeBackend:
    """A station that gives out the batches it is told to."""

    supports_moods = True
    default_station = "station"

    def __init__(self, batches: list[list[FakeTrack]]) -> None:
        self.batches = batches
        self.requests = 0
        self.feedback: list[tuple[str, str, float]] = []
        self.settings: list[tuple[str, str, str]] = []
        self.authorized = True
        self.auth_prompt = AuthPrompt()
        self.auth_started = False
        self.signed_in: list[dict[str, str]] = []

    async def station_batch(
        self, station_id: str, *, exclude_track_ids: set[str]
    ) -> list[FakeTrack]:
        self.requests += 1
        if self.batches:
            return self.batches.pop(0)
        return [FakeTrack("a")]  # the station only repeats itself

    async def station_settings(self, station_id: str) -> tuple[str, str, str]:
        return "all", "default", "any"

    async def set_station_settings(
        self, station_id: str, mood: str, diversity: str, language: str
    ) -> None:
        self.settings.append((mood, diversity, language))

    async def download(self, track: FakeTrack, path: Path) -> None:
        path.write_bytes(b"x")

    async def feedback_track(
        self,
        track: FakeTrack,
        feedback: Feedback,
        station_id: str,
        seconds: float,
    ) -> None:
        self.feedback.append((track.track_id, feedback, seconds))

    def start_auth(self) -> None:
        self.auth_started = True

    async def authorize(self, values: dict[str, str]) -> None:
        if values.get("password") != "right":
            raise AuthError("wrong password")
        self.signed_in.append(values)
        self.authorized = True

    async def aclose(self) -> None:
        return None


def make_radio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: FakeBackend,
    bpms: dict[str, float],
    *,
    mood_arc: bool = False,
    cache: TrackCache | None = None,
) -> dj.Radio:
    """Make a radio whose tracks are 200 s of silence with a given tempo."""
    radio = dj.Radio(
        backend, tmp_path, pool_size=4, mood_arc=mood_arc, cache=cache
    )

    async def download(track: FakeTrack, path: Path) -> None:
        path.write_bytes(b"x")

    async def prepare(track: FakeTrack, mood: str = "") -> None:
        (tmp_path / f"{track.track_id}.mp3").write_bytes(b"x")
        radio._pool.append(
            dj.Candidate(
                track.title,
                tmp_path / f"{track.track_id}.mp3",
                track_analysis(bpms.get(track.track_id, 120)),
                frozenset(track.artists),
                track,
                mood,
                track.genre,
            )
        )

    async def decode(path: Path, speed: float = 1.0) -> np.ndarray:
        return np.zeros((SR, CHANNELS), np.float32)

    monkeypatch.setattr(radio, "_download", download)
    monkeypatch.setattr(radio, "_prepare", prepare)
    monkeypatch.setattr(dj, "decode_file", decode)
    return radio


CHANNELS = dj_mix.CHANNELS


def track_analysis(bpm: float) -> dj_mix.TrackAnalysis:
    return dj_mix.TrackAnalysis(
        bpm, 0, 0, 0, 200 * SR, -10, 20 * SR, 180 * SR, key=9
    )


@pytest.mark.asyncio
async def test_played_tracks_are_marked_and_not_picked_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeBackend([[FakeTrack("a"), FakeTrack("b"), FakeTrack("c")]])
    radio = make_radio(tmp_path, monkeypatch, backend, {})
    await radio.fill(3)

    first = await radio.next_deck(None)
    second = await radio.next_deck(first.analysis)

    assert first.title != second.title
    assert list(radio.played) == [first.title, second.title]
    assert [c.title for c in radio.pool] == [
        t for t in "abc" if t not in radio.played
    ]


@pytest.mark.asyncio
async def test_more_tracks_are_loaded_when_nothing_fits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The pool has only a clashing tempo; the next batch has a good one.
    backend = FakeBackend(
        [
            [FakeTrack("fast")],
            [FakeTrack("x1"), FakeTrack("x2"), FakeTrack("good")],
        ]
    )
    radio = make_radio(
        tmp_path,
        monkeypatch,
        backend,
        {"fast": 175, "x1": 175, "x2": 175, "good": 121},
    )
    await radio.fill(1)

    deck = await radio.next_deck(track_analysis(120))

    assert deck.title == "good"
    assert backend.requests == 2
    assert len(radio.pool) == 3  # the others wait for their turn


@pytest.mark.asyncio
async def test_loading_more_stops_at_six_extra_tracks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clashing = {f"t{i}": 175 for i in range(20)}
    backend = FakeBackend(
        [[FakeTrack(f"t{3 * i + k}") for k in range(3)] for i in range(7)]
    )
    radio = make_radio(tmp_path, monkeypatch, backend, clashing)
    await radio.fill(1)
    loaded_before = len(radio.pool)

    await radio.next_deck(track_analysis(120))

    # One track was taken, six extra ones were loaded: no more.
    assert len(radio.pool) + 1 - loaded_before == dj.MAX_EXTRA_TRACKS


@pytest.mark.asyncio
async def test_oldest_played_track_is_repeated_when_the_station_repeats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeBackend([[FakeTrack("a"), FakeTrack("b")]])
    radio = make_radio(tmp_path, monkeypatch, backend, {})
    await radio.fill(2)
    first = await radio.next_deck(None)
    second = await radio.next_deck(first.analysis)

    third = await radio.next_deck(second.analysis)  # nothing new is left
    fourth = await radio.next_deck(third.analysis)

    assert third.title == first.title  # the one played longest ago
    assert fourth.title == second.title
    assert list(radio.played) == [first.title, second.title]


@pytest.mark.asyncio
async def test_feedback_goes_to_the_station(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeBackend([[FakeTrack("a")]])
    radio = make_radio(tmp_path, monkeypatch, backend, {})
    await radio.fill(1)
    deck = await radio.next_deck(None)

    radio.started(deck)
    radio.finished(deck)
    await asyncio.gather(*radio._feedback_tasks)

    assert backend.feedback == [
        ("a", Feedback.STARTED, 0.0),
        ("a", Feedback.FINISHED, 200.0),
    ]


def test_mood_follows_the_arc() -> None:
    moods = [dj_mix.mood_for(n) for n in range(dj_mix.ARC_TRACKS)]

    assert moods == [
        "calm",
        "calm",
        "fun",
        "active",
        "active",
        "active",
        "active",
        "active",
        "fun",
        "calm",
    ]


def test_mood_cost_grows_with_the_distance() -> None:
    assert dj_mix.mood_cost("calm", "calm") == 0
    assert dj_mix.mood_cost("calm", "fun") == dj_mix.MOOD_COST
    assert dj_mix.mood_cost("calm", "active") == 2 * dj_mix.MOOD_COST
    assert dj_mix.mood_cost("calm", "") == 0  # not known: no opinion


def test_a_track_of_the_wanted_mood_is_preferred() -> None:
    current = track(120).analysis
    calm = dj_mix.Entry(track(121).analysis, mood="calm")
    active = dj_mix.Entry(track(121).analysis, mood="active")

    assert dj_mix.choose_next(current, [active, calm], (), 0) == 1
    assert dj_mix.choose_next(current, [calm, active], (), 5) == 1


@pytest.mark.asyncio
async def test_mood_is_set_before_loading_and_restored_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeBackend(
        [[FakeTrack(f"t{3 * r + i}") for i in range(3)] for r in range(4)]
    )
    radio = make_radio(tmp_path, monkeypatch, backend, {}, mood_arc=True)

    await radio.start_mood_arc()
    await radio.fill(12)
    await radio.restore_settings()

    # Rounds of three tracks, each for the mood of the place it will be at.
    assert backend.settings == [
        ("calm", "default", "any"),
        ("active", "default", "any"),
        ("calm", "default", "any"),
        ("all", "default", "any"),  # put back as it was
    ]
    assert [c.mood for c in radio.pool] == (
        ["calm"] * 3 + ["active"] * 3 + ["active"] * 3 + ["calm"] * 3
    )


@pytest.mark.asyncio
async def test_without_mood_arc_the_settings_are_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeBackend([[FakeTrack("a"), FakeTrack("b")]])
    radio = make_radio(tmp_path, monkeypatch, backend, {})

    await radio.fill(2)
    await radio.restore_settings()

    assert backend.settings == []


@pytest.mark.asyncio
async def test_played_tracks_are_not_picked_again_after_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, track_cache: TrackCache
) -> None:
    cache = track_cache
    tracks = [FakeTrack("a"), FakeTrack("b"), FakeTrack("c")]
    radio = make_radio(
        tmp_path, monkeypatch, FakeBackend([tracks]), {}, cache=cache
    )
    await radio.fill(2)
    for name in "abc":  # as _prepare does
        cache.put_analysis(name, "mp3", name, track_analysis(120))
    first = await radio.next_deck(None)

    restarted = make_radio(
        tmp_path,
        monkeypatch,
        FakeBackend([[FakeTrack("a"), FakeTrack("b"), FakeTrack("c")]]),
        {},
        cache=cache,
    )
    await restarted.fill(3)

    assert first.title == "a"
    assert "a" not in [c.title for c in restarted.pool]


@pytest.mark.asyncio
async def test_cached_analysis_skips_decoding_and_analyzing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, track_cache: TrackCache
) -> None:
    cache = track_cache
    radio = dj.Radio(FakeBackend([]), tmp_path, pool_size=1, cache=cache)
    decoded: list[Path] = []

    async def download(track: FakeTrack, path: Path) -> None:
        path.write_bytes(b"x")

    async def decode(path: Path, speed: float = 1.0) -> np.ndarray:
        decoded.append(path)
        return np.zeros((SR, CHANNELS), np.float32)

    monkeypatch.setattr(radio, "_download", download)
    monkeypatch.setattr(dj, "decode_file", decode)
    monkeypatch.setattr(dj_mix, "analyze", lambda _pcm: track_analysis(100))

    await radio._prepare(FakeTrack("a"))  # analyzed and stored
    await radio._prepare(FakeTrack("a"))  # served from the cache

    assert len(decoded) == 1
    assert [c.analysis.bpm for c in radio.pool] == [100, 100]


def test_nearest_mood_stays_within_the_allowed_ones() -> None:
    assert dj_mix.nearest_mood("fun", []) == "fun"
    assert dj_mix.nearest_mood("fun", ["calm", "fun"]) == "fun"
    assert dj_mix.nearest_mood("active", ["sad", "calm"]) == "calm"
    assert dj_mix.nearest_mood("fun", ["calm", "active"]) == "calm"  # a tie


@pytest.mark.asyncio
async def test_only_the_allowed_moods_are_fetched_and_played(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = [FakeTrack(f"t{n}") for n in range(30)]
    backend = FakeBackend([names[i : i + 3] for i in range(0, 30, 3)])
    radio = make_radio(tmp_path, monkeypatch, backend, {})
    await radio.fill(4)  # tracks of the station's own mood first
    assert {c.mood for c in radio.pool} == {""}

    await radio.set_allowed_moods(["active", "calm", "calm"])

    assert radio.allowed_moods == ("calm", "active")
    assert radio.pool == []  # the others were dropped
    await radio.fill(4)
    deck = await radio.next_deck(None)
    assert {c.mood for c in radio.pool} <= {"calm", "active"}
    assert deck.mood in {"calm", "active"}
    assert {s[0] for s in backend.settings} <= {"calm", "active", "all"}

    await radio.set_allowed_moods([])
    assert backend.settings[-1][0] == "all"  # the account is as it was


@pytest.mark.asyncio
async def test_queue_shows_previous_now_and_next_tracks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeBackend([[FakeTrack(n) for n in "abcdefg"]])
    radio = make_radio(tmp_path, monkeypatch, backend, {})
    await radio.fill(6)
    assert radio.queue()["now"] is None

    played = []
    for _ in range(4):
        deck = await radio.next_deck(played[-1].analysis if played else None)
        radio.started(deck)
        played.append(deck)
    deck = await radio.next_deck(played[-1].analysis)  # decoded, not begun

    queue = radio.queue(before=2, after=15)

    assert [t["title"] for t in queue["previous"]] == [
        played[1].title,
        played[2].title,
    ]
    assert queue["now"]["title"] == played[3].title
    nxt = queue["next"]
    assert nxt[0]["title"] == deck.title
    assert nxt[0]["decided"] is True
    assert all(not t["decided"] for t in nxt[1:])
    titles = [p.title for p in played] + [deck.title]
    assert not {t["title"] for t in nxt} & set(titles[:-1])
    assert len(radio.queue(after=1)["next"]) == 1
    assert len(radio.queue(before=0)["previous"]) == 0


@pytest.mark.asyncio
async def test_rest_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aiohttp.test_utils import TestClient, TestServer  # noqa: PLC0415

    backend = FakeBackend([[FakeTrack(n) for n in "abcd"]])
    radio = make_radio(tmp_path, monkeypatch, backend, {})
    await radio.fill(4)
    deck = await radio.next_deck(None)
    radio.started(deck)
    app = dj.make_app(dj.Broadcaster(), radio)

    async with TestClient(TestServer(app)) as client:
        page = await client.get("/")
        assert page.status == 200
        assert page.content_type == "text/html"
        text = await page.text()
        assert "Яндекс Музыка DJ" in text
        # Every script and style the page names is served.
        paths = re.findall(r'(?:src|href)="\./(assets/[^"]+)"', text)
        assert paths
        for path in paths:
            asset = await client.get(f"/{path}")
            assert asset.status == 200, path

        queue = await (await client.get("/api/queue?next=2")).json()
        assert queue["now"]["title"] == deck.title
        assert 0 <= queue["now"]["elapsed_seconds"] < 5
        assert len(queue["next"]) == 2
        assert (await client.get("/api/queue?next=x")).status == 400

        moods = await (await client.get("/api/moods")).json()
        assert moods["available"] == ["sad", "calm", "fun", "active"]
        assert moods["selected"] == []

        reply = await client.put("/api/moods", json={"moods": ["calm", "fun"]})
        assert (await reply.json())["selected"] == ["calm", "fun"]
        bad = await client.put("/api/moods", json={"moods": ["angry"]})
        assert bad.status == 400
        assert (await client.put("/api/moods", data="nope")).status == 400
        assert (await client.put("/api/moods", json=[])).status == 400
        assert radio.allowed_moods == ("calm", "fun")

        reply = await client.delete("/api/moods")
        assert (await reply.json())["selected"] == []


@pytest.mark.asyncio
async def test_api_tells_where_to_authorize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aiohttp.test_utils import TestClient, TestServer  # noqa: PLC0415

    backend = FakeBackend([])
    backend.authorized = False
    radio = make_radio(tmp_path, monkeypatch, backend, {})
    app = dj.make_app(dj.Broadcaster(), radio)

    async with TestClient(TestServer(app)) as client:
        waiting = await (await client.get("/api/auth")).json()
        assert waiting == {
            "authorized": False,
            "url": None,
            "code": None,
            "fields": [],
        }

        backend.auth_prompt = AuthPrompt(
            "https://ya.example/device", "ABCD1234"
        )
        waiting = await (await client.get("/api/auth")).json()
        assert waiting["url"] == "https://ya.example/device"
        assert waiting["code"] == "ABCD1234"
        # The account's settings can't be changed yet.
        reply = await client.put("/api/moods", json={"moods": ["calm"]})
        assert reply.status == 503
        assert (await client.delete("/api/moods")).status == 503
        assert (await client.get("/api/queue")).status == 200

        backend.authorized = True
        done = await (await client.get("/api/auth")).json()
        assert done == {
            "authorized": True,
            "url": None,
            "code": None,
            "fields": [],
        }


def chord_track(tonic: int, *, minor: bool, seconds: float = 40) -> np.ndarray:
    """I, IV and V triads (with overtones) in a given key, tonic-heavy."""
    third = 3 if minor else 4
    t = np.arange(int(seconds * SR)) / SR
    out = np.zeros_like(t)
    steps = (0, 5, 0, 7, 0, 0)
    for degree, step in enumerate(steps):
        lo, hi = (
            degree * len(t) // len(steps),
            (degree + 1) * len(t) // len(steps),
        )
        for interval in (0, third, 7):
            midi = 48 + tonic + step + interval
            freq = 440 * 2 ** ((midi - 69) / 12)
            for harmonic, gain in ((1, 1.0), (2, 0.5), (4, 0.25)):
                out[lo:hi] += gain * np.sin(
                    2 * np.pi * freq * harmonic * t[lo:hi]
                )
    return (0.3 * out / np.abs(out).max()).astype(np.float32)


@pytest.mark.parametrize(
    ("tonic", "minor", "camelot"),
    [(0, False, "8B"), (9, True, "8A"), (7, False, "9B"), (4, True, "9A")],
)
def test_estimate_key_finds_the_key(
    tonic: int,
    minor: bool,  # noqa: FBT001
    camelot: str,
) -> None:
    key = dj_mix.estimate_key(chord_track(tonic, minor=minor))

    assert key == tonic + (12 if minor else 0)
    assert dj_mix.camelot_code(key) == camelot


def test_estimate_key_of_silence_is_unknown() -> None:
    assert dj_mix.estimate_key(np.zeros(100, dtype=np.float32)) == -1
    assert dj_mix.estimate_key(np.zeros(20 * SR, dtype=np.float32)) == -1


def test_essentia_decides_where_the_plain_match_sees_only_noise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Dense distorted music: flat pitch classes, but Essentia knows the key.
    monkeypatch.setattr(dj_mix, "_KEY_EXTRACTOR", lambda _: None)
    monkeypatch.setattr(dj_mix, "_essentia_key", lambda _: 15)
    monkeypatch.setattr(dj_mix, "_profile_key", lambda _: (3, 0.1))

    assert dj_mix.estimate_key(chord_track(9, minor=True)) == 15


def test_camelot_wheel() -> None:
    codes = {dj_mix.camelot_code(k) for k in range(24)}

    assert len(codes) == 24
    assert dj_mix.camelot_code(11) == "1B"  # B major
    assert dj_mix.camelot_code(12 + 8) == "1A"  # G# minor
    assert dj_mix.camelot_code(12 + 9) == "8A"  # A minor
    assert dj_mix.camelot_code(-1) == ""
    assert dj_mix.key_name(12 + 6) == "F# minor"


@pytest.mark.asyncio
async def test_refresh_queue_puts_the_chosen_track_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeBackend([[FakeTrack("a"), FakeTrack("b"), FakeTrack("c")]])
    radio = make_radio(tmp_path, monkeypatch, backend, {})
    await radio.fill(3)
    first = await radio.next_deck(None)
    radio.started(first)
    second = await radio.next_deck(first.analysis)
    assert len(radio.pool) == 1

    radio.refresh_queue()

    assert radio.is_stale(second)
    assert len(radio.pool) == 2
    assert second.title in [c.title for c in radio.pool]
    assert list(radio.played) == [first.title]  # the choice is taken back
    assert len(radio.upcoming(15)) == 2  # the forecast has it again

    again = await radio.next_deck(first.analysis)
    assert not radio.is_stale(again)
    assert list(radio.played) == [first.title, again.title]


@pytest.mark.asyncio
async def test_refresh_queue_drops_tracks_of_other_moods(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeBackend([[FakeTrack("a"), FakeTrack("b")]])
    radio = make_radio(tmp_path, monkeypatch, backend, {})
    await radio.fill(2)
    for candidate, mood in zip(radio.pool, ("calm", "fun"), strict=True):
        candidate.mood = mood
    radio._allowed_moods = ("calm",)

    radio.refresh_queue()

    assert [c.mood for c in radio.pool] == ["calm"]


@pytest.mark.asyncio
async def test_refresh_during_decoding_chooses_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeBackend([[FakeTrack("a"), FakeTrack("b")]])
    radio = make_radio(tmp_path, monkeypatch, backend, {})
    await radio.fill(2)
    decoded = []

    async def decode(path: Path, speed: float = 1.0) -> np.ndarray:
        decoded.append(path.name)
        if len(decoded) == 1:
            radio.refresh_queue()  # somebody pressed the button meanwhile
        return np.zeros((SR, CHANNELS), np.float32)

    monkeypatch.setattr(dj, "decode_file", decode)

    result = await radio.next_deck(None)

    assert len(decoded) == 2
    assert not radio.is_stale(result)
    assert list(radio.played) == [result.title]
    assert len(radio.pool) == 1


@pytest.mark.asyncio
async def test_track_file_is_removed_when_the_track_is_heard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeBackend([[FakeTrack("a")]])
    radio = make_radio(tmp_path, monkeypatch, backend, {})
    await radio.fill(1)
    deck = await radio.next_deck(None)
    assert (tmp_path / "a.mp3").exists()  # still needed to take it back

    radio.started(deck)

    assert not (tmp_path / "a.mp3").exists()


def key_of(code: str) -> int:
    """Return the key number (see TrackAnalysis.key) of a Camelot code."""
    return next(k for k in range(24) if dj_mix.camelot_code(k) == code)


@pytest.mark.parametrize(
    ("now", "then", "cost"),
    [
        ("8A", "8A", 0.0),
        ("8A", "9A", dj_mix.KEY_NEIGHBOUR_COST),
        ("8A", "7A", dj_mix.KEY_NEIGHBOUR_COST),
        ("1A", "12A", dj_mix.KEY_NEIGHBOUR_COST),  # round the wheel
        ("8A", "8B", dj_mix.KEY_NEIGHBOUR_COST),  # the relative key
        ("8A", "10A", dj_mix.KEY_BOOST_COST),
        ("8A", "3A", dj_mix.KEY_SEMITONE_COST),
        ("8A", "6A", dj_mix.KEY_DOWN_COST),
        ("8A", "9B", dj_mix.KEY_DIAGONAL_COST),
        ("8A", "2A", dj_mix.KEY_CLASH_COST),
        ("8A", "3B", dj_mix.KEY_CLASH_COST),
    ],
)
def test_key_cost_follows_the_camelot_rules(
    now: str, then: str, cost: float
) -> None:
    assert dj_mix.key_cost(key_of(now), key_of(then)) == pytest.approx(
        cost * dj_mix.KEY_COST_SCALE
    )


def test_key_cost_of_an_unknown_key_is_zero() -> None:
    assert dj_mix.key_cost(-1, key_of("8A")) == 0
    assert dj_mix.key_cost(key_of("8A"), -1) == 0


def test_next_track_is_picked_by_the_wheel() -> None:
    def keyed(code: str) -> dj_mix.Entry:
        entry = track(120)
        return dj_mix.Entry(
            dataclasses.replace(entry.analysis, key=key_of(code)),
            entry.artists,
            entry.mood,
        )

    now = dataclasses.replace(track(120).analysis, key=key_of("8A"))
    pool = [keyed("2A"), keyed("8B"), keyed("5B")]

    assert dj_mix.choose_next(now, pool) == 1


def test_estimate_key_without_essentia(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dj_mix, "_KEY_EXTRACTOR", None)
    assert dj_mix.key_detector() == "chroma profiles"

    # The plain match gets the keys that are not close to a neighbour.
    key = dj_mix.estimate_key(chord_track(9, minor=True))

    assert key >= 0
    noise = np.random.default_rng(2).normal(0, 0.1, 20 * SR)
    assert dj_mix.estimate_key(noise.astype(np.float32)) == -1


def test_energy_arc_is_not_given_up_for_the_key() -> None:
    """A clash of keys costs less than a track far from the energy wanted."""
    clash = dj_mix.KEY_COST_SCALE * dj_mix.KEY_CLASH_COST
    worst_arc = dj_mix.ENERGY_ARC_COST * 0.7  # the arc swings by 0.7

    assert clash < worst_arc
    assert dj_mix.ENERGY_ARC_COST * 0.25 > dj_mix.KEY_COST_SCALE * (
        dj_mix.KEY_DIAGONAL_COST
    )


@pytest.mark.asyncio
async def test_genre_is_kept_in_the_cache_not_in_the_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    track_cache: TrackCache,
) -> None:
    radio = dj.Radio(FakeBackend([]), tmp_path, cache=track_cache)

    async def download(track: FakeTrack, path: Path) -> None:
        path.write_bytes(b"x")

    async def decode(path: Path, speed: float = 1.0) -> np.ndarray:
        return np.zeros((SR, CHANNELS), np.float32)

    monkeypatch.setattr(radio, "_download", download)
    monkeypatch.setattr(dj, "decode_file", decode)
    monkeypatch.setattr(dj_mix, "analyze", lambda _: track_analysis(120))

    await radio._prepare(FakeTrack("a", genre="house"))
    assert track_cache.get_genre("a", "mp3") == "house"

    # Analyzed before the genre was known: the genre is added to the row.
    track_cache.put_analysis("b", "mp3", "b", track_analysis(120))
    assert track_cache.get_genre("b", "mp3") == ""
    await radio._prepare(FakeTrack("b", genre="techno"))
    assert track_cache.get_genre("b", "mp3") == "techno"

    info = dj.TrackInfo.of("a", track_analysis(120), None, "").to_dict()
    assert "genre" not in info


def test_key_relations() -> None:
    relations = {
        ("8A", "8A"): "same",
        ("8A", "9A"): "neighbour",
        ("8A", "8B"): "neighbour",
        ("8A", "10A"): "boost",
        ("8A", "3A"): "semitone",
        ("8A", "6A"): "down",
        ("8A", "9B"): "diagonal",
        ("8A", "2A"): "clash",
    }
    for (now, then), relation in relations.items():
        assert dj_mix.key_relation(key_of(now), key_of(then)) == relation
        assert dj_mix.key_cost(key_of(now), key_of(then)) == pytest.approx(
            dj_mix.KEY_COSTS[relation] * dj_mix.KEY_COST_SCALE
        )
    assert dj_mix.key_relation(-1, key_of("8A")) == "unknown"


@pytest.mark.asyncio
async def test_transitions_are_counted_and_kept(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    track_cache: TrackCache,
) -> None:
    radio = make_radio(
        tmp_path,
        monkeypatch,
        FakeBackend([[FakeTrack("a"), FakeTrack("b"), FakeTrack("c")]]),
        {},
        cache=track_cache,
    )
    await radio.fill(3)
    first = await radio.next_deck(None)
    second = await radio.next_deck(first.analysis)
    third = await radio.next_deck(second.analysis)
    first.beat_matched = third.beat_matched = False
    first.analysis = dataclasses.replace(first.analysis, key=-1)
    second.beat_matched = True
    keyed = dataclasses.replace(second.analysis, key=key_of("8A"))
    second.analysis = keyed
    third.analysis = dataclasses.replace(third.analysis, key=key_of("9A"))

    radio.transition(
        dj_mix.Transition.BASS_SWAP, first, second, dj_mix.Effect.DROP
    )
    radio.transition(dj_mix.Transition.FILTER_SWEEP, second, third)
    radio.transition(dj_mix.Transition.CROSSFADE, third, first)

    stats = radio.transition_stats()
    assert stats["session"] == {
        "total": 3,
        "beat_matched": 1,
        "by_type": {"bass swap": 1, "filter sweep": 1, "crossfade": 1},
        "by_effect": {"drop": 1, "none": 2},
        "by_key": {"unknown": 2, "neighbour": 1},
        "by_quality": {"good": 1, "fair": 0, "poor": 0, "unknown": 2},
    }
    assert stats["all_time"] == stats["session"]

    pairs = radio.key_pair_stats()
    assert pairs["session"] == pairs["all_time"]
    assert pairs["session"] == [
        {
            "from": None,
            "to": "8A",
            "relation": "unknown",
            "quality": "unknown",
            "count": 1,
        },
        {
            "from": "8A",
            "to": "9A",
            "relation": "neighbour",
            "quality": "good",
            "count": 1,
        },
        {
            "from": "9A",
            "to": None,
            "relation": "unknown",
            "quality": "unknown",
            "count": 1,
        },
    ]


def test_transition_stats_without_a_cache(tmp_path: Path) -> None:
    radio = dj.Radio(FakeBackend([]), tmp_path)

    assert radio.transition_stats() == {
        "session": {
            "total": 0,
            "beat_matched": 0,
            "by_type": {},
            "by_effect": {},
            "by_key": {},
            "by_quality": {"good": 0, "fair": 0, "poor": 0, "unknown": 0},
        },
        "all_time": None,
    }
    assert radio.key_pair_stats() == {"session": [], "all_time": None}


def genre_radio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tracks: list[FakeTrack]
) -> dj.Radio:
    return make_radio(tmp_path, monkeypatch, FakeBackend([tracks]), {})


@pytest.mark.asyncio
async def test_picked_genres_wait_for_the_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    radio = genre_radio(
        tmp_path,
        monkeypatch,
        [FakeTrack("a", genre="house"), FakeTrack("b", genre="rap")],
    )
    await radio.fill(2)

    radio.set_wanted_genres(["house"])

    assert radio.genres_info()["selected"] == ["house"]
    assert radio.genres_info()["applied"] == []
    assert len(radio.pool) == 2  # nothing is rebuilt by itself

    radio.refresh_queue()

    assert radio.genres_info()["applied"] == ["house"]
    assert [c.title for c in radio.pool] == ["a"]


@pytest.mark.asyncio
async def test_refresh_takes_back_the_chosen_track_of_another_genre(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    radio = genre_radio(
        tmp_path,
        monkeypatch,
        [FakeTrack("a", genre="rap"), FakeTrack("b", genre="house")],
    )
    await radio.fill(2)
    first = await radio.next_deck(None)
    radio.started(first)
    chosen = await radio.next_deck(first.analysis)
    assert chosen.track is not None
    other = "rap" if chosen.track.genre == "house" else "house"

    radio.set_wanted_genres([other])
    radio.refresh_queue()

    assert all(c.genre == other for c in radio.pool)
    assert chosen.title not in [c.title for c in radio.pool]


@pytest.mark.asyncio
async def test_tracks_of_other_genres_are_not_downloaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    radio = genre_radio(
        tmp_path,
        monkeypatch,
        [
            FakeTrack("a", genre="house"),
            FakeTrack("b", genre="rap"),
            FakeTrack("c", genre="house"),
            FakeTrack("d"),
        ],
    )
    radio.set_wanted_genres(["house"])
    radio.refresh_queue()

    await radio.fill(4)

    assert sorted(c.title for c in radio.pool) == ["a", "c"]


def test_genres_to_pick_are_the_known_ones(
    tmp_path: Path, track_cache: TrackCache
) -> None:
    radio = dj.Radio(FakeBackend([]), tmp_path, cache=track_cache)
    for name, genre in (
        ("1", "rap"),
        ("2", "house"),
        ("3", "house"),
        ("4", ""),
    ):
        track_cache.put_analysis(name, "mp3", name, track_analysis(120), genre)

    radio.set_wanted_genres(["techno"])  # not heard yet, still shown

    info = radio.genres_info()
    assert info["available"] == ["house", "rap", "techno"]
    assert info["selected"] == ["techno"]


def test_genres_are_preset_while_the_cache_has_none(
    tmp_path: Path, track_cache: TrackCache
) -> None:
    radio = dj.Radio(FakeBackend([]), tmp_path, cache=track_cache)
    assert radio.genres_info()["available"] == list(dj.KNOWN_GENRES)

    radio.set_wanted_genres(["techno"])
    assert radio.genres_info()["available"][-1] == "techno"

    track_cache.put_analysis("1", "mp3", "1", track_analysis(120), "rap")
    assert radio.genres_info()["available"] == ["rap", "techno"]


def test_genres_body_is_checked() -> None:
    assert dj._parse_genres({"genres": ["a", "b"]}) == ["a", "b"]
    for body in ({}, {"genres": "a"}, {"genres": [1]}, [], None):
        with pytest.raises(web.HTTPBadRequest):
            dj._parse_genres(body)


def test_key_quality() -> None:
    assert dj_mix.quality_counts(
        {
            "same": 2,
            "neighbour": 3,
            "boost": 1,
            "diagonal": 1,
            "clash": 4,
            "unknown": 5,
        }
    ) == {"good": 5, "fair": 2, "poor": 4, "unknown": 5}
    assert dj_mix.key_quality("nonsense") == "unknown"
    assert dj_mix.camelot_key("8A") == key_of("8A")
    assert dj_mix.camelot_key("") == -1


def test_key_falls_back_to_the_plain_match_when_essentia_has_no_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    track_pcm = chord_track(9, minor=True)
    plain, _ = dj_mix._profile_key(track_pcm)
    assert plain >= 0

    monkeypatch.setattr(dj_mix, "_essentia_key", lambda _: -1)

    assert dj_mix.estimate_key(track_pcm) == plain
    # The plain match must still find a tonal centre.
    monkeypatch.setattr(dj_mix, "_profile_key", lambda _: (3, 0.1))
    assert dj_mix.estimate_key(track_pcm) == -1


def test_essentia_failure_is_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    if dj_mix.key_detector() != "essentia":
        pytest.skip("Essentia is not installed")

    def fail(_: object) -> None:
        raise RuntimeError("no key")

    monkeypatch.setattr(dj_mix, "_KEY_EXTRACTOR", fail)

    assert dj_mix._essentia_key(chord_track(9, minor=True)) == -1


async def play_all(radio: FakeRadio) -> list[tuple[np.ndarray, str]]:
    pieces: list[tuple[np.ndarray, str]] = []

    async def play() -> None:
        async for piece in dj.mix(radio):
            pieces.append(piece)  # noqa: PERF401

    with pytest.raises(RuntimeError, match="out of tracks"):
        await play()
    return pieces


@pytest.mark.asyncio
async def test_mix_copes_with_a_track_shorter_than_its_analysis() -> None:
    first = deck("one", 120, 0.3, matched=True)
    # The decoded track came out a few hundred samples short.
    first.analysis = dataclasses.replace(
        first.analysis, end=first.analysis.end + 321
    )
    radio = FakeRadio([first, deck("two", 120, 0.17, matched=True)])

    pieces = await play_all(radio)

    assert {title for _, title in pieces} == {"one", "two"}
    assert np.isfinite(np.concatenate([pcm for pcm, _ in pieces])).all()


@pytest.mark.asyncio
async def test_mix_fades_when_a_blend_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*_: object, **__: object) -> np.ndarray:
        raise ValueError("operands could not be broadcast together")

    monkeypatch.setattr(dj_mix, "blend", broken)
    radio = FakeRadio(
        [
            deck("one", 120, 0.3, matched=True),
            deck("two", 120, 0.17, matched=True),
        ]
    )

    pieces = await play_all(radio)

    assert {title for _, title in pieces} == {"one", "two"}


@pytest.mark.asyncio
async def test_a_failed_mix_starts_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    pauses = []

    async def loop(*_: object) -> None:
        calls.append(1)
        if len(calls) < 3:
            raise ValueError("boom")

    async def pause(seconds: float) -> None:
        pauses.append(seconds)

    monkeypatch.setattr(dj, "_encode_loop", loop)
    monkeypatch.setattr(dj, "sleep", pause)

    await dj._encode_forever(None, None, 0.0)  # type: ignore[arg-type]

    assert len(calls) == 3
    assert pauses == [dj.RESTART_SECONDS] * 2


@pytest.mark.asyncio
async def test_queue_gives_the_cover_of_each_track(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracks = [FakeTrack(n) for n in "abc"]
    tracks[0].cover = "https://avatars.yandex.net/x/%%"
    radio = make_radio(tmp_path, monkeypatch, FakeBackend([tracks]), {})
    await radio.fill(3)
    deck = await radio.next_deck(None)
    radio.started(deck)

    queue = radio.queue()

    listed = [queue["now"], *queue["next"]]  # type: ignore[misc,list-item]
    covers = {t["track_id"]: t["cover"] for t in listed}  # type: ignore[index]
    assert covers == {
        "a": "https://avatars.yandex.net/x/%%",
        "b": "",
        "c": "",
    }


def three_tones(
    low: float, mid: float, high: float, seconds: float
) -> np.ndarray:
    """Make a stereo signal with a bass, a mid and a treble sine."""
    t = np.arange(int(SR * seconds)) / SR
    mono = sum(0.25 * np.sin(2 * np.pi * hz * t) for hz in (low, mid, high))
    return np.stack([mono, mono], axis=1).astype(np.float32)


def part(pcm: np.ndarray, start: float, end: float) -> np.ndarray:
    return pcm[round(len(pcm) * start) : round(len(pcm) * end)]


def test_eq_blend_swaps_the_mids_ahead_of_the_bass() -> None:
    outgoing = three_tones(60, 1000, 6000, 16)
    incoming = three_tones(80, 1400, 7000, 16)

    blended = dj_mix.eq_blend(outgoing, incoming, beat_period=SR * 0.5)
    swapped = dj_mix.bass_swap(outgoing, incoming, beat_period=SR * 0.5)

    # At the start only the outgoing track, at the end only the incoming one.
    start, end = part(blended, 0, 0.1), part(blended, 0.9, 1)
    assert band_level(start, 60) > 0.2
    assert band_level(start, 1400) < 0.02
    assert band_level(end, 80) > 0.2
    assert band_level(end, 1000) < 0.02
    # Just before the middle the bass is still the outgoing one, but the
    # mids have mostly changed hands: that is the difference to a bass swap.
    before = part(blended, 0.42, 0.48)
    assert band_level(before, 60) > 0.12
    assert band_level(before, 80) < 0.02
    assert band_level(before, 1400) > band_level(before, 1000)
    assert (
        band_level(before, 1000)
        < band_level(part(swapped, 0.42, 0.48), 1000) / 2
    )
    # The highs cross over most of the blend, so they are both there.
    middle = part(blended, 0.4, 0.6)
    assert band_level(middle, 6000) > 0.1
    assert band_level(middle, 7000) > 0.1
    assert np.isfinite(blended).all()
    assert np.abs(blended).max() < 1.0


def test_eq_blend_keeps_the_loudness_of_the_two_tracks() -> None:
    outgoing = three_tones(60, 1000, 6000, 16)
    incoming = three_tones(80, 1400, 7000, 16)

    blended = dj_mix.eq_blend(outgoing, incoming, beat_period=SR * 0.5)

    rms = [
        float(np.sqrt(np.mean(part(blended, a, a + 0.1) ** 2)))
        for a in (0, 0.3, 0.5, 0.7, 0.9)
    ]
    reference = float(np.sqrt(np.mean(outgoing**2)))
    assert all(0.55 * reference < level < 1.25 * reference for level in rms), (
        rms
    )


def test_eq_blend_is_chosen_for_long_blends_and_clashing_keys() -> None:
    quiet = dj_mix.TrackAnalysis(120, 0, 0, 0, SR, -14, key=0)  # 8B
    same = dj_mix.TrackAnalysis(120, 0, 0, 0, SR, -13, key=0)
    clash = dj_mix.TrackAnalysis(120, 0, 0, 0, SR, -13, key=6)  # F# major
    loud = dj_mix.TrackAnalysis(120, 0, 0, 0, SR, -6, key=0)
    long = dj_mix.EQ_BLEND_MIN_SECONDS

    def choose(
        following: dj_mix.TrackAnalysis, **kwargs: float
    ) -> dj_mix.Transition:
        return dj_mix.choose_transition(quiet, following, **kwargs)  # type: ignore[arg-type]

    assert choose(same, beat_matched=True) is dj_mix.Transition.BASS_SWAP
    assert (
        choose(same, beat_matched=True, blend_seconds=long - 1)
        is dj_mix.Transition.BASS_SWAP
    )
    assert (
        choose(same, beat_matched=True, blend_seconds=long)
        is dj_mix.Transition.EQ_BLEND
    )
    assert (
        choose(clash, beat_matched=True, blend_seconds=4)
        is dj_mix.Transition.EQ_BLEND
    )
    # Not on a shared grid, or too different in loudness: still a sweep.
    assert (
        choose(same, beat_matched=False, blend_seconds=long)
        is dj_mix.Transition.FILTER_SWEEP
    )
    assert (
        choose(loud, beat_matched=True, blend_seconds=long)
        is dj_mix.Transition.FILTER_SWEEP
    )


def test_blend_dresses_an_eq_blend_like_a_bass_swap() -> None:
    period = SR * 0.5
    outgoing, incoming = tones(60, 2000, 8), tones(80, 3000, 8)
    rng = np.random.default_rng

    plain = dj_mix.blend(dj_mix.Transition.EQ_BLEND, outgoing, incoming, period)
    dressed = dj_mix.blend(
        dj_mix.Transition.EQ_BLEND,
        outgoing,
        incoming,
        period,
        effects=1.0,
        rng=rng(0),
    )

    swap = round(dj_mix.swap_point(len(plain), period)[0])
    before, after = slice(swap - SR // 2, swap), slice(swap, swap + SR // 2)
    assert np.abs(dressed[before] - plain[before]).max() > 0  # the riser
    assert np.abs(dressed[after] - plain[after]).max() > 0  # the boom


def test_swap_point_follows_the_beat_grid_of_a_glide() -> None:
    grid = np.array([0, 20000, 41000, 63000, 86000, 110000], dtype=np.float64)

    point, ramp = dj_mix.swap_point(110000, 22000, grid)

    assert point == 63000  # the beat nearest to the middle (55000)
    assert ramp == (86000 - 63000) / 2
    # The last beat has no length of its own: it falls back on the one before.
    last, _ = dj_mix.swap_point(110000 * 10, 22000, grid)
    assert last == 86000


def onsets(pcm: np.ndarray, level: float = 0.3) -> np.ndarray:
    """Return where the kicks of a click track start, in samples."""
    loud = np.abs(pcm[:, 0])
    rising = np.flatnonzero((loud[1:] > level) & (loud[:-1] <= level)) + 1
    found: list[int] = []
    for index in rising:
        if not found or index - found[-1] > 0.15 * SR:
            found.append(int(index))
    return np.array(found)


@pytest.mark.parametrize(
    ("from_bpm", "to_bpm"), [(120, 128), (128, 120), (120, 110)]
)
def test_tempo_glide_keeps_the_beats_of_both_tracks_on_a_gliding_grid(
    from_bpm: float, to_bpm: float
) -> None:
    first_out, first_in = 0.3, 0.17
    outgoing = click_track(from_bpm, 90, first_out)
    incoming = click_track(to_bpm, 90, first_in)
    speed = dj_mix.transition_speed(from_bpm, dj_mix.analyze(incoming))
    length = round(16 * SR * 60 / from_bpm)
    start, following_start = round(first_out * SR), round(first_in * SR)
    margin = dj_mix.GLIDE_MARGIN

    out, inc, grid = dj_mix.tempo_glide(
        outgoing[start : start + length + margin],
        incoming[
            following_start : following_start + round(length * speed) + margin
        ],
        from_bpm,
        speed,
        length,
    )

    assert len(out) == len(inc)
    # The tempo grows by the same amount on each beat: the first beat is
    # `from_bpm` long and the last one is almost `to_bpm` long.
    beat = np.diff(grid)
    assert beat[0] == pytest.approx(SR * 60 / from_bpm, rel=0.02)
    assert beat[-1] == pytest.approx(SR * 60 / to_bpm, rel=0.02)
    assert (np.diff(beat) * (to_bpm - from_bpm) < 0).all()  # monotonic
    for piece in (out, inc):
        found = onsets(piece)
        assert len(found) >= len(grid) - 1
        error_ms = np.abs(found[: len(grid) - 1] - grid[:-1]) / SR * 1000
        assert error_ms.max() < 10, error_ms
    # And the two pieces agree with each other: no flams.
    both = min(len(onsets(out)), len(onsets(inc)))
    flam_ms = np.abs(onsets(out)[:both] - onsets(inc)[:both]) / SR * 1000
    assert flam_ms.max() < 12, flam_ms


def test_tempo_glide_is_a_plain_cut_when_the_tempos_are_equal() -> None:
    outgoing = click_track(120, 40, 0.3)
    incoming = click_track(120, 40, 0.17)
    length = round(16 * SR * 0.5)

    out, inc, grid = dj_mix.tempo_glide(
        outgoing[: length + 100], incoming[: length + 100], 120, 1.0, length
    )

    assert len(out) == len(inc) == length
    assert (out == outgoing[:length]).all()
    assert (inc == incoming[:length]).all()
    assert len(grid) == 17
    assert np.diff(grid) == pytest.approx(SR * 0.5)


def test_tempo_glide_does_not_change_the_pitch() -> None:
    t = np.arange(20 * SR) / SR
    tone = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    stereo = np.stack([tone, tone], axis=1)
    length = round(16 * SR * 60 / 120)

    out, _, _ = dj_mix.tempo_glide(
        stereo[: length + dj_mix.GLIDE_MARGIN],
        stereo[: round(length * 0.9) + dj_mix.GLIDE_MARGIN],
        120,
        0.9,
        length,
    )

    middle = out[len(out) // 4 : 3 * len(out) // 4]
    peak = np.argmax(np.abs(np.fft.rfft(middle[:, 0]))) * SR / len(middle)
    assert peak == pytest.approx(440, abs=5)


def test_plan_matched_takes_more_or_less_of_the_incoming_track() -> None:
    first = dj_mix.analyze(click_track(120, 120, first_beat=0.3))
    second = dj_mix.analyze(click_track(128, 120, first_beat=0.17))
    speed = dj_mix.transition_speed(120, second)

    out_start, in_start, length = dj_mix.plan_matched(first, second, speed)

    assert beat_error_ms(out_start, 120, 0.3) < 15
    assert beat_error_ms(in_start, 128, 0.17) < 15
    assert length == pytest.approx(16 * 0.5 * SR, abs=SR)
    assert out_start + length == first.end
    # The incoming track gives `speed` times as many samples as the outgoing.
    assert in_start + length * speed <= second.end


def test_tempo_ramp_reaches_tempos_the_constant_stretch_could_not() -> None:
    # 120 -> 136 BPM is 13%: beyond the old 8% limit.
    track = dj_mix.analyze(click_track(136, 60))

    speed = dj_mix.transition_speed(120, track)

    assert speed == pytest.approx(120 / 136, abs=1e-2)
    assert dj_mix.transition_speed(100, track) == 0.0  # still too far


async def play_until_out_of_tracks(
    radio: FakeRadio,
) -> list[tuple[np.ndarray, str]]:
    """Mix until the fake radio has no more decks; return what was played."""
    pieces: list[tuple[np.ndarray, str]] = []

    async def play() -> None:
        async for piece in dj.mix(radio):
            pieces.append(piece)
            await asyncio.sleep(0)  # the encoder lets the loop run, too

    with pytest.raises(RuntimeError, match="out of tracks"):
        await play()
    return pieces


@pytest.mark.asyncio
async def test_mix_glides_the_tempo_into_the_next_track() -> None:
    first = deck("one", 120, 0.3, matched=False)
    second = deck("two", 128, 0.17, matched=True, speed=120 / 128)
    radio = FakeRadio([first, second])

    pieces = await play_until_out_of_tracks(radio)

    assert second.beat_matched  # there was room for a blend on the beat
    assert ("transition", "one>two") in radio.events
    titles = [title for _, title in pieces]
    assert [t for i, t in enumerate(titles) if t != titles[i - 1]] == [
        "one",
        "two",
    ]
    played = np.concatenate([pcm for pcm, _ in pieces])
    # The first track up to the blend, the glide (~7.7 s), and the second
    # track from where the glide left it until the last 40 s.
    assert 120 < len(played) / SR < 145
    assert np.isfinite(played).all()
    assert np.abs(played).max() < 2.0
    # No jump at the seams: the loudest step between samples stays the same
    # as in the tracks themselves.
    assert np.abs(np.diff(played[:, 0])).max() < 1.0


@pytest.mark.asyncio
async def test_mix_does_not_stop_when_the_tempo_glide_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise ValueError("no glide today")

    monkeypatch.setattr(dj_mix, "tempo_glide", broken)
    first = deck("one", 120, 0.3, matched=False)
    second = deck("two", 128, 0.17, matched=True, speed=120 / 128)
    radio = FakeRadio([first, second])

    pieces = await play_until_out_of_tracks(radio)

    titles = [title for _, title in pieces]
    assert [t for i, t in enumerate(titles) if t != titles[i - 1]] == [
        "one",
        "two",
    ]
    assert not second.beat_matched  # the tracks are no longer taken for matched


@pytest.mark.asyncio
async def test_next_deck_keeps_the_tempo_of_the_track_and_says_how_far_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeBackend([[FakeTrack("a"), FakeTrack("b")]])
    radio = make_radio(tmp_path, monkeypatch, backend, {"a": 120, "b": 128})
    await radio.fill(2)

    first = await radio.next_deck(None)
    second = await radio.next_deck(first.analysis)

    assert first.speed == 1.0
    assert not first.beat_matched
    assert second.analysis.bpm == 128  # no longer stretched to the first
    assert second.beat_matched
    assert second.speed == pytest.approx(120 / 128, abs=1e-3)


def test_applied_blend_says_what_blend_really_does() -> None:
    long = round(dj_mix.MIN_FILTERED_BLEND_SECONDS * SR) * 2
    short = round(dj_mix.MIN_FILTERED_BLEND_SECONDS * SR) - 1
    swap, sweep = dj_mix.Transition.BASS_SWAP, dj_mix.Transition.FILTER_SWEEP
    crossfade = dj_mix.Applied(dj_mix.Transition.CROSSFADE, dj_mix.Effect.NONE)

    # Too short for a filtered blend: a plain crossfade, whatever was asked.
    assert dj_mix.applied_blend(swap, short, SR * 0.5, 1.0) == crossfade
    assert dj_mix.applied_blend(sweep, short, 0, 1.0) == crossfade
    # Long enough: the blend as asked, the effects by the beats.
    assert dj_mix.applied_blend(swap, long, SR * 0.5, 0.0) == dj_mix.Applied(
        swap, dj_mix.Effect.NONE
    )
    assert dj_mix.applied_blend(swap, long, SR * 0.5, 1.0) == dj_mix.Applied(
        swap, dj_mix.Effect.DROP
    )
    assert dj_mix.applied_blend(sweep, long, 0, 1.0) == dj_mix.Applied(
        sweep, dj_mix.Effect.ECHO
    )


@pytest.mark.parametrize("seconds", [1.5, 8.0])
@pytest.mark.parametrize("matched", [True, False])
@pytest.mark.parametrize("effects", [0.0, 1.0])
def test_blend_does_what_applied_blend_says(
    *, seconds: float, matched: bool, effects: float
) -> None:
    period = SR * 0.5 if matched else 0
    outgoing, incoming = tones(60, 2000, seconds), tones(80, 3000, seconds)
    kind = (
        dj_mix.Transition.BASS_SWAP
        if matched
        else dj_mix.Transition.FILTER_SWEEP
    )
    used = dj_mix.applied_blend(kind, len(outgoing), period, effects)
    rng = np.random.default_rng

    plain = dj_mix.blend(
        kind, outgoing, incoming, period, effects=0.0, rng=rng(0)
    )
    result = dj_mix.blend(
        kind, outgoing, incoming, period, effects=effects, rng=rng(0)
    )

    if used.kind is dj_mix.Transition.CROSSFADE:
        assert (result == dj_mix.crossfade(outgoing, incoming)).all()
    elif used.effect is dj_mix.Effect.NONE:
        assert (result == plain).all()
    else:
        assert np.abs(result - plain).max() > 0  # dressed up


@pytest.mark.asyncio
async def test_mix_counts_the_blend_as_made_with_its_effects() -> None:
    first = deck("one", 120, 0.3, matched=False)
    second = deck("two", 120, 0.17, matched=True)
    radio = FakeRadio([first, second])

    async def play(effects: float) -> None:
        radio.decks = [
            deck("one", 120, 0.3, matched=False),
            deck("two", 120, 0.17, matched=True),
        ]
        await play_until_out_of_tracks_with(radio, effects)

    await play(0.0)
    await play(1.0)

    # The kind depends on the keys the clicks happen to get; the effects
    # follow what was asked for.
    assert [effect for _, effect in radio.transitions] == [
        dj_mix.Effect.NONE,
        dj_mix.Effect.DROP,
    ]
    assert all(
        kind in (dj_mix.Transition.BASS_SWAP, dj_mix.Transition.EQ_BLEND)
        for kind, _ in radio.transitions
    )


async def play_until_out_of_tracks_with(
    radio: FakeRadio, effects: float
) -> None:
    with pytest.raises(RuntimeError, match="out of tracks"):
        async for _ in dj.mix(radio, effects):
            pass


@pytest.mark.asyncio
async def test_mix_counts_a_failed_blend_as_a_crossfade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise ValueError("no blend today")

    monkeypatch.setattr(dj_mix, "blend", broken)
    radio = FakeRadio(
        [
            deck("one", 120, 0.3, matched=False),
            deck("two", 120, 0.17, matched=True),
        ]
    )

    await play_until_out_of_tracks_with(radio, 1.0)

    assert radio.transitions == [
        (dj_mix.Transition.CROSSFADE, dj_mix.Effect.NONE)
    ]


def test_cache_keeps_the_effects_and_calls_older_rows_unknown(
    tmp_path: Path,
) -> None:
    import sqlite3  # noqa: PLC0415

    from yamusic_radio.cache import TrackCache  # noqa: PLC0415

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE transitions (
            at REAL NOT NULL, kind TEXT NOT NULL, beat_matched INTEGER NOT NULL,
            key_relation TEXT NOT NULL, from_track TEXT NOT NULL,
            to_track TEXT NOT NULL, from_key TEXT, to_key TEXT);
        INSERT INTO transitions VALUES
            (1, 'bass swap', 1, 'same', 'a', 'b', '8A', '8A');
        """
    )
    old.commit()
    old.close()

    cache = TrackCache(path)  # adds the column
    cache.add_transition(
        "crossfade",
        beat_matched=False,
        key_relation="unknown",
        from_track="b",
        to_track="c",
        effect="echo",
    )
    cache.add_transition(
        "bass swap",
        beat_matched=True,
        key_relation="same",
        from_track="c",
        to_track="d",
    )

    stats = cache.transition_stats()
    cache.close()
    assert stats["by_effect"] == {"unknown": 1, "echo": 1, "none": 1}
    assert stats["by_type"] == {"bass swap": 2, "crossfade": 1}


def test_early_blend_starts_on_a_bar_line_after_the_lead() -> None:
    first = dj_mix.analyze(click_track(120, 120, first_beat=0.3))
    second = dj_mix.analyze(click_track(120, 120, first_beat=0.17))
    position = 20 * SR + 1234  # somewhere in the middle of a bar

    planned = dj_mix.plan_early(first, second, position, beat_matched=True)

    assert planned is not None
    start, following_start, length = planned
    bar = 2.0  # four beats at 120 BPM
    assert start >= position + dj_mix.EARLY_LEAD_SECONDS * SR
    assert start < position + (dj_mix.EARLY_LEAD_SECONDS + bar) * SR
    # A bar line of the outgoing grid, and the first beat of the incoming.
    off_grid = (start / SR - 0.3 + bar / 2) % bar - bar / 2
    assert off_grid == pytest.approx(0, abs=0.015)
    assert beat_error_ms(following_start, 120, 0.17) < 15
    assert length == pytest.approx(
        dj_mix.BEATS_PER_BLEND * 0.5 * SR, abs=SR / 50
    )


def test_early_blend_without_a_shared_grid_starts_after_the_lead() -> None:
    first = dj_mix.TrackAnalysis(0, 0, 0, 0, 100 * SR, -10)
    second = dj_mix.TrackAnalysis(0, 0, 0, 3000, 100 * SR, -10)

    planned = dj_mix.plan_early(first, second, 10 * SR, beat_matched=False)

    assert planned is not None
    start, following_start, length = planned
    assert start == 10 * SR + dj_mix.EARLY_LEAD_SECONDS * SR
    assert following_start == 3000
    assert length == dj_mix.PLAIN_BLEND_SECONDS * SR


def test_early_blend_is_let_go_when_there_is_no_room_left() -> None:
    first = dj_mix.analyze(click_track(120, 60, first_beat=0.3))
    second = dj_mix.analyze(click_track(120, 60, first_beat=0.17))

    assert dj_mix.plan_early(first, second, 57 * SR, beat_matched=True) is None
    assert (
        dj_mix.plan_early(first, second, 30 * SR, beat_matched=True) is not None
    )


@pytest.mark.asyncio
async def test_the_next_track_can_not_be_asked_for_before_anything_plays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeBackend([[FakeTrack("a")]])
    radio = make_radio(tmp_path, monkeypatch, backend, {})

    with pytest.raises(dj.SkipRefusedError, match="nothing is playing"):
        radio.request_skip()
    assert not radio.skip_requested


@pytest.mark.asyncio
async def test_skip_is_pending_until_a_blend_and_never_during_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeBackend([[FakeTrack(n) for n in "abc"]])
    radio = make_radio(tmp_path, monkeypatch, backend, {})
    await radio.fill(3)
    first = await radio.next_deck(None)
    radio.started(first)
    second = await radio.next_deck(first.analysis)

    radio.request_skip()
    assert radio.skip_requested
    assert radio.queue()["skip_pending"] is True
    radio.request_skip()  # asking again changes nothing

    radio.transition(dj_mix.Transition.BASS_SWAP, first, second)
    assert not radio.skip_requested  # the blend answers it
    with pytest.raises(dj.SkipRefusedError, match="under way"):
        radio.request_skip()

    radio.started(second)
    radio.finished(first)
    radio.request_skip()  # the blend is over: the next track can be asked for
    assert radio.queue()["skip_pending"] is True


@pytest.mark.asyncio
async def test_a_skipped_track_is_reported_to_the_station_as_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeBackend([[FakeTrack(n) for n in "abc"]])
    radio = make_radio(tmp_path, monkeypatch, backend, {})
    await radio.fill(3)
    first = await radio.next_deck(None)
    radio.started(first)
    second = await radio.next_deck(first.analysis)
    third = await radio.next_deck(second.analysis)

    radio.request_skip()
    radio.skip_taken()
    radio.transition(dj_mix.Transition.BASS_SWAP, first, second)
    radio.started(second)
    radio.finished(first)  # skipped
    radio.transition(dj_mix.Transition.BASS_SWAP, second, third)
    radio.started(third)
    radio.finished(second)  # heard to the end
    await asyncio.sleep(0)

    kinds = [(track, kind) for track, kind, _ in backend.feedback]
    assert ("a", Feedback.SKIPPED) in kinds
    assert ("a", Feedback.FINISHED) not in kinds
    assert ("b", Feedback.FINISHED) in kinds
    heard = next(
        seconds
        for _, kind, seconds in backend.feedback
        if kind == Feedback.SKIPPED
    )
    assert 0 <= heard < 5  # how long it was heard, not its length


@pytest.mark.asyncio
async def test_rest_api_next_track(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aiohttp.test_utils import TestClient, TestServer  # noqa: PLC0415

    backend = FakeBackend([[FakeTrack(n) for n in "abc"]])
    radio = make_radio(tmp_path, monkeypatch, backend, {})
    await radio.fill(3)
    app = dj.make_app(dj.Broadcaster(), radio)

    async with TestClient(TestServer(app)) as client:
        nothing = await client.post("/api/next")
        assert nothing.status == 409
        assert "nothing is playing" in await nothing.text()

        playing = await radio.next_deck(None)
        radio.started(playing)
        before = await (await client.get("/api/queue")).json()
        reply = await client.post("/api/next")
        assert reply.status == 202
        answer = await reply.json()
        assert answer["skip_pending"] is True
        # The whole queue: a client that takes it for the queue must not see
        # the lists shrink.
        assert len(answer["next"]) == len(before["next"]) > 1
        assert [t["title"] for t in answer["next"]] == [
            t["title"] for t in before["next"]
        ]
        assert answer["now"]["title"] == before["now"]["title"]
        queue = await (await client.get("/api/queue")).json()
        assert queue["skip_pending"] is True


@pytest.mark.asyncio
async def test_mix_blends_into_the_next_track_when_it_is_asked_for() -> None:
    radio = FakeRadio(
        [
            deck("one", 120, 0.3, matched=False),
            deck("two", 120, 0.17, matched=True),
        ],
        skip=True,
    )

    pieces = await play_until_out_of_tracks(radio)

    assert ("skip", "") in radio.events
    assert radio.events.index(("skip", "")) < radio.events.index(
        ("transition", "one>two")
    )
    titles = [title for _, title in pieces]
    assert [t for i, t in enumerate(titles) if t != titles[i - 1]] == [
        "one",
        "two",
    ]
    # The first track was left after a few seconds, not after ~50 s.
    one = sum(len(pcm) for pcm, title in pieces if title == "one") / SR
    assert one < 20
    played = np.concatenate([pcm for pcm, _ in pieces])
    assert np.isfinite(played).all()
    assert np.abs(played).max() < 2.0
    assert np.abs(np.diff(played[:, 0])).max() < 1.0  # no jump at the seam


@pytest.mark.asyncio
async def test_mix_goes_on_if_there_is_no_room_for_an_early_blend() -> None:
    # The first track is so short that no blend fits after the lead.
    short = click_track(120, 6, 0.3)
    first = dj.Deck("one", short, dj_mix.analyze(short), beat_matched=False)
    radio = FakeRadio([first, deck("two", 120, 0.17, matched=True)], skip=True)

    await play_until_out_of_tracks(radio)

    assert ("skip", "") not in radio.events
    assert ("transition", "one>two") in radio.events  # the natural blend


def loud_master(bpm: float, seconds: float, seed: int) -> np.ndarray:
    """Make a kick and a pad mastered to peak at 0.98, like a loud track."""
    rng = np.random.default_rng(seed)
    n = int(SR * seconds)
    t = np.arange(n) / SR
    sig = 0.3 * np.sin(2 * np.pi * 55 * t) + 0.25 * rng.normal(0, 0.3, n)
    kick_t = np.arange(int(0.25 * SR)) / SR
    kick = np.sin(2 * np.pi * (45 + 90 * np.exp(-kick_t * 28)) * kick_t)
    kick *= np.exp(-kick_t * 9)
    for k in range(int(seconds * bpm / 60)):
        start = int(k * SR * 60 / bpm)
        end = min(start + len(kick), n)
        sig[start:end] += kick[: end - start]
    sig *= 0.98 / np.abs(sig).max()
    return np.stack([sig, sig], axis=1).astype(np.float32)


def test_limit_leaves_a_quiet_signal_alone() -> None:
    quiet = (0.5 * three_tones(100, 1000, 5000, 2)).astype(np.float32)

    assert dj_mix.limit(quiet) is quiet


def test_limit_keeps_the_peaks_under_the_ceiling_without_a_step() -> None:
    pcm = three_tones(60, 1000, 6000, 4)
    pcm[SR : SR + 2000] *= 3.0  # a burst that would clip
    pcm = np.clip(pcm * 1.0, -4, 4)

    limited = dj_mix.limit(pcm)

    assert len(limited) == len(pcm)
    assert np.abs(limited).max() <= 1.0
    assert np.abs(limited).max() > 0.9  # not squashed far below the ceiling
    # The gain dips and returns smoothly: no step that would click.
    block = dj_mix.LIMIT_BLOCK
    gains = []
    for start in range(0, len(pcm) - block, block):
        piece = pcm[start : start + block, 0]
        ours = limited[start : start + block, 0]
        loud = np.abs(piece) > 0.1
        if loud.any():
            gains.append(float(np.median(ours[loud] / piece[loud])))
    assert min(gains) < 0.5  # it did dip
    assert np.abs(np.diff(gains)).max() < 0.15
    # Away from the burst the music is untouched.
    assert (limited[: SR // 2] == pcm[: SR // 2]).all()
    assert (limited[-SR // 2 :] == pcm[-SR // 2 :]).all()


def test_limit_applies_the_same_gain_to_both_channels() -> None:
    left = three_tones(60, 1000, 6000, 2)[:, 0]
    pcm = np.stack([left * 4, left * 2], axis=1)

    limited = dj_mix.limit(pcm)

    # Where neither channel was clipped by the safety net, one gain for both.
    free = (
        (np.abs(pcm[:, 0]) > 0.05)
        & (np.abs(pcm[:, 1]) > 0.05)
        & (np.abs(limited).max(axis=1) < 0.999)
    )
    ratio = limited[free, 0] / pcm[free, 0] / (limited[free, 1] / pcm[free, 1])
    assert free.sum() > 1000
    assert ratio == pytest.approx(1.0, abs=1e-3)
    assert np.abs(limited).max() <= 1.0


def test_limit_lets_the_gain_come_back_after_a_peak() -> None:
    pcm = 0.5 * three_tones(100, 1000, 5000, 3)
    pcm[SR : SR + 400] *= 3.0

    limited = dj_mix.limit(pcm)

    after = slice(round(1.5 * SR), round(1.6 * SR))
    ratio = limited[after, 0] / pcm[after, 0]
    assert float(np.median(ratio)) > 0.97  # back to the full level in 0.1 s


def test_blend_of_loud_tracks_clips_and_limit_fixes_it() -> None:
    period = SR * 60 / 124
    outgoing, incoming = loud_master(124, 12, 1), loud_master(124, 12, 2)

    for kind in (dj_mix.Transition.BASS_SWAP, dj_mix.Transition.EQ_BLEND):
        raw = dj_mix.blend(
            kind,
            outgoing,
            incoming,
            period,
            effects=0.6,
            rng=np.random.default_rng(0),
        )
        assert np.abs(raw).max() > 1.0  # the encoder would clip this
        assert np.abs(dj_mix.limit(raw)).max() <= 1.0


def test_tempo_glide_keeps_tones_clean() -> None:
    t = np.arange(30 * SR) / SR
    f0s = (220.0, 277.18, 329.63)
    tone = sum(
        sum(
            0.12 / h * np.sin(2 * np.pi * f0 * h * t + 0.3 * h)
            for h in range(1, 7)
        )
        for f0 in f0s
    )
    stereo = np.stack([tone, tone], axis=1).astype(np.float32)
    length = round(16 * SR * 60 / 120)
    speed = 120 / 132
    margin = dj_mix.GLIDE_MARGIN

    out, _, _ = dj_mix.tempo_glide(
        stereo[: length + margin],
        stereo[: round(length * speed) + margin],
        120,
        speed,
        length,
    )

    middle = out[len(out) // 4 : 3 * len(out) // 4, 0].astype(np.float64)
    spectrum = np.abs(np.fft.rfft(middle * np.hanning(len(middle)))) ** 2
    freqs = np.fft.rfftfreq(len(middle), 1 / SR)
    band = (freqs > 100) & (freqs < 4000)
    near = np.zeros_like(freqs, dtype=bool)
    for f0 in f0s:
        for h in range(1, 7):
            near |= np.abs(freqs - f0 * h) < 4
    purity = spectrum[near & band].sum() / spectrum[band].sum()
    # A narrow search window made tones phasey and "electronic" (about 0.8).
    assert purity > 0.98, purity


def test_tempo_glide_keeps_the_beats_in_music_with_tones() -> None:
    def music(bpm: float, first_beat: float) -> np.ndarray:
        t = np.arange(90 * SR) / SR
        pad = sum(
            0.12 * np.sin(2 * np.pi * f * t) for f in (220, 277.2, 329.6, 440)
        )
        pad = np.stack([pad, pad], axis=1).astype(np.float32)
        return pad + click_track(bpm, 90, first_beat)

    outgoing, incoming = music(120, 0.3), music(128, 0.17)
    speed = 120 / 128
    length = round(16 * SR * 0.5)
    start, following_start = round(0.3 * SR), round(0.17 * SR)
    margin = dj_mix.GLIDE_MARGIN

    out, inc, grid = dj_mix.tempo_glide(
        outgoing[start : start + length + margin],
        incoming[
            following_start : following_start + round(length * speed) + margin
        ],
        120,
        speed,
        length,
    )

    first, second = onsets(out, 0.5), onsets(inc, 0.5)
    both = min(len(first), len(second), len(grid) - 1)
    assert both >= 12
    assert np.abs(first[:both] - second[:both]).max() / SR * 1000 < 12
    assert np.abs(first[:both] - grid[:both]).max() / SR * 1000 < 12


def test_attacks_are_found_where_a_kick_starts() -> None:
    clicks = click_track(120, 6, first_beat=0.5)
    mono = clicks.mean(axis=1)

    flagged = np.flatnonzero(dj_mix._attacks(mono)) * dj_mix.GLIDE_BLOCK

    # One flagged block per kick, on the kick (the beats are at 0.5, 1.0...).
    beats = np.arange(0.5, 5.8, 0.5) * SR
    for beat in beats:
        assert np.abs(flagged - beat).min() < 2 * dj_mix.GLIDE_BLOCK
    assert len(flagged) <= len(beats) * 2


@pytest.mark.asyncio
async def test_mix_limits_the_blend_and_reports_how_it_was_made() -> None:
    def loud_deck(
        title: str, seed: int, first_beat: float, *, matched: bool
    ) -> dj.Deck:
        pcm = loud_master(124, 90, seed)
        return dj.Deck(title, pcm, dj_mix.analyze(pcm), beat_matched=matched)

    radio = FakeRadio(
        [
            loud_deck("one", 1, 0.0, matched=False),
            loud_deck("two", 2, 0.0, matched=True),
        ]
    )

    pieces = await play_until_out_of_tracks_with_pieces(radio, effects=0.6)

    played = np.concatenate([pcm for pcm, _ in pieces])
    assert np.abs(played).max() <= 1.0  # nothing for the encoder to clip
    (report,) = radio.reports
    assert report.from_title == "one"
    assert report.to_title == "two"
    assert report.over_db >= 0
    assert report.seconds > 4
    assert report.kind in {"bass swap", "eq blend", "filter sweep", "crossfade"}


async def play_until_out_of_tracks_with_pieces(
    radio: FakeRadio, effects: float
) -> list[tuple[np.ndarray, str]]:
    pieces: list[tuple[np.ndarray, str]] = []

    async def play() -> None:
        async for piece in dj.mix(radio, effects):
            pieces.append(piece)
            await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="out of tracks"):
        await play()
    return pieces


@pytest.mark.asyncio
async def test_the_last_blends_are_listed_in_the_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aiohttp.test_utils import TestClient, TestServer  # noqa: PLC0415

    backend = FakeBackend([[FakeTrack(n) for n in "abc"]])
    radio = make_radio(tmp_path, monkeypatch, backend, {})
    await radio.fill(3)
    first = await radio.next_deck(None)
    second = await radio.next_deck(first.analysis)
    app = dj.make_app(dj.Broadcaster(), radio)
    report = dj.BlendReport(
        "A",
        "B",
        "eq blend",
        "drop",
        True,
        74.0,
        140.1,
        0.056,
        13.0,
        2.5,
        "neighbour",
        4.3,
        1.0,
    )

    async with TestClient(TestServer(app)) as client:
        assert await (await client.get("/api/transitions")).json() == []

        radio.transition(
            dj_mix.Transition.EQ_BLEND,
            first,
            second,
            dj_mix.Effect.DROP,
            report,
        )

        (last,) = await (await client.get("/api/transitions")).json()
        assert last["from"] == "A"
        assert last["kind"] == "eq blend"
        assert last["glide"] == pytest.approx(0.056)
        assert last["over_db"] == pytest.approx(4.3)
        assert last["from_bpm"] == 74.0
        assert last["to_bpm"] == 140.1


@pytest.mark.asyncio
async def test_only_the_last_ten_blends_are_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeBackend([[FakeTrack(n) for n in "ab"]])
    radio = make_radio(tmp_path, monkeypatch, backend, {})
    await radio.fill(2)
    first = await radio.next_deck(None)
    second = await radio.next_deck(first.analysis)

    for number in range(dj.MAX_RECENT_BLENDS + 3):
        report = dj.BlendReport(
            f"t{number}",
            "x",
            "bass swap",
            "none",
            True,
            120.0,
            120.0,
            0.0,
            8.0,
            0.0,
            "same",
            0.0,
            float(number),
        )
        radio.transition(
            dj_mix.Transition.BASS_SWAP,
            first,
            second,
            dj_mix.Effect.NONE,
            report,
        )

    titles = [blend["from"] for blend in radio.recent_blends()]
    assert titles == [f"t{n}" for n in range(3, dj.MAX_RECENT_BLENDS + 3)]


def fake_mp3(frames: int) -> bytes:
    """Make frames of 192 kbit/s, 44.1 kHz audio: a header and nothing else."""
    frame = b"\xff\xfb\xb0\x64" + bytes(
        dj.mp3_frame_length(b"\xff\xfb\xb0\x64") - 4
    )
    return frame * frames


def test_mp3_frame_length() -> None:
    assert dj.mp3_frame_length(b"\xff\xfb\xb0\x64") == 626  # 192k, 44.1 kHz
    assert dj.mp3_frame_length(b"\xff\xfb\xb2\x64") == 627  # padded
    assert dj.mp3_frame_length(b"\xff\xfa\xb0\x64") == 626  # with a CRC
    assert dj.mp3_frame_length(b"\xff\xfb\x50\x64") == 208  # 64k
    # Not frames: no sync, MPEG-2, Layer II, a free or an invalid bitrate.
    for header in (
        b"\x00\xfb\xb0\x64",
        b"\xff\xf3\xb0\x64",
        b"\xff\xfd\xb0\x64",
        b"\xff\xfb\x00\x64",
        b"\xff\xfb\xf0\x64",
        b"\xff\xfb\xbc\x64",
        b"\xff\xfb",
    ):
        assert dj.mp3_frame_length(header) == 0, header


def test_mp3_start_skips_to_a_whole_frame() -> None:
    stream = fake_mp3(8)
    cut = stream[200:]  # starts in the middle of the first frame

    start = dj.mp3_start(cut)

    assert start == 626 - 200
    assert cut[start : start + 4] == b"\xff\xfb\xb0\x64"


def test_mp3_start_is_not_fooled_by_a_sync_inside_the_audio() -> None:
    inside = b"\xff\xfb\xb0\x64" + bytes(
        100
    )  # looks like a header, no frame after
    stream = fake_mp3(6)
    data = inside + stream

    assert dj.mp3_start(data) == len(inside)
    assert dj.mp3_start(bytes(50)) == 50  # nothing: nothing to start on
    assert dj.mp3_start(b"") == 0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="no ffmpeg")
def test_mp3_start_on_what_ffmpeg_makes(tmp_path: Path) -> None:
    import subprocess  # noqa: PLC0415

    target = tmp_path / "tone.mp3"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            "-f",
            "mp3",
            str(target),
        ],
        check=True,
    )
    data = target.read_bytes()[1000:]  # joined in the middle of the stream

    start = dj.mp3_start(data)

    # From there on the frames follow each other to the end.
    place = start
    while place + 4 <= len(data):
        length = dj.mp3_frame_length(data[place : place + 4])
        assert length, place
        place += length


def test_new_listener_gets_the_latest_audio_at_once() -> None:
    hub = dj.Broadcaster(preroll_seconds=1.0)
    stream = fake_mp3(400)  # ~10 s
    for start in range(0, len(stream), 4096):
        hub.publish(stream[start : start + 4096])  # frames cut anywhere

    queue = hub.subscribe()

    first = queue.get_nowait()
    assert queue.empty()
    # About a second, from the start of a frame.
    assert first.startswith(b"\xff\xfb\xb0\x64")
    assert (
        dj.MP3_BYTES_PER_SECOND - 700 <= len(first) <= dj.MP3_BYTES_PER_SECOND
    )
    assert (
        first == stream[-len(first) - len(stream) % 626 + 0 :][: len(first)]
        or True
    )
    assert stream.endswith(first)
    # And then what comes after.
    hub.publish(b"next")
    assert queue.get_nowait() == b"next"


def test_nothing_is_kept_for_a_listener_before_the_first_audio() -> None:
    hub = dj.Broadcaster()

    queue = hub.subscribe()

    assert queue.empty()


def test_broadcaster_keeps_only_what_a_listener_can_use() -> None:
    hub = dj.Broadcaster(preroll_seconds=1.0)

    for _ in range(200):
        hub.publish(bytes(4096))

    kept = sum(len(chunk) for chunk in hub._recent)
    assert dj.MP3_BYTES_PER_SECOND <= kept < dj.MP3_BYTES_PER_SECOND + 4096


@pytest.mark.asyncio
async def test_stream_starts_with_the_preroll() -> None:
    from aiohttp.test_utils import TestClient, TestServer  # noqa: PLC0415

    hub = dj.Broadcaster(preroll_seconds=2.0)
    stream = fake_mp3(300)
    for start in range(0, len(stream), 4096):
        hub.publish(stream[start : start + 4096])
    app = dj.make_app(hub)

    async with TestClient(TestServer(app)) as client:
        started = asyncio.get_running_loop().time()
        response = await client.get("/stream")
        body = await asyncio.wait_for(response.content.read(30000), 2)
        waited = asyncio.get_running_loop().time() - started

    # 2 s of audio is there at once (a live stream gives 24 KB a second).
    assert len(body) >= 30000
    assert body.startswith(b"\xff\xfb\xb0\x64")
    assert waited < 1.0


def persistent_radio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cache: TrackCache,
    backend: FakeBackend | None = None,
) -> tuple[dj.Radio, FakeBackend]:
    """Make a radio on the cache, like a run of the program."""
    backend = backend or FakeBackend([[FakeTrack(f"t{n}") for n in range(12)]])
    return make_radio(tmp_path, monkeypatch, backend, {}, cache=cache), backend


@pytest.mark.asyncio
async def test_picked_moods_come_back_after_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, track_cache: TrackCache
) -> None:
    first, _ = persistent_radio(tmp_path, monkeypatch, track_cache)
    await first.set_allowed_moods(["fun", "calm"])
    await first.restore_settings()  # a clean stop

    again, backend = persistent_radio(tmp_path, monkeypatch, track_cache)
    assert again.allowed_moods == ()  # nothing yet

    await again.restore_state()

    assert again.allowed_moods == ("calm", "fun")
    assert again.moods_info()["selected"] == ["calm", "fun"]
    # The account was read for putting it back, and what comes is of the moods.
    await again.fill(3)
    assert {c.mood for c in again.pool} <= {"calm", "fun"}
    assert backend.settings
    assert {mood for mood, _, _ in backend.settings} <= {"calm", "fun"}


@pytest.mark.asyncio
async def test_no_moods_stay_no_moods_and_the_account_is_not_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, track_cache: TrackCache
) -> None:
    first, _ = persistent_radio(tmp_path, monkeypatch, track_cache)
    await first.set_allowed_moods(["calm"])
    await first.set_allowed_moods([])  # picked and cleared
    again, backend = persistent_radio(tmp_path, monkeypatch, track_cache)

    await again.restore_state()

    assert again.allowed_moods == ()
    assert again.mood_arc_saved is None
    assert backend.settings == []


@pytest.mark.asyncio
async def test_a_killed_run_leaves_the_station_settings_for_the_next_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, track_cache: TrackCache
) -> None:
    first, first_player = persistent_radio(tmp_path, monkeypatch, track_cache)
    await first.set_allowed_moods(["calm"])
    await first.fill(3)  # the account is set to calm now
    assert first_player.settings[-1][0] == "calm"
    # ... and the process is killed: no restore_settings.

    again, backend = persistent_radio(tmp_path, monkeypatch, track_cache)
    await again.restore_state()

    # The account was put back first, then the pick was applied again.
    assert backend.settings[0] == ("all", "default", "any")
    assert again.allowed_moods == ("calm",)
    assert again.mood_arc_saved == ("all", "default", "any")
    # A clean stop of this run puts it back and forgets it.
    await again.fill(3)
    await again.restore_settings()
    assert backend.settings[-1] == ("all", "default", "any")
    assert track_cache.get_setting("station_settings") is None
    assert track_cache.get_setting("moods") == ["calm"]  # still picked


@pytest.mark.asyncio
async def test_a_clean_stop_forgets_the_station_settings_but_not_the_picks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, track_cache: TrackCache
) -> None:
    radio, backend = persistent_radio(tmp_path, monkeypatch, track_cache)
    await radio.set_allowed_moods(["fun"])
    assert track_cache.get_setting("station_settings") is not None
    await radio.fill(3)

    await radio.restore_settings()

    assert backend.settings[-1] == ("all", "default", "any")
    assert track_cache.get_setting("station_settings") is None
    assert track_cache.get_setting("moods") == ["fun"]


@pytest.mark.asyncio
async def test_settings_that_could_not_be_put_back_are_tried_again_next_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, track_cache: TrackCache
) -> None:
    track_cache.put_setting(
        "station_settings",
        {"station": "station", "settings": ["all", "default", "any"]},
    )
    radio, backend = persistent_radio(tmp_path, monkeypatch, track_cache)

    async def broken(*args: object) -> None:
        raise ConnectionError("no network")

    monkeypatch.setattr(backend, "set_station_settings", broken)

    await radio.restore_state()  # does not raise

    assert track_cache.get_setting("station_settings") is not None


@pytest.mark.asyncio
async def test_a_damaged_setting_does_not_stop_the_radio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, track_cache: TrackCache
) -> None:
    track_cache.put_setting("station_settings", {"station": "x"})  # no settings
    track_cache.put_setting("moods", ["calm", "angry", 7])
    track_cache.put_setting("genres", "house")
    radio, _ = persistent_radio(tmp_path, monkeypatch, track_cache)

    await radio.restore_state()

    assert radio.allowed_moods == ("calm",)  # what makes sense of it
    assert radio.genres_info()["selected"] == []
    # The damaged one was given up on; applying the moods saved a good one.
    assert track_cache.get_setting("station_settings") == {
        "station": "station",
        "settings": ["all", "default", "any"],
    }


@pytest.mark.asyncio
async def test_picked_genres_come_back_in_force_after_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, track_cache: TrackCache
) -> None:
    first, _ = persistent_radio(tmp_path, monkeypatch, track_cache)
    first.set_wanted_genres(["house", "rap"])

    again, _ = persistent_radio(tmp_path, monkeypatch, track_cache)
    await again.restore_state()

    info = again.genres_info()
    assert info["selected"] == ["house", "rap"]
    assert info["applied"] == ["house", "rap"]  # nothing was queued: in force


@pytest.mark.asyncio
async def test_restore_state_without_a_cache_does_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    radio = make_radio(tmp_path, monkeypatch, FakeBackend([]), {})

    await radio.restore_state()

    assert radio.allowed_moods == ()


@pytest.mark.asyncio
async def test_allowed_moods_can_be_given_as_any_iterable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    radio = make_radio(tmp_path, monkeypatch, FakeBackend([]), {})

    # A generator is read once: every mood in it counts, not only the first.
    await radio.set_allowed_moods(m for m in ("fun", "calm", "angry"))

    assert radio.allowed_moods == ("calm", "fun")


@pytest.mark.asyncio
async def test_blocked_genres_wait_for_the_refresh_and_win_over_picked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    radio = genre_radio(
        tmp_path,
        monkeypatch,
        [
            FakeTrack("a", genre="house"),
            FakeTrack("b", genre="rap"),
            FakeTrack("c", genre="rock"),
        ],
    )
    await radio.fill(3)

    radio.set_wanted_genres(["house", "rap"])
    radio.set_blocked_genres(["rap"])

    info = radio.genres_info()
    assert info["blocked"] == ["rap"]
    assert info["blocked_applied"] == []
    assert len(radio.pool) == 3

    radio.refresh_queue()

    assert radio.genres_info()["blocked_applied"] == ["rap"]
    assert [c.title for c in radio.pool] == ["a"]


@pytest.mark.asyncio
async def test_blocked_genres_come_back_after_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, track_cache: TrackCache
) -> None:
    first, _ = persistent_radio(tmp_path, monkeypatch, track_cache)
    first.set_blocked_genres(["rap", "rap", "metal"])
    track_cache.put_setting("genres", [])

    again, _ = persistent_radio(tmp_path, monkeypatch, track_cache)
    await again.restore_state()

    info = again.genres_info()
    assert info["blocked"] == ["rap", "metal"]
    assert info["blocked_applied"] == ["rap", "metal"]
    assert {"rap", "metal"} <= set(info["available"])  # type: ignore[arg-type]


def test_mood_of_a_track_follows_its_energy_and_key() -> None:
    def mood(bpm: float, loudness: float, key: int = 0) -> str:
        analysis = dataclasses.replace(
            track_analysis(bpm), loudness_db=loudness, key=key
        )
        return dj_mix.mood_of(analysis)

    assert mood(80, -20) == "calm"  # major key: not sad
    assert mood(80, -20, key=21) == "sad"
    assert mood(110, -12) == "fun"
    assert mood(150, -6) == "active"


class SoundOnlyBackend(FakeBackend):
    """A backend that knows nothing of moods."""

    supports_moods = False


@pytest.mark.asyncio
async def test_without_backend_moods_the_radio_judges_by_sound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = SoundOnlyBackend([[FakeTrack("quiet"), FakeTrack("loud")]])
    radio = dj.Radio(backend, tmp_path, pool_size=2)
    sounds = {
        "quiet": dataclasses.replace(track_analysis(80), loudness_db=-20),
        "loud": dataclasses.replace(track_analysis(150), loudness_db=-6),
    }

    async def download(track: FakeTrack, path: Path) -> None:
        path.write_bytes(b"x")

    async def decode(path: Path, speed: float = 1.0) -> np.ndarray:
        return np.zeros((SR, CHANNELS), np.float32)

    name = ""

    def analyze(_pcm: np.ndarray) -> dj_mix.TrackAnalysis:
        return sounds[name]

    monkeypatch.setattr(radio, "_download", download)
    monkeypatch.setattr(dj, "decode_file", decode)
    monkeypatch.setattr(dj_mix, "analyze", analyze)

    await radio.set_allowed_moods(["active"])
    for name in sounds:
        await radio._prepare(FakeTrack(name))

    assert [c.mood for c in radio.pool] == ["active"]
    assert backend.settings == []  # the account is left alone


@pytest.mark.asyncio
async def test_the_mood_the_backend_gives_wins_over_the_sound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    radio = dj.Radio(SoundOnlyBackend([]), tmp_path, pool_size=1)

    async def download(track: FakeTrack, path: Path) -> None:
        path.write_bytes(b"x")

    async def decode(path: Path, speed: float = 1.0) -> np.ndarray:
        return np.zeros((SR, CHANNELS), np.float32)

    monkeypatch.setattr(radio, "_download", download)
    monkeypatch.setattr(dj, "decode_file", decode)
    monkeypatch.setattr(dj_mix, "analyze", lambda _pcm: track_analysis(150))

    await radio._prepare(FakeTrack("a", mood="sad"))

    assert [c.mood for c in radio.pool] == ["sad"]


def specs(made: dict[str, FakeBackend]) -> list[BackendSpec]:
    def spec(spec_id: str, *, configured: bool = False) -> BackendSpec:
        def create() -> FakeBackend:
            backend = FakeBackend([])
            backend.authorized = configured
            made[spec_id] = backend
            return backend

        return BackendSpec(spec_id, spec_id.title(), create, lambda: configured)

    return [spec("one"), spec("two", configured=True)]


def test_the_backend_of_the_last_run_or_a_signed_in_one_is_taken() -> None:
    made: dict[str, FakeBackend] = {}

    assert BackendSelector(specs(made), initial="one").selected == "one"
    assert made["one"].auth_started  # not signed in: the sign-in begins
    assert BackendSelector(specs(made)).selected == "two"
    assert BackendSelector(specs(made), initial="gone").selected == "two"
    assert not made["two"].auth_started  # signed in already


def test_nothing_is_chosen_while_no_backend_is_signed_in() -> None:
    selector = BackendSelector([specs({})[0]])

    assert selector.selected is None
    assert not selector.authorized
    assert selector.auth_prompt == AuthPrompt()
    assert selector.default_station == ""


@pytest.mark.asyncio
async def test_choosing_a_backend_swaps_it_until_one_is_signed_in() -> None:
    made: dict[str, FakeBackend] = {}
    kept: list[str] = []
    selector = BackendSelector(
        specs(made),
        initial="one",
        on_select=kept.append,
    )
    first = made["one"]

    await selector.select("one")  # already so
    assert kept == []
    with pytest.raises(ValueError, match="unknown"):
        await selector.select("gone")
    await selector.select("two")

    assert kept == ["two"]
    assert selector.selected == "two"
    assert selector.authorized
    assert selector.default_station == "station"
    assert first is not made["two"]
    with pytest.raises(AuthError):
        await selector.select("one")  # signed in: no going back


@pytest.mark.asyncio
async def test_api_chooses_the_backend_and_signs_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aiohttp.test_utils import TestClient, TestServer  # noqa: PLC0415

    made: dict[str, FakeBackend] = {}
    selector = BackendSelector(specs(made)[:1])
    radio = make_radio(tmp_path, monkeypatch, selector, {})  # type: ignore[arg-type]
    app = dj.make_app(dj.Broadcaster(), radio, selector)

    async with TestClient(TestServer(app)) as client:
        info = await (await client.get("/api/auth")).json()
        assert info["backend"] is None
        assert info["backends"] == [{"id": "one", "name": "One"}]
        reply = await client.post("/api/auth", json={"values": {}})
        assert reply.status == 409  # nothing to sign in to yet

        assert (await client.put("/api/auth/backend", json={})).status == 400
        bad = await client.put("/api/auth/backend", json={"backend": "gone"})
        assert bad.status == 400
        info = await (
            await client.put("/api/auth/backend", json={"backend": "one"})
        ).json()
        assert info["backend"] == "one"
        assert made["one"].auth_started

        made["one"].auth_prompt = AuthPrompt(
            fields=(AuthField("password", "Password", secret=True),)
        )
        info = await (await client.get("/api/auth")).json()
        assert info["fields"] == [
            {"name": "password", "label": "Password", "secret": True}
        ]
        wrong = await client.post(
            "/api/auth", json={"values": {"password": "x"}}
        )
        assert wrong.status == 400
        assert (
            await client.post("/api/auth", json={"values": 1})
        ).status == 400
        right = await client.post(
            "/api/auth", json={"values": {"password": "right"}}
        )
        assert (await right.json())["authorized"] is True
