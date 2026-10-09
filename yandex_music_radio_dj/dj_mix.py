"""Beat analysis and transition planning for the DJ radio mode.

A track is reduced to a few numbers (tempo, position of the first beat,
audible range, loudness). The next track is picked by tempo/loudness
proximity, time-stretched to the tempo of the one playing, and blended in
over a whole number of beats so that both beat grids coincide.

Needs numpy.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from math import ceil, cos, floor, log2, log10, pi
from typing import TYPE_CHECKING

import numpy as np
import numpy.typing as npt

if TYPE_CHECKING:
    from collections.abc import Sequence

type Pcm = npt.NDArray[np.float32]
type Spectrum = npt.NDArray[np.complexfloating]

SAMPLE_RATE = 44100
CHANNELS = 2

FRAME = 1024
HOP = 512
FRAMES_PER_SECOND = SAMPLE_RATE / HOP
BLOCK_FRAMES = 2048

MIN_BPM = 70.0
MAX_BPM = 180.0
PREFERRED_BPM = 120.0
# Reject a tempo whose autocorrelation peak is weaker than this.
MIN_TEMPO_CONFIDENCE = 0.05

# Samples quieter than this (about -40 dBFS) count as silence.
SILENCE_LEVEL = 0.01

# Largest playback speed change to match the tempo of the previous track.
MAX_TEMPO_SHIFT = 0.08
# Below this a speed change isn't worth a re-decode.
NEGLIGIBLE_TEMPO_SHIFT = 0.002

# Length of the stretch of a track the beat position is measured on.
BEAT_WINDOW_SECONDS = 30

# Blend length when the structure of the tracks is unknown.
BEATS_PER_BLEND = 16
PLAIN_BLEND_SECONDS = 8.0
# Blends follow the shorter of the outgoing outro and the incoming intro,
# within these limits (beats are counted in steps of two bars).
MIN_BLEND_BEATS = 8
MAX_BLEND_BEATS = 32
BLEND_BEATS_STEP = 8
MIN_PLAIN_BLEND_SECONDS = 4.0
MAX_PLAIN_BLEND_SECONDS = 16.0
# Intros and outros shorter than this don't count as a section.
MIN_SECTION_SECONDS = 4.0
# A bar is this many beats; the loudness is looked at one bar at a time.
BEATS_PER_BAR = 4
FALLBACK_BAR_SECONDS = 2.0
MIN_BARS_FOR_STRUCTURE = 8
# Bars quieter than the median bar by this much belong to an intro/outro.
SECTION_THRESHOLD_DB = 4.0
# A matched blend must leave at least this much of the outgoing track.
MIN_SOLO_SECONDS = 10.0

# Below the crossover the two tracks never play together (bass swap).
CROSSOVER_HZ = 200.0
# Cutoffs of the filtered copies a sweep moves between, low to high.
SWEEP_CUTOFFS_HZ = (150, 250, 400, 650, 1000, 1600, 2600, 4200, 7000, 12000)
# Share of the blend after which the sweeps are done.
SWEEP_DONE_AT = 0.85
# Spectrum padding, so that the circular FFT filter doesn't wrap around.
FFT_PADDING = 1 << 14
# Loudness gap (dB) from which a beat-matched pair is swept, not swapped.
SWEEP_LOUDNESS_GAP_DB = 3.0
# Shorter blends are plain crossfades; a swap or sweep needs room.
MIN_FILTERED_BLEND_SECONDS = 2.0

# Club effects: loudness is relative to the blend they are added to.
RISER_BEATS = 8
RISER_FLOOR_HZ = 400.0
RISER_GAIN = 1.2
IMPACT_SECONDS = 1.2
IMPACT_GAIN = 3.0
IMPACT_PEAK = 0.9
# Echo-out for blends without a shared beat grid: where the outgoing track
# is cut (share of the blend), the echo spacing and decay.
ECHO_CUT_AT = 0.55
ECHO_DELAY_SECONDS = 0.375
ECHO_REPEATS = 6
ECHO_FEEDBACK = 0.6
ECHO_DARKEN_HZ = 4000.0
CUT_FADE_SECONDS = 0.01

# Cost of a tempo ratio (per octave) against a loudness gap (per dB).
# Costs of a transition when choosing the next track. They are in the same
# unit, "how much worse than a perfect blend".
MATCHED_TEMPO_COST = 10.0  # per octave of tempo change
UNMATCHED_COST = 4.0  # beats can't be lined up
UNMATCHED_TEMPO_COST = 5.0  # per octave of tempo gap, at most one octave
UNKNOWN_TEMPO_COST = 2.5  # on top of UNMATCHED_COST
SWEEP_COST = 1.0  # a filter sweep instead of a bass swap
PLAIN_LENGTH_COST = 0.5  # no intro/outro to size the blend by
LOUDNESS_COST = 0.1  # per dB
# Harmonic mixing on the Camelot Wheel, see key_cost.
KEY_COST_SCALE = 0.7
KEY_NEIGHBOUR_COST = 0.3  # one step round the wheel, or the relative key
KEY_BOOST_COST = 1.2  # two steps up: the energy goes up with the key
KEY_SEMITONE_COST = 1.5  # seven steps up: the key goes up a semitone
KEY_DOWN_COST = 1.8  # two steps down
KEY_DIAGONAL_COST = 2.5  # a step round the wheel and the other mode
KEY_CLASH_COST = 4.0  # keys that clash
KEY_COSTS = {
    "same": 0.0,
    "neighbour": KEY_NEIGHBOUR_COST,
    "boost": KEY_BOOST_COST,
    "semitone": KEY_SEMITONE_COST,
    "down": KEY_DOWN_COST,
    "diagonal": KEY_DIAGONAL_COST,
    "clash": KEY_CLASH_COST,
}
REPEAT_ARTIST_COST = 6.0
ENERGY_ARC_COST = 8.0  # per unit of distance from the target energy
# A next track costing more than this doesn't really fit: look for others.
SUITABLE_COST = 5.0

# The queue is planned this many tracks ahead, trying the best few
# candidates at each step; later steps count for less.
LOOKAHEAD_WEIGHTS = (1.0, 0.6, 0.4)
LOOKAHEAD_BEAM = 8
# The station mood wanted at each point of the arc, calm to lively, and the
# cost of a track that was picked for another mood (per step apart).
MOODS = ("calm", "fun", "active")
# Every mood the station can be set to (quietest first); "all" is no choice.
ALL_MOODS = ("sad", "calm", "fun", "active")
MOOD_CALM_BELOW = 0.3
MOOD_FUN_BELOW = 0.5
MOOD_COST = 2.0
# A track by a recently played artist is penalized.
REPEAT_WINDOW = 4
# A set builds up and winds down over this many tracks.
ARC_TRACKS = 10
ENERGY_BPM_RANGE = (90.0, 150.0)
ENERGY_LOUDNESS_RANGE_DB = (-18.0, -6.0)


@dataclass(frozen=True, slots=True)
class TrackAnalysis:
    bpm: float  # 0 when the tempo couldn't be told
    head_beat: float  # samples, a beat near the start of the track
    tail_beat: float  # samples, a beat near the end of the track
    start: int  # first audible sample
    end: int  # one past the last audible sample
    loudness_db: float
    # Where the quiet intro ends and the quiet outro begins (samples), or
    # -1 when it couldn't be told. The stretch between is the core of the
    # track, which a blend should keep out of.
    intro_end: int = -1
    outro_start: int = -1
    # Musical key: 0-11 is a major key (C, C#, ... B), 12-23 the minor key
    # with tonic 0-11; -1 when it couldn't be told.
    key: int = -1

    @property
    def camelot(self) -> str:
        """Camelot Wheel code of the key (8B, 5A, ...), "" when unknown."""
        return camelot_code(self.key)

    @property
    def key_name(self) -> str:
        """Key as musicians write it (C major, F# minor), "" when unknown."""
        return key_name(self.key)

    @property
    def beat_period(self) -> float:
        """Samples per beat."""
        return SAMPLE_RATE * 60.0 / self.bpm


# Key detection: the pitch-class profile of the track (chroma) is compared
# with the Krumhansl-Kessler major and minor profiles.
KEY_FRAME = 8192
KEY_HOP = 16384
KEY_MIN_HZ = 110.0
KEY_MAX_HZ = 2000.0
# Frames quieter than this share of the loudest one are not looked at.
KEY_QUIET_RATIO = 0.1
# Below this correlation the track has no tonal centre worth a label.
MIN_KEY_CONFIDENCE = 0.3
PITCH_CLASSES = 12
KEYS = 2 * PITCH_CLASSES
NOTES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
_MAJOR_PROFILE = np.array(
    [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
)
_MINOR_PROFILE = np.array(
    [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
)


try:
    import essentia.standard as _essentia

    _KEY_EXTRACTOR = _essentia.KeyExtractor(
        profileType="edma", sampleRate=SAMPLE_RATE
    )
except ImportError:
    _KEY_EXTRACTOR = None
# Essentia spells the flats; the sharps are in NOTES.
_ESSENTIA_NOTES = {
    **{name: i for i, name in enumerate(NOTES)},
    "Db": 1,
    "Eb": 3,
    "Gb": 6,
    "Ab": 8,
    "Bb": 10,
}


def key_detector() -> str:
    """Name of what finds the keys: "essentia" or "chroma profiles"."""
    return "chroma profiles" if _KEY_EXTRACTOR is None else "essentia"


def camelot_code(key: int) -> str:
    """Camelot Wheel code of a key as numbered in TrackAnalysis.key."""
    if not 0 <= key < KEYS:
        return ""
    minor = key >= PITCH_CLASSES
    # The relative major of a minor key is a minor third above its tonic;
    # going up a fifth moves one step round the wheel, C major being 8B.
    major_tonic = (key + 3) % PITCH_CLASSES if minor else key
    number = (major_tonic * 7 + 7) % PITCH_CLASSES + 1
    return f"{number}{'A' if minor else 'B'}"


def camelot_key(code: str) -> int:
    """Return the key (see TrackAnalysis.key) of a Camelot code, -1 if none."""
    for key in range(KEYS):
        if camelot_code(key) == code:
            return key
    return -1


def key_name(key: int) -> str:
    if not 0 <= key < KEYS:
        return ""
    mode = "minor" if key >= PITCH_CLASSES else "major"
    return f"{NOTES[key % PITCH_CLASSES]} {mode}"


def key_relation(current: int, following: int) -> str:
    """Name the key change: a key of KEY_COSTS, or "unknown"."""
    now, then = camelot_code(current), camelot_code(following)
    if not now or not then:
        return "unknown"
    steps = (int(then[:-1]) - int(now[:-1])) % 12
    if now[-1] == then[-1]:
        relations = {
            0: "same",
            1: "neighbour",
            11: "neighbour",
            2: "boost",
            7: "semitone",
            10: "down",
        }
    else:
        relations = {0: "neighbour", 1: "diagonal", 11: "diagonal"}
    return relations.get(steps, "clash")


# How good a key change is for the listener, roughly (see key_relation).
KEY_QUALITY = {
    "same": "good",
    "neighbour": "good",
    "boost": "fair",
    "semitone": "fair",
    "down": "fair",
    "diagonal": "fair",
    "clash": "poor",
}


def key_quality(relation: str) -> str:
    """Return "good", "fair", "poor" or "unknown" for a key relation."""
    return KEY_QUALITY.get(relation, "unknown")


def quality_counts(by_key: dict[str, int]) -> dict[str, int]:
    """Sum the counts of key relations up into the qualities."""
    counts = dict.fromkeys(("good", "fair", "poor", "unknown"), 0)
    for relation, number in by_key.items():
        counts[key_quality(relation)] += number
    return counts


def key_cost(current: int, following: int) -> float:
    """Cost of a key change by the rules of harmonic mixing.

    On the Camelot Wheel a track blends cleanly into the same key, the
    keys one step round the wheel (a fifth up or down) and the relative
    key (the same number, the other letter). Two steps up lifts the
    energy, seven steps up raises the key by a semitone; both are
    allowed and cost a little. Anything else clashes. Nothing is known
    about a key that was not detected, so it costs nothing.
    """
    relation = key_relation(current, following)
    return KEY_COST_SCALE * KEY_COSTS.get(relation, 0.0)


def chroma(mono: Pcm) -> npt.NDArray[np.float64]:
    """Energy of the track at each of the 12 pitch classes (C = 0)."""
    freqs = np.fft.rfftfreq(KEY_FRAME, 1 / SAMPLE_RATE)
    usable = (freqs >= KEY_MIN_HZ) & (freqs <= KEY_MAX_HZ)
    pitch_class = (
        np.round(12 * np.log2(freqs[usable] / 440.0) + 9).astype(int) % 12
    )
    starts = np.arange(0, len(mono) - KEY_FRAME, KEY_HOP)
    if not len(starts):
        return np.zeros(12)
    window = np.hanning(KEY_FRAME).astype(np.float32)
    frames = np.stack([mono[i : i + KEY_FRAME] * window for i in starts])
    # The square root keeps a loud bass note from drowning the harmony.
    magnitude = np.sqrt(np.abs(np.fft.rfft(frames, axis=1))[:, usable])
    per_frame = np.stack(
        [
            np.bincount(pitch_class, weights=row, minlength=12)
            for row in magnitude
        ]
    )
    totals = per_frame.sum(axis=1)
    per_frame = per_frame[totals > KEY_QUIET_RATIO * totals.max()]
    # Every frame counts the same, whatever its loudness.
    per_frame = per_frame / per_frame.sum(axis=1, keepdims=True)
    total: npt.NDArray[np.float64] = per_frame.sum(axis=0)
    return total


def _profile_key(mono: Pcm) -> tuple[int, float]:
    """Return the key whose profile fits the chroma best, and how well."""
    profile = chroma(mono)
    if not profile.any():
        return -1, 0.0
    best, best_score = -1, 0.0
    for offset, reference in (
        (0, _MAJOR_PROFILE),
        (PITCH_CLASSES, _MINOR_PROFILE),
    ):
        for tonic in range(PITCH_CLASSES):
            score = float(np.corrcoef(profile, np.roll(reference, tonic))[0, 1])
            if score > best_score:
                best, best_score = offset + tonic, score
    return best, best_score


def _essentia_key(mono: Pcm) -> int:
    """Key by Essentia, with the profile made for electronic music.

    -1 when Essentia has no answer: it gave no key, or failed.
    """
    try:
        name, scale, strength = _KEY_EXTRACTOR(mono)
    except RuntimeError:
        return -1
    note = _ESSENTIA_NOTES.get(name)
    if note is None or not strength:
        return -1
    return note + (PITCH_CLASSES if scale == "minor" else 0)


def estimate_key(mono: Pcm) -> int:
    """Key of the track (see TrackAnalysis.key), -1 if there is none.

    Essentia tells the key much better than the plain profile match
    (see README) but always names one, even for noise, so the plain
    match decides first whether the track has a tonal centre at all. The
    key is then Essentia's; where Essentia has no answer, or is not
    installed, it is the key of the plain match.
    """
    key, confidence = _profile_key(mono)
    if confidence < MIN_KEY_CONFIDENCE:
        return -1
    if _KEY_EXTRACTOR is not None:
        found = _essentia_key(mono)
        if found >= 0:
            return found
    return key


def onset_envelope(mono: Pcm) -> npt.NDArray[np.float32]:
    """Spectral flux per hop: how much new sound appears at that moment."""
    windows = np.lib.stride_tricks.sliding_window_view(mono, FRAME)[::HOP]
    count = len(windows)
    window = np.hanning(FRAME).astype(np.float32)
    flux = np.zeros(count, dtype=np.float32)
    previous: npt.NDArray[np.float32] | None = None
    for first in range(0, count, BLOCK_FRAMES):
        block = windows[first : first + BLOCK_FRAMES] * window
        spectrum = np.log1p(10 * np.abs(np.fft.rfft(block, axis=1)))
        spectrum = spectrum.astype(np.float32)
        # The first frame of a block is compared with the last of the
        # previous one.
        row = first + 1
        if previous is not None:
            spectrum = np.vstack([previous, spectrum])
            row = first
        rise = np.clip(np.diff(spectrum, axis=0), 0, None).sum(axis=1)
        flux[row : row + len(rise)] = rise
        previous = spectrum[-1:]
    # Drop the slow trend so that only the peaks remain.
    kernel = np.ones(int(FRAMES_PER_SECOND / 2)) / int(FRAMES_PER_SECOND / 2)
    trend = np.convolve(flux, kernel, mode="same")
    return np.clip(flux - trend, 0, None).astype(np.float32)


def estimate_tempo(flux: npt.NDArray[np.float32]) -> float:
    """Tempo in BPM from the autocorrelation of the onset envelope."""
    count = len(flux)
    min_lag = int(FRAMES_PER_SECOND * 60 / MAX_BPM)
    max_lag = int(FRAMES_PER_SECOND * 60 / MIN_BPM)
    if count < 4 * max_lag or not flux.any():
        return 0.0

    # Without the mean even noise would correlate with itself.
    spectrum = np.fft.rfft(flux - flux.mean(), 2 * count)
    correlation = np.fft.irfft(np.abs(spectrum) ** 2)[:count]
    correlation = correlation / correlation[0]

    lags = np.arange(min_lag, max_lag + 1)
    score = correlation[lags] + 0.5 * correlation[lags * 2]
    # Prefer tempos near the middle of the range when peaks tie.
    bpm = 60 * FRAMES_PER_SECOND / lags
    score = score * np.exp(-0.5 * (np.log2(bpm / PREFERRED_BPM) / 1.0) ** 2)
    best = int(np.argmax(score))
    if correlation[lags[best]] < MIN_TEMPO_CONFIDENCE:
        return 0.0

    # Parabolic interpolation around the peak for a fractional lag.
    lag = float(lags[best])
    if 0 < best < len(lags) - 1:
        left, mid, right = (correlation[lags[best] + d] for d in (-1, 0, 1))
        denominator = left - 2 * mid + right
        if denominator < 0:
            lag += 0.5 * (left - right) / denominator
    return float(60 * FRAMES_PER_SECOND / lag)


def _comb_scores(
    flux: npt.NDArray[np.float32],
    period: float,
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """Onset energy found on a beat grid, for every grid phase."""
    offsets = np.arange(0, period, 0.5)
    beats = np.arange(int(len(flux) / period) - 1)
    positions = offsets[:, None] + period * beats[None, :]
    return offsets, flux[positions.astype(int)].sum(axis=1)


def refine_tempo(flux: npt.NDArray[np.float32], bpm: float) -> float:
    """Fine tuning: the tempo whose beat grid fits the onsets best.

    The autocorrelation is precise to about a percent -- over a whole track
    that is a drift of seconds, too much to line two beat grids up.
    """
    best_bpm, best_score = bpm, -1.0
    for shift in np.linspace(-0.02, 0.02, 81):
        candidate = bpm * (1 + float(shift))
        _, scores = _comb_scores(flux, 60 * FRAMES_PER_SECOND / candidate)
        if scores.max() > best_score:
            best_bpm, best_score = candidate, float(scores.max())
    return best_bpm


def find_beat(
    flux: npt.NDArray[np.float32],
    bpm: float,
    first_frame: int,
    last_frame: int,
) -> float:
    """Position, in samples, of a beat inside the given frame range."""
    period = 60 * FRAMES_PER_SECOND / bpm
    offsets, scores = _comb_scores(flux[first_frame:last_frame], period)
    best = float(offsets[int(np.argmax(scores))]) + first_frame
    return best * HOP + FRAME / 2


def audible_range(pcm: Pcm) -> tuple[int, int]:
    loud = np.flatnonzero(np.abs(pcm).max(axis=1) > SILENCE_LEVEL)
    if not len(loud):
        return 0, len(pcm)
    return int(loud[0]), int(loud[-1]) + 1


def find_sections(mono: Pcm, start: int, bar: int) -> tuple[int, int]:
    """Return where the intro ends and the outro starts, from the loudness.

    The track is cut into bars; the intro is everything before the first
    bar that is as loud as the track usually is, the outro everything
    after the last one. (-1, -1) when the track is too short to tell.
    """
    count = len(mono) // bar
    if count < MIN_BARS_FOR_STRUCTURE:
        return -1, -1
    bars = mono[: count * bar].reshape(count, bar).astype(np.float64)
    levels = 10 * np.log10(np.mean(bars**2, axis=1) + 1e-12)
    loud = np.flatnonzero(levels >= np.median(levels) - SECTION_THRESHOLD_DB)
    return start + int(loud[0]) * bar, start + (int(loud[-1]) + 1) * bar


def _snap(position: int, grid_beat: float, bar: float, *, up: bool) -> int:
    """Move a position onto the bar grid, later (up) or earlier."""
    bars = (position - grid_beat) / bar
    return round(grid_beat + (ceil(bars) if up else floor(bars)) * bar)


def analyze(pcm: Pcm) -> TrackAnalysis:
    start, end = audible_range(pcm)
    body = pcm[start:end]
    mono = np.ascontiguousarray(body.mean(axis=1), dtype=np.float32)

    bpm = 0.0
    head_beat = tail_beat = 0.0
    if len(mono) > 2 * FRAME:
        flux = onset_envelope(mono)
        bpm = estimate_tempo(flux)
        if bpm:
            bpm = refine_tempo(flux, bpm)
            # The grid is only known to be exact near where it was
            # measured, so measure it where the tracks will be blended.
            edge = int(BEAT_WINDOW_SECONDS * FRAMES_PER_SECOND)
            head_beat = start + find_beat(flux, bpm, 0, edge)
            tail_beat = start + find_beat(
                flux, bpm, len(flux) - edge, len(flux)
            )

    bar = BEATS_PER_BAR * SAMPLE_RATE * 60 / bpm if bpm else 0
    intro_end, outro_start = find_sections(
        mono, start, round(bar or FALLBACK_BAR_SECONDS * SAMPLE_RATE)
    )
    if bpm and intro_end >= 0:
        # A transition should land on a bar line of the track it is in.
        intro_end = min(_snap(intro_end, head_beat, bar, up=True), end)
        outro_start = max(_snap(outro_start, tail_beat, bar, up=False), start)
        if outro_start <= intro_end:
            intro_end = outro_start = -1

    rms = float(np.sqrt(np.mean(np.square(body, dtype=np.float64)) + 1e-12))
    return TrackAnalysis(
        bpm=bpm,
        head_beat=head_beat,
        tail_beat=tail_beat,
        start=start,
        end=end,
        loudness_db=20 * log10(rms),
        intro_end=intro_end,
        outro_start=outro_start,
        key=estimate_key(mono),
    )


def tempo_ratio(target_bpm: float, bpm: float) -> float:
    """Playback speed bringing bpm to target (half/double time allowed)."""
    ratios = (target_bpm / (bpm * factor) for factor in (0.5, 1.0, 2.0))
    return min(ratios, key=lambda ratio: abs(log2(ratio)))


def transition_speed(current_bpm: float, following: TrackAnalysis) -> float:
    """Speed to play the following track at, 0 when it can't be matched."""
    if not current_bpm or not following.bpm:
        return 0.0
    speed = tempo_ratio(current_bpm, following.bpm)
    if abs(speed - 1) > MAX_TEMPO_SHIFT:
        return 0.0
    return 1.0 if abs(speed - 1) < NEGLIGIBLE_TEMPO_SHIFT else speed


def stretched(analysis: TrackAnalysis, speed: float) -> TrackAnalysis:
    """Return the analysis of the track played `speed` times faster."""
    if speed == 1.0:
        return analysis
    return replace(
        analysis,
        bpm=analysis.bpm * speed,
        head_beat=analysis.head_beat / speed,
        tail_beat=analysis.tail_beat / speed,
        start=round(analysis.start / speed),
        end=round(analysis.end / speed),
        intro_end=round(analysis.intro_end / speed)
        if analysis.intro_end >= 0
        else -1,
        outro_start=round(analysis.outro_start / speed)
        if analysis.outro_start >= 0
        else -1,
    )


def section_length(
    current: TrackAnalysis, following: TrackAnalysis
) -> int | None:
    """Return the shorter of the outgoing outro and the incoming intro.

    This is how Mixxx's AutoDJ sizes a transition: both sections play out
    in full, none of the core of either track is covered. None when one of
    the sections is unknown or too short to speak of.
    """
    if current.outro_start < 0 or following.intro_end < 0:
        return None
    outro = current.end - current.outro_start
    intro = following.intro_end - following.start
    shortest = min(outro, intro)
    if shortest < MIN_SECTION_SECONDS * SAMPLE_RATE:
        return None
    return shortest


def _blend_beats(section: int | None, period: float) -> int:
    if section is None:
        return BEATS_PER_BLEND
    beats = round(section / period / BLEND_BEATS_STEP) * BLEND_BEATS_STEP
    return min(max(beats, MIN_BLEND_BEATS), MAX_BLEND_BEATS)


def _plan_matched(
    current: TrackAnalysis, following: TrackAnalysis
) -> tuple[int, int, int] | None:
    period = current.beat_period
    solo = round(MIN_SOLO_SECONDS * SAMPLE_RATE)

    # The first beat of the incoming track after its silence.
    j = ceil((following.start - following.head_beat) / following.beat_period)
    following_start = max(
        round(following.head_beat + j * following.beat_period), 0
    )

    section = section_length(current, following)
    beats = _blend_beats(section, period)
    while True:
        length = round(beats * period)
        # The blend ends with the outgoing track, on a beat of its grid.
        k = floor((current.end - length - current.tail_beat) / period)
        current_start = round(current.tail_beat + k * period)
        if section is None or beats <= MIN_BLEND_BEATS:
            break
        # Rounding up to whole bars mustn't reach into the cores.
        into_outgoing_core = current_start < current.outro_start - period / 2
        into_incoming_core = (
            following_start + length > following.intro_end + period / 2
        )
        if not (into_outgoing_core or into_incoming_core):
            break
        beats -= BLEND_BEATS_STEP

    if current_start < current.start + solo:
        return None
    length = min(current.end - current_start, following.end - following_start)
    return current_start, following_start, length


def plan_blend(
    current: TrackAnalysis,
    following: TrackAnalysis,
    *,
    beat_matched: bool,
) -> tuple[int, int, int]:
    """Where the blend starts in each track and how long it lasts.

    Returns (start in current, start in following, length), in samples.
    """
    if beat_matched and current.bpm and following.bpm:
        planned = _plan_matched(current, following)
        if planned is not None:
            return planned

    section = section_length(current, following)
    seconds = PLAIN_BLEND_SECONDS
    if section is not None:
        seconds = min(
            max(section / SAMPLE_RATE, MIN_PLAIN_BLEND_SECONDS),
            MAX_PLAIN_BLEND_SECONDS,
        )
    length = min(
        round(seconds * SAMPLE_RATE),
        (current.end - current.start) // 2,
        (following.end - following.start) // 2,
    )
    return current.end - length, following.start, length


def crossfade(outgoing: Pcm, incoming: Pcm) -> Pcm:
    """Equal-power blend of two equally long pieces."""
    fade = np.linspace(0.0, np.pi / 2, len(outgoing), dtype=np.float32)
    blended = (
        outgoing * np.cos(fade)[:, None] + incoming * np.sin(fade)[:, None]
    )
    return blended.astype(np.float32)


class Transition(StrEnum):
    CROSSFADE = "crossfade"
    BASS_SWAP = "bass swap"
    FILTER_SWEEP = "filter sweep"


def choose_transition(
    current: TrackAnalysis,
    following: TrackAnalysis,
    *,
    beat_matched: bool,
) -> Transition:
    """Pick the kind of blend that suits the pair of tracks.

    Two tracks on the same beat grid can share the highs while the bass
    is handed over. Without a shared grid (or with a big jump in
    loudness) the beats would clash, so one track is filtered away while
    the other is filtered in, which hides the mismatch.
    """
    loudness_gap = abs(current.loudness_db - following.loudness_db)
    if beat_matched and loudness_gap < SWEEP_LOUDNESS_GAP_DB:
        return Transition.BASS_SWAP
    return Transition.FILTER_SWEEP


def _lowpass(
    spectrum: Spectrum,
    size: int,
    frames: int,
    cutoff: float,
) -> Pcm:
    """Zero-phase low-pass (4th order Butterworth magnitude)."""
    freqs = np.fft.rfftfreq(size, 1 / SAMPLE_RATE)
    gain = 1 / np.sqrt(1 + (freqs / cutoff) ** 8)
    filtered = np.fft.irfft(spectrum * gain[:, None], size, axis=0)
    return filtered[:frames].astype(np.float32)


def _spectrum(pcm: Pcm) -> tuple[Spectrum, int]:
    size = 1 << (len(pcm) + FFT_PADDING - 1).bit_length()
    return np.fft.rfft(pcm, size, axis=0), size


def swap_point(frames: int, beat_period: float) -> tuple[float, float]:
    """Return where the bass changes hands and how long it takes."""
    if not beat_period:
        return frames / 2, SAMPLE_RATE / 4
    return round(frames / 2 / beat_period) * beat_period, beat_period / 2


def bass_swap(outgoing: Pcm, incoming: Pcm, beat_period: float = 0) -> Pcm:
    """Blend the highs, hand the bass over in one go in the middle.

    Two basslines at once turn the low end to mud, so the outgoing bass
    stays until the swap point and the incoming one only starts there.
    The swap is on a beat when the beat period is known.
    """
    frames = len(outgoing)
    out_spec, size = _spectrum(outgoing)
    in_spec, _ = _spectrum(incoming)
    out_low = _lowpass(out_spec, size, frames, CROSSOVER_HZ)
    in_low = _lowpass(in_spec, size, frames, CROSSOVER_HZ)

    swap, ramp = swap_point(frames, beat_period)
    handover = np.clip((np.arange(frames) - (swap - ramp)) / (2 * ramp), 0, 1)
    low_angle = (handover * np.pi / 2).astype(np.float32)
    high_angle = np.linspace(0.0, np.pi / 2, frames, dtype=np.float32)

    result: Pcm = (
        out_low * np.cos(low_angle)[:, None]
        + in_low * np.sin(low_angle)[:, None]
        + (outgoing - out_low) * np.cos(high_angle)[:, None]
        + (incoming - in_low) * np.sin(high_angle)[:, None]
    )
    return result.astype(np.float32)


def _sweep(
    pcm: Pcm,
    position: npt.NDArray[np.float32],
    *,
    high_pass: bool,
) -> Pcm:
    """Filter with a cutoff that moves through SWEEP_CUTOFFS_HZ over time.

    The filtered copies lie on an axis of len(SWEEP_CUTOFFS_HZ) + 1 points
    and `position` says, per sample, where on it the output is. For a
    low-pass the axis runs from the lowest cutoff to the unfiltered track,
    for a high-pass from the unfiltered track to the highest cutoff. The
    output blends neighbouring copies, which is far cheaper than a filter
    whose coefficients change from sample to sample.
    """
    spectrum, size = _spectrum(pcm)
    levels = len(SWEEP_CUTOFFS_HZ)
    result = np.zeros_like(pcm)
    for point in range(levels + 1):
        weight = np.clip(1 - np.abs(position - point), 0, 1)
        if not weight.any():
            continue
        if high_pass:
            copy = pcm
            if point:
                cutoff = SWEEP_CUTOFFS_HZ[point - 1]
                copy = pcm - _lowpass(spectrum, size, len(pcm), cutoff)
        elif point == levels:
            copy = pcm
        else:
            cutoff = SWEEP_CUTOFFS_HZ[point]
            copy = _lowpass(spectrum, size, len(pcm), cutoff)
        result += copy * weight[:, None]
    return result


def filter_sweep(outgoing: Pcm, incoming: Pcm) -> Pcm:
    """Close a low-pass on the outgoing track, open a high-pass on the next.

    The outgoing track loses its highs and then its body; the incoming one
    enters as a thin layer of highs and gains its bass last.
    """
    frames = len(outgoing)
    progress = np.linspace(0.0, 1.0, frames, dtype=np.float32)
    # Both sweeps finish a little before the end of the blend, so that the
    # incoming track is fully open when it takes over.
    swept = np.clip(progress / SWEEP_DONE_AT, 0, 1)
    position = len(SWEEP_CUTOFFS_HZ) * (1 - swept)
    closing = _sweep(outgoing, position, high_pass=False)
    opening = _sweep(incoming, position, high_pass=True)
    fade = progress * np.pi / 2
    blended = closing * np.cos(fade)[:, None] + opening * np.sin(fade)[:, None]
    return blended.astype(np.float32)


def riser(frames: int, rng: np.random.Generator) -> Pcm:
    """Make white noise that gets brighter and louder, peak 1."""
    noise = rng.normal(0, 1, (frames, CHANNELS)).astype(np.float32)
    top = len(SWEEP_CUTOFFS_HZ) - 1
    position = np.linspace(0.0, top, frames, dtype=np.float32)
    bright = _sweep(noise, position, high_pass=False)
    spectrum, size = _spectrum(bright)
    bright = bright - _lowpass(spectrum, size, frames, RISER_FLOOR_HZ)
    envelope = np.linspace(0.0, 1.0, frames, dtype=np.float32) ** 2.5
    shaped: Pcm = bright * envelope[:, None]
    # The riser is cut off on the drop; fade the last few ms to avoid a click.
    fade = min(frames, round(CUT_FADE_SECONDS * SAMPLE_RATE))
    shaped[frames - fade :] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)[
        :, None
    ]
    peak = float(np.abs(shaped).max())
    return (shaped / peak if peak else shaped).astype(np.float32)


def impact(frames: int, rng: np.random.Generator) -> Pcm:
    """Make a deep boom: a sine falling from ~110 to 40 Hz, plus a click."""
    count = min(frames, round(IMPACT_SECONDS * SAMPLE_RATE))
    t = np.arange(count) / SAMPLE_RATE
    frequency = 40 + 70 * np.exp(-t * 18)
    phase = 2 * np.pi * np.cumsum(frequency) / SAMPLE_RATE
    boom = np.sin(phase) * np.exp(-t * 4.5)
    click = rng.normal(0, 1, count) * np.exp(-t * 60) * 0.3
    mono = boom + click
    mono = mono / max(float(np.abs(mono).max()), 1e-9)
    return np.stack([mono, mono], axis=1).astype(np.float32)


def echo_out(pcm: Pcm, cut: int, level: float) -> Pcm:
    """Cut the track at `cut`, and let its last slice echo on, fading.

    Before the cut nothing changes. The last slice (one echo delay long)
    is then repeated, each time quieter and darker, as if the track was
    thrown into a delay unit at the moment its fader went down.
    """
    delay = round(ECHO_DELAY_SECONDS * SAMPLE_RATE)
    cut = max(cut, delay)
    frames = len(pcm)
    edge = min(delay // 2, round(CUT_FADE_SECONDS * SAMPLE_RATE))

    slice_ = pcm[cut - delay : cut].copy()
    slice_[:edge] *= np.linspace(0.0, 1.0, edge, dtype=np.float32)[:, None]
    slice_[-edge:] *= np.linspace(1.0, 0.0, edge, dtype=np.float32)[:, None]

    result = pcm.copy()
    result[cut:] = 0
    result[cut - edge : cut] *= np.linspace(1.0, 0.0, edge, dtype=np.float32)[
        :, None
    ]

    tail = np.zeros_like(pcm)
    for repeat in range(1, ECHO_REPEATS + 1):
        start = cut + (repeat - 1) * delay
        if start >= frames:
            break
        end = min(frames, start + delay)
        tail[start:end] = (
            slice_[: end - start] * (ECHO_FEEDBACK * level) ** repeat
        )
    spectrum, size = _spectrum(tail)
    result += _lowpass(spectrum, size, frames, ECHO_DARKEN_HZ)
    return result


def _rms(pcm: Pcm) -> float:
    return float(np.sqrt(np.mean(np.square(pcm, dtype=np.float64)) + 1e-12))


def add_drop(
    blended: Pcm,
    drop: int,
    beat_period: float,
    level: float,
    rng: np.random.Generator,
) -> Pcm:
    """Add a riser that builds up to the drop and a boom on it."""
    loudness = _rms(blended)
    length = min(drop, round(RISER_BEATS * beat_period))
    result = blended.copy()
    if length > MIN_FILTERED_BLEND_SECONDS * SAMPLE_RATE / 4:
        rise = riser(length, rng)
        result[drop - length : drop] += rise * (RISER_GAIN * level * loudness)
    if drop >= len(blended):
        return result
    boom = impact(len(blended) - drop, rng)
    gain = min(IMPACT_GAIN * level * loudness, IMPACT_PEAK)
    result[drop : drop + len(boom)] += boom * gain
    return result


def blend(
    kind: Transition,
    outgoing: Pcm,
    incoming: Pcm,
    beat_period: float = 0,
    *,
    effects: float = 0.0,
    rng: np.random.Generator | None = None,
) -> Pcm:
    """Blend two equally long pieces the given way.

    `effects` (0..1) dresses the transition up like in a club: with a
    shared beat grid a riser builds up to the drop and a boom lands on it;
    without one the outgoing track is echoed out instead, as the beats
    can't be lined up.
    """
    frames = len(outgoing)
    if frames < MIN_FILTERED_BLEND_SECONDS * SAMPLE_RATE:
        return crossfade(outgoing, incoming)
    rng = rng or np.random.default_rng()
    dressed = effects > 0

    if dressed and not beat_period:
        outgoing = echo_out(outgoing, round(frames * ECHO_CUT_AT), effects)

    if kind is Transition.BASS_SWAP:
        blended = bass_swap(outgoing, incoming, beat_period)
    elif kind is Transition.FILTER_SWEEP:
        blended = filter_sweep(outgoing, incoming)
    else:
        blended = crossfade(outgoing, incoming)

    if dressed and beat_period:
        if kind is Transition.BASS_SWAP:
            drop = round(swap_point(frames, beat_period)[0])
        else:
            # The last beat of the blend, so that the boom has room.
            drop = int((ceil(frames / beat_period) - 1) * beat_period)
        blended = add_drop(blended, drop, beat_period, effects, rng)
    return blended


@dataclass(frozen=True, slots=True)
class Entry:
    """A track waiting in the pool, as far as choosing it is concerned."""

    analysis: TrackAnalysis
    artists: frozenset[str] = frozenset()
    # The station mood the track was fetched with, "" when not known.
    mood: str = ""


def energy(analysis: TrackAnalysis) -> float:
    """Return how intense a track is, 0..1: faster and louder is more."""

    def scaled(value: float, bounds: tuple[float, float]) -> float:
        low, high = bounds
        return min(max((value - low) / (high - low), 0.0), 1.0)

    loudness = scaled(analysis.loudness_db, ENERGY_LOUDNESS_RANGE_DB)
    if not analysis.bpm:
        return loudness
    tempo = scaled(analysis.bpm, ENERGY_BPM_RANGE)
    return (tempo + loudness) / 2


def arc_target(position: int) -> float:
    """Return the energy wanted at the given place in the set.

    It starts calm, builds to a peak half way through the arc and winds
    down again, over and over.
    """
    return 0.5 - 0.35 * cos(2 * pi * position / ARC_TRACKS)


def mood_for(position: int) -> str:
    """Return the station mood that goes with the place in the set.

    Calm in the valleys of the arc; on the way up and down, fun first and
    then active (and active, then fun, on the way back); active on the peak.
    """
    target = arc_target(position)
    if target < MOOD_CALM_BELOW:
        return "calm"
    return "fun" if target < MOOD_FUN_BELOW else "active"


def nearest_mood(wanted: str, allowed: Sequence[str]) -> str:
    """Return the mood among the allowed ones that is closest to wanted.

    Everything is allowed when the list is empty.
    """
    if not allowed or wanted in allowed:
        return wanted
    if wanted not in ALL_MOODS:
        return allowed[0]
    place = ALL_MOODS.index(wanted)
    return min(allowed, key=lambda mood: abs(ALL_MOODS.index(mood) - place))


def mood_cost(wanted: str, got: str) -> float:
    """Return the cost of a track picked for another mood than wanted."""
    if got not in MOODS or wanted not in MOODS:
        return 0.0
    return MOOD_COST * abs(MOODS.index(wanted) - MOODS.index(got))


def transition_cost(
    current: TrackAnalysis, following: TrackAnalysis
) -> tuple[float, TrackAnalysis]:
    """Judge the transition the way the radio would actually make it.

    Returns the cost and the analysis of the following track as it would
    be played (sped up or slowed down to match the tempo).
    """
    speed = transition_speed(current.bpm, following)
    matched = bool(speed)
    playing = stretched(following, speed or 1.0)

    if matched:
        cost = MATCHED_TEMPO_COST * abs(log2(speed))
        kind = choose_transition(current, playing, beat_matched=True)
        if kind is Transition.FILTER_SWEEP:
            cost += SWEEP_COST
    elif current.bpm and following.bpm:
        gap = abs(log2(tempo_ratio(current.bpm, following.bpm)))
        cost = UNMATCHED_COST + UNMATCHED_TEMPO_COST * min(gap, 1.0)
    else:
        cost = UNMATCHED_COST + UNKNOWN_TEMPO_COST

    if section_length(current, playing) is None:
        cost += PLAIN_LENGTH_COST
    cost += LOUDNESS_COST * abs(current.loudness_db - following.loudness_db)
    cost += key_cost(current.key, following.key)
    return cost, playing


def step_cost(
    current: TrackAnalysis | None,
    entry: Entry,
    history: tuple[frozenset[str], ...],
    position: int,
) -> tuple[float, TrackAnalysis]:
    """Cost of playing the entry after current, and how it would play."""
    if current is None:
        cost, playing = 0.0, entry.analysis
    else:
        cost, playing = transition_cost(current, entry.analysis)
    if entry.artists & frozenset().union(*history[-REPEAT_WINDOW:]):
        cost += REPEAT_ARTIST_COST
    cost += ENERGY_ARC_COST * abs(energy(playing) - arc_target(position))
    cost += mood_cost(mood_for(position), entry.mood)
    return cost, playing


def _best_path(
    current: TrackAnalysis | None,
    pool: Sequence[Entry],
    history: tuple[frozenset[str], ...],
    position: int,
    depth: int,
) -> tuple[float, int]:
    """Return the cost of the best queue from here and its first pick."""
    steps = [
        (step_cost(current, entry, history, position), index)
        for index, entry in enumerate(pool)
    ]
    steps.sort(key=lambda step: step[0][0])

    best_cost, best_index = float("inf"), 0
    for (cost, playing), index in steps[:LOOKAHEAD_BEAM]:
        total = LOOKAHEAD_WEIGHTS[-depth] * cost
        if depth > 1 and len(pool) > 1:
            rest = [e for i, e in enumerate(pool) if i != index]
            total += _best_path(
                playing,
                rest,
                (*history, pool[index].artists),
                position + 1,
                depth - 1,
            )[0]
        if total < best_cost:
            best_cost, best_index = total, index
    return best_cost, best_index


def choose_next(
    current: TrackAnalysis | None,
    pool: Sequence[Entry],
    history: tuple[frozenset[str], ...] = (),
    position: int = 0,
) -> int:
    """Return the index in the pool of the track to play after current.

    Plans the next few tracks as a whole -- a track that blends perfectly
    but leaves nothing that fits after it is not a good pick -- and
    returns the first of the best queue.
    """
    depth = min(len(LOOKAHEAD_WEIGHTS), len(pool))
    return _best_path(current, pool, history, position, depth)[1]
