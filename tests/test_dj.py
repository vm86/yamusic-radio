# ruff: noqa: S101, SLF001, PLR2004, ARG001, ARG002, ASYNC240
# mypy: ignore-errors
from __future__ import annotations

import asyncio
import dataclasses
import itertools
from typing import TYPE_CHECKING

import pytest
from aiohttp import web

if TYPE_CHECKING:
    from pathlib import Path

    from yandex_music_radio_dj.cache import TrackCache

np = pytest.importorskip("numpy")

from yandex_music_radio_dj import dj, dj_mix  # noqa: E402

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


def test_stretched_scales_positions() -> None:
    base = dj_mix.TrackAnalysis(100, 1000, 3000, 500, 20000, -9)

    fast = dj_mix.stretched(base, 1.25)

    assert fast.bpm == 125
    assert (fast.head_beat, fast.tail_beat) == (800, 2400)
    assert (fast.start, fast.end) == (400, 16000)


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

    def __init__(self, decks: list[dj.Deck]) -> None:
        self.decks = decks
        self.asked: list[dj_mix.TrackAnalysis | None] = []
        self.events: list[tuple[str, str]] = []

    async def next_deck(self, current: dj_mix.TrackAnalysis | None) -> dj.Deck:
        self.asked.append(current)
        if not self.decks:
            raise RuntimeError("out of tracks")
        return self.decks.pop(0)

    def is_stale(self, deck: dj.Deck) -> bool:
        return False

    def transition(
        self, kind: dj_mix.Transition, current: dj.Deck, following: dj.Deck
    ) -> None:
        self.events.append(("transition", f"{current.title}>{following.title}"))

    def started(self, deck: dj.Deck) -> None:
        self.events.append(("started", deck.title))

    def finished(self, deck: dj.Deck) -> None:
        self.events.append(("finished", deck.title))


def deck(
    title: str, bpm: float, first_beat: float, *, matched: bool
) -> dj.Deck:
    pcm = click_track(bpm, 90, first_beat)
    return dj.Deck(title, pcm, dj_mix.analyze(pcm), beat_matched=matched)


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


def test_stretched_scales_sections() -> None:
    base = dj_mix.TrackAnalysis(100, 0, 0, 0, 20000, -9, 1000, 18000)

    fast = dj_mix.stretched(base, 2.0)

    assert (fast.intro_end, fast.outro_start) == (500, 9000)
    unknown = dj_mix.stretched(dj_mix.TrackAnalysis(100, 0, 0, 0, 9, -9), 2.0)
    assert (unknown.intro_end, unknown.outro_start) == (-1, -1)


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
    assert playing.bpm == pytest.approx(120)  # it is played at our tempo


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
    """The bits of ExtendTrack that the radio uses."""

    def __init__(self, name: str, artist: str = "", genre: str = "") -> None:
        self.genre = genre
        self.track_id = name
        self.title = name
        self.codec = "mp3"
        self.bitrate_in_kbps = 192
        self.batch_id = "batch"
        self._artist = artist or name

    def artists_name(self) -> list[str]:
        return [self._artist]


class FakePlayer:
    """A station that gives out the batches it is told to."""

    def __init__(self, batches: list[list[FakeTrack]]) -> None:
        self.batches = batches
        self.requests = 0
        self.feedback: list[tuple[str, str, float]] = []
        self.settings: list[tuple[str, str, str]] = []
        self.is_init = True
        self.auth_url: str | None = None
        self.user_code: str | None = None

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

    async def get_download_links(self, *args: object) -> list[str]:
        return ["http://example.invalid/track"]

    async def feedback_track(
        self,
        track_id: str,
        kind: str,
        station_id: str,
        batch_id: str,
        seconds: float,
    ) -> None:
        self.feedback.append((track_id, kind, seconds))


