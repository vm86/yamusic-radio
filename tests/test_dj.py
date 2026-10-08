# ruff: noqa: S101, SLF001, PLR2004, ARG001, ARG002, ASYNC240
# mypy: ignore-errors
from __future__ import annotations

import asyncio
import itertools
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

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

    def __init__(self, name: str, artist: str = "") -> None:
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
) -> dj.Radio:
    """Make a radio whose tracks are 200 s of silence with a given tempo."""
    radio = dj.Radio(player, None, tmp_path, pool_size=4, mood_arc=mood_arc)

    async def download(track: FakeTrack, path: Path) -> None:
        path.write_bytes(b"x")

    async def prepare(track: FakeTrack, mood: str = "") -> None:
        radio._pool.append(
            dj.Candidate(
                track.title,
                tmp_path / f"{track.track_id}.mp3",
                track_analysis(bpms.get(track.track_id, 120)),
                frozenset(track.artists_name()),
                track,
                mood,
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