def make_radio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    player: FakePlayer,
    bpms: dict[str, float],
    *,
    mood_arc: bool = False,
    cache: TrackCache | None = None,
) -> dj.Radio:
    """Make a radio whose tracks are 200 s of silence with a given tempo."""
    radio = dj.Radio(
        player, None, tmp_path, pool_size=4, mood_arc=mood_arc, cache=cache
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
                frozenset(track.artists_name()),
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
    return dj_mix.TrackAnalysis(bpm, 0, 0, 0, 200 * SR, -10, 20 * SR, 180 * SR)


@pytest.mark.asyncio
async def test_played_tracks_are_marked_and_not_picked_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    player = FakePlayer([[FakeTrack("a"), FakeTrack("b"), FakeTrack("c")]])
    radio = make_radio(tmp_path, monkeypatch, player, {})
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
    player = FakePlayer(
        [
            [FakeTrack("fast")],
            [FakeTrack("x1"), FakeTrack("x2"), FakeTrack("good")],
        ]
    )
    radio = make_radio(
        tmp_path,
        monkeypatch,
        player,
        {"fast": 175, "x1": 175, "x2": 175, "good": 121},
    )
    await radio.fill(1)

    deck = await radio.next_deck(track_analysis(120))

    assert deck.title == "good"
    assert player.requests == 2
    assert len(radio.pool) == 3  # the others wait for their turn


@pytest.mark.asyncio
async def test_loading_more_stops_at_six_extra_tracks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clashing = {f"t{i}": 175 for i in range(20)}
    player = FakePlayer(
        [[FakeTrack(f"t{3 * i + k}") for k in range(3)] for i in range(7)]
    )
    radio = make_radio(tmp_path, monkeypatch, player, clashing)
    await radio.fill(1)
    loaded_before = len(radio.pool)

    await radio.next_deck(track_analysis(120))

    # One track was taken, six extra ones were loaded: no more.
    assert len(radio.pool) + 1 - loaded_before == dj.MAX_EXTRA_TRACKS


@pytest.mark.asyncio
async def test_oldest_played_track_is_repeated_when_the_station_repeats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    player = FakePlayer([[FakeTrack("a"), FakeTrack("b")]])
    radio = make_radio(tmp_path, monkeypatch, player, {})
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
    player = FakePlayer([[FakeTrack("a")]])
    radio = make_radio(tmp_path, monkeypatch, player, {})
    await radio.fill(1)
    deck = await radio.next_deck(None)

    radio.started(deck)
    radio.finished(deck)
    await asyncio.gather(*radio._feedback_tasks)

    assert player.feedback == [
        ("a", "trackStarted", 0.0),
        ("a", "trackFinished", 200.0),
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
    player = FakePlayer(
        [[FakeTrack(f"t{3 * r + i}") for i in range(3)] for r in range(4)]
    )
    radio = make_radio(tmp_path, monkeypatch, player, {}, mood_arc=True)

    await radio.start_mood_arc()
    await radio.fill(12)
    await radio.restore_settings()

    # Rounds of three tracks, each for the mood of the place it will be at.
    assert player.settings == [
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
    player = FakePlayer([[FakeTrack("a"), FakeTrack("b")]])
    radio = make_radio(tmp_path, monkeypatch, player, {})

    await radio.fill(2)
    await radio.restore_settings()

    assert player.settings == []


@pytest.mark.asyncio
async def test_played_tracks_are_not_picked_again_after_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, track_cache: TrackCache
) -> None:
    cache = track_cache
    tracks = [FakeTrack("a"), FakeTrack("b"), FakeTrack("c")]
    radio = make_radio(
        tmp_path, monkeypatch, FakePlayer([tracks]), {}, cache=cache
    )
    await radio.fill(2)
    for name in "abc":  # as _prepare does
        cache.put_analysis(name, "mp3", name, track_analysis(120))
    first = await radio.next_deck(None)

    restarted = make_radio(
        tmp_path,
        monkeypatch,
        FakePlayer([[FakeTrack("a"), FakeTrack("b"), FakeTrack("c")]]),
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
    radio = dj.Radio(FakePlayer([]), None, tmp_path, pool_size=1, cache=cache)
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
    player = FakePlayer([names[i : i + 3] for i in range(0, 30, 3)])
    radio = make_radio(tmp_path, monkeypatch, player, {})
    await radio.fill(4)  # tracks of the station's own mood first
    assert {c.mood for c in radio.pool} == {""}

    await radio.set_allowed_moods(["active", "calm", "calm"])

    assert radio.allowed_moods == ("calm", "active")
    assert radio.pool == []  # the others were dropped
    await radio.fill(4)
    deck = await radio.next_deck(None)
    assert {c.mood for c in radio.pool} <= {"calm", "active"}
    assert deck.mood in {"calm", "active"}
    assert {s[0] for s in player.settings} <= {"calm", "active", "all"}

    await radio.set_allowed_moods([])
    assert player.settings[-1][0] == "all"  # the account is as it was


@pytest.mark.asyncio
async def test_queue_shows_previous_now_and_next_tracks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    player = FakePlayer([[FakeTrack(n) for n in "abcdefg"]])
    radio = make_radio(tmp_path, monkeypatch, player, {})
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

    player = FakePlayer([[FakeTrack(n) for n in "abcd"]])
    radio = make_radio(tmp_path, monkeypatch, player, {})
    await radio.fill(4)
    deck = await radio.next_deck(None)
    radio.started(deck)
    app = dj.make_app(dj.Broadcaster(), radio)

    async with TestClient(TestServer(app)) as client:
        page = await client.get("/")
        assert page.status == 200
        assert page.content_type == "text/html"
        assert "Яндекс Музыка DJ" in await page.text()

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

    player = FakePlayer([])
    player.is_init = False
    radio = make_radio(tmp_path, monkeypatch, player, {})
    app = dj.make_app(dj.Broadcaster(), radio)

    async with TestClient(TestServer(app)) as client:
        waiting = await (await client.get("/api/auth")).json()
        assert waiting == {"authorized": False, "url": None, "code": None}

        player.auth_url = "https://ya.example/device"
        player.user_code = "ABCD1234"
        waiting = await (await client.get("/api/auth")).json()
        assert waiting["url"] == "https://ya.example/device"
        assert waiting["code"] == "ABCD1234"
        # The account's settings can't be changed yet.
        reply = await client.put("/api/moods", json={"moods": ["calm"]})
        assert reply.status == 503
        assert (await client.delete("/api/moods")).status == 503
        assert (await client.get("/api/queue")).status == 200

        player.is_init = True
        done = await (await client.get("/api/auth")).json()
        assert done == {"authorized": True, "url": None, "code": None}


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


def test_estimate_key_of_noise_is_unknown() -> None:
    noise = np.random.default_rng(1).normal(0, 0.1, 20 * SR)

    assert dj_mix.estimate_key(noise.astype(np.float32)) == -1
    assert dj_mix.estimate_key(np.zeros(100, dtype=np.float32)) == -1


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
    player = FakePlayer([[FakeTrack("a"), FakeTrack("b"), FakeTrack("c")]])
    radio = make_radio(tmp_path, monkeypatch, player, {})
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
    player = FakePlayer([[FakeTrack("a"), FakeTrack("b")]])
    radio = make_radio(tmp_path, monkeypatch, player, {})
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
    player = FakePlayer([[FakeTrack("a"), FakeTrack("b")]])
    radio = make_radio(tmp_path, monkeypatch, player, {})
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
    player = FakePlayer([[FakeTrack("a")]])
    radio = make_radio(tmp_path, monkeypatch, player, {})
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
    radio = dj.Radio(FakePlayer([]), None, tmp_path, cache=track_cache)

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
        FakePlayer([[FakeTrack("a"), FakeTrack("b"), FakeTrack("c")]]),
        {},
        cache=track_cache,
    )
    await radio.fill(3)
    first = await radio.next_deck(None)
    second = await radio.next_deck(first.analysis)
    third = await radio.next_deck(second.analysis)
    first.beat_matched = third.beat_matched = False
    second.beat_matched = True
    keyed = dataclasses.replace(second.analysis, key=key_of("8A"))
    second.analysis = keyed
    third.analysis = dataclasses.replace(third.analysis, key=key_of("9A"))

    radio.transition(dj_mix.Transition.BASS_SWAP, first, second)
    radio.transition(dj_mix.Transition.FILTER_SWEEP, second, third)
    radio.transition(dj_mix.Transition.BASS_SWAP, third, first)

    stats = radio.transition_stats()
    assert stats["session"] == {
        "total": 3,
        "beat_matched": 1,
        "by_type": {"bass swap": 2, "filter sweep": 1},
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
    radio = dj.Radio(FakePlayer([]), None, tmp_path)

    assert radio.transition_stats() == {
        "session": {
            "total": 0,
            "beat_matched": 0,
            "by_type": {},
            "by_key": {},
            "by_quality": {"good": 0, "fair": 0, "poor": 0, "unknown": 0},
        },
        "all_time": None,
    }
    assert radio.key_pair_stats() == {"session": [], "all_time": None}


def genre_radio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tracks: list[FakeTrack]
) -> dj.Radio:
    return make_radio(tmp_path, monkeypatch, FakePlayer([tracks]), {})


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
    radio = dj.Radio(FakePlayer([]), None, tmp_path, cache=track_cache)
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
