"""BPM, musical key and a beat grid, measured from the audio.

rekordbox usually has all three, but a library that has never been analysed --
a folder of downloads, a set from a friend, a track pulled from a service --
has none of them, and every decision the builder makes rests on them.  This is
the local answer: decode with ffmpeg, take a spectral flux and a chroma with
numpy, and estimate the tempo, the key and where the beats fall.

The methods are the small, explainable ones rather than a model:

* **Tempo** is the strongest period in the onset envelope's autocorrelation,
  refined by fitting a line through the onset times that land on the grid.  The
  autocorrelation finds the ballpark; the regression is what gets a 128 BPM
  track to 128.0 instead of 129.2, which is what a CDJ's sync will notice.
* **Key** is a chroma vector correlated against the Krumhansl-Schmuckler major
  and minor profiles in all twelve rotations.  It is the classic algorithm for
  this, and it is right far more often than it is wrong on dance music, which
  keeps one tonal centre for minutes at a time.
* **The beat grid** is the tempo's period plus the phase that puts the most
  onset energy on the grid.  It is one tempo for the whole track -- dynamic
  tempo is a different problem -- and it is marked as such in the result.

Nothing here writes to a library or a file.  The result is a measurement the
caller can store and show; what it is used for (the pool's BPM column, the cue
fallback, the exported ``<TEMPO>``) is decided elsewhere.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path

from . import audio, camelot

log = logging.getLogger("parity.analysis")

#: Bump when the estimate changes, so cached results are redone instead of
#: quietly disagreeing with today's algorithm.
MODEL_VERSION = 1

#: How much audio to read.  A tempo and a key are stable across a track; two
#: minutes bounds the decode and leaves a long DJ tool fast to analyse.
MAX_SECONDS = 120.0

SAMPLE_RATE = audio.SAMPLE_RATE
FRAME = audio.FRAME
HOP = audio.HOP
FRAME_RATE = SAMPLE_RATE / HOP  # onset frames per second (~43.07)

#: The tempo range a DJ library actually uses.  Anything below or above is
#: folded by an octave rather than reported as a number nobody can beatmatch.
MIN_BPM = 70.0
MAX_BPM = 190.0

#: Krumhansl-Schmuckler key profiles (the 1982 probe-tone ratings).
MAJOR_PROFILE = (6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88)
MINOR_PROFILE = (6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17)

#: Canonical spellings, chosen to match what :mod:`parity.camelot` parses.
KEY_NAMES = ("C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B")

#: Below this profile correlation there is no tonal centre to report.  A noise
#: burst correlates with a profile by chance; a real chord clears this easily.
MIN_KEY_CORRELATION = 0.55


class AnalysisUnavailable(RuntimeError):
    """The file cannot be decoded, or numpy/ffmpeg is missing."""


@dataclass
class Analysis:
    """What the audio said about tempo, key and the grid."""

    bpm: float = 0.0
    bpm_confidence: float = 0.0
    key: str = ""
    camelot: str = ""
    key_confidence: float = 0.0
    first_beat_ms: int = 0
    interval_ms: float = 0.0
    beats: int = 0
    seconds: float = 0.0
    engine: str = "numpy"
    model: int = MODEL_VERSION
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "bpm": round(self.bpm, 2),
            "bpm_confidence": round(self.bpm_confidence, 3),
            "key": self.key,
            "camelot": self.camelot,
            "key_confidence": round(self.key_confidence, 3),
            "first_beat_ms": int(self.first_beat_ms),
            "interval_ms": round(self.interval_ms, 3),
            "beats": int(self.beats),
            "seconds": round(self.seconds, 2),
            "engine": self.engine,
            "model": self.model,
            "note": self.note,
        }


# ------------------------------------------------------------------ availability


def status() -> dict:
    """What this feature can use here, in the shape the other statuses use."""
    base = audio.status()
    return {
        "available": bool(base.get("ffmpeg_ok") and base.get("numpy_ok")),
        "ffmpeg": base.get("ffmpeg"),
        "ffmpeg_ok": base.get("ffmpeg_ok"),
        "ffmpeg_error": base.get("ffmpeg_error"),
        "numpy": base.get("numpy"),
        "numpy_ok": base.get("numpy_ok"),
        "numpy_error": base.get("numpy_error"),
        "model": MODEL_VERSION,
        "error": base.get("error"),
        "install_hint": base.get("install_hint", ""),
        "searched": base.get("searched", []),
    }


# --------------------------------------------------------------------- analysis


def analyse(path: str | Path, seconds: float = MAX_SECONDS) -> Analysis:
    """Measure one file; raises :class:`AnalysisUnavailable` when it cannot."""
    try:
        samples = audio.decode(path, seconds)
    except audio.AudioUnavailable as exc:
        raise AnalysisUnavailable(str(exc)) from exc
    return analyse_samples(samples)


def analyse_samples(samples) -> Analysis:
    """The maths, separated from the decoder so it can be tested on a signal."""
    try:
        import numpy as np  # noqa: PLC0415 - optional dependency, imported late
    except Exception as exc:  # noqa: BLE001
        raise AnalysisUnavailable(
            "numpy is not installed, so the audio cannot be analysed"
        ) from exc

    if samples is None or len(samples) < FRAME * 4:
        raise AnalysisUnavailable("not enough audio to analyse")

    frames = np.lib.stride_tricks.sliding_window_view(samples, FRAME)[::HOP]
    if frames.shape[0] < 8:
        raise AnalysisUnavailable("not enough audio to analyse")
    window = np.hanning(FRAME).astype("float32")

    flux, chroma, bass_root, seconds = _features(frames, window, len(samples))
    bpm, bpm_confidence, onsets = _tempo(flux)
    key, camelot_code, key_confidence = _key(chroma, bass_root)
    first_beat, interval = _grid(flux, onsets, bpm)

    beats = int((seconds - first_beat) / interval) + 1 if interval > 0 else 0
    notes: list[str] = []
    if bpm_confidence < 0.3:
        notes.append("weak pulse: the tempo is a guess, check it against the track")
    if not key:
        notes.append("no clear tonal centre: the key is a guess")
    elif key_confidence < 0.05:
        notes.append("ambiguous key: check it in the booth")
    note = "; ".join(notes)
    return Analysis(
        bpm=bpm,
        bpm_confidence=bpm_confidence,
        key=key,
        camelot=camelot_code,
        key_confidence=key_confidence,
        first_beat_ms=int(round(max(0.0, first_beat) * 1000)),
        interval_ms=interval * 1000.0,
        beats=beats,
        seconds=seconds,
        note=note,
    )


def _features(frames, window, sample_count: int):
    """One pass for the spectral flux and the chroma.

    Reading the whole spectrogram at once is what makes this cheap: a two-minute
    track is about 10k frames, and the FFT is done in blocks so the intermediate
    array stays small.
    """
    import numpy as np  # noqa: PLC0415

    frequencies = np.fft.rfftfreq(FRAME, 1.0 / SAMPLE_RATE)
    # The pitch classes are only summed where a musical note can be: below 55 Hz
    # is rumble, above 5 kHz the harmonics stop naming the chord.
    pitched = (frequencies >= 55.0) & (frequencies <= 5000.0)
    midi = 69.0 + 12.0 * np.log2(np.maximum(frequencies[pitched], 1.0) / 440.0)
    classes = np.round(midi).astype(int) % 12
    # The bass root is the strongest key cue in dance music: the chord above it
    # can be ambiguous (F major and A minor share three notes) while the bass
    # almost always plays the tonic.
    bass_bins = pitched & (frequencies <= 250.0)
    bass_classes = classes[frequencies[pitched] <= 250.0]

    flux_parts: list = []
    chroma = np.zeros(12, dtype="float64")
    bass_spectrum = np.zeros(int(bass_bins.sum()), dtype="float64")
    previous = None
    for start in range(0, frames.shape[0], 512):
        block = frames[start : start + 512] * window
        spectrum = np.abs(np.fft.rfft(block, axis=1))
        # Onset strength: how much each bin grew since the previous frame.
        if previous is not None:
            flux_parts.append(
                np.asarray([np.maximum(spectrum[0] - previous, 0.0).sum()], dtype="float64")
            )
        if spectrum.shape[0] > 1:
            flux_parts.append(
                np.asarray(
                    np.maximum(spectrum[1:] - spectrum[:-1], 0.0).sum(axis=1),
                    dtype="float64",
                )
            )
        previous = spectrum[-1]
        # Chroma from the pitched bins; sqrt compresses the bass so one kick
        # does not outweigh the chord above it.
        pitched_magnitude = np.sqrt(spectrum[:, pitched])
        for index in range(12):
            chroma[index] += float(pitched_magnitude[:, classes == index].sum())
        if bass_bins.any():
            bass_spectrum += np.sqrt(spectrum[:, bass_bins]).sum(axis=0)

    flux = (
        np.concatenate(flux_parts)
        if flux_parts
        else np.zeros(1, dtype="float64")
    )
    total = float(chroma.sum())
    if total > 0:
        chroma = chroma / total

    # The bass root is the *lowest* strong bass note, not the loudest: a chord
    # third can be louder than the fundamental through a small speaker, while
    # the lowest note is almost always the tonic.
    bass_root = None
    if bass_spectrum.size:
        ceiling = float(bass_spectrum.max())
        if ceiling > 0:
            strong = bass_spectrum >= ceiling * 0.5
            first = int(np.argmax(strong)) if bool(strong.any()) else 0
            bass_root = int(bass_classes[first])
    return flux, chroma, bass_root, sample_count / SAMPLE_RATE


def _tempo(flux):
    """BPM, a confidence, and the onset times in seconds."""
    import numpy as np  # noqa: PLC0415

    env = np.asarray(flux, dtype="float64")
    if env.size < 16:
        return 0.0, 0.0, []
    # Remove the local mean (about a second) so a long loud section does not
    # dominate the autocorrelation, then keep only growth.
    width = max(3, int(round(FRAME_RATE)))
    kernel = np.ones(width, dtype="float64") / width
    env = env - np.convolve(env, kernel, mode="same")
    env = np.maximum(env, 0.0)
    spread = float(env.std())
    if spread <= 0:
        return 0.0, 0.0, []
    env = env / spread

    # Autocorrelation via FFT: the peak is the beat period.
    size = 1 << (2 * env.size - 1).bit_length()
    spectrum = np.fft.rfft(env, size)
    autocorr = np.fft.irfft(spectrum * np.conj(spectrum), size)[: env.size]
    autocorr = autocorr / np.maximum(np.arange(env.size, 0, -1), 1)

    low = max(1, int(math.floor(FRAME_RATE * 60.0 / MAX_BPM)))
    high = min(env.size - 1, int(math.ceil(FRAME_RATE * 60.0 / MIN_BPM)))
    if high <= low:
        return 0.0, 0.0, []
    lags = np.arange(low, high + 1)
    scores = autocorr[lags].astype("float64")
    # A period and its multiples are equally periodic; a small preference for
    # the shortest one keeps a 128 BPM track from being reported at 64.
    for index, lag in enumerate(lags):
        double = 2 * lag
        if double < autocorr.size:
            scores[index] += 0.35 * autocorr[double]
    peak = int(np.argmax(scores))
    if peak > 0 and peak < len(scores) - 1:
        # Parabolic interpolation around the peak: the beat rarely lands on an
        # exact frame, and the integer lag alone is off by up to one BPM.
        left, centre, right = scores[peak - 1], scores[peak], scores[peak + 1]
        denominator = left - 2 * centre + right
        offset = 0.5 * (left - right) / denominator if denominator else 0.0
    else:
        offset = 0.0
    period = float(lags[peak]) + float(offset)
    bpm = 60.0 * FRAME_RATE / period
    bpm = _fold_bpm(bpm)
    confidence = float(min(1.0, max(0.0, scores[peak] / (env.size * 0.12))))

    onsets = _onsets(env)
    if onsets:
        refined, inliers = _refine_tempo(onsets, bpm)
        if refined:
            bpm = _fold_bpm(refined)
            confidence = max(confidence, inliers)
    return round(bpm, 2), round(confidence, 3), onsets


def _fold_bpm(bpm: float) -> float:
    """Keep a tempo in the range a DJ would write on the record sleeve."""
    if bpm <= 0:
        return 0.0
    while bpm < MIN_BPM:
        bpm *= 2.0
    while bpm > MAX_BPM:
        bpm /= 2.0
    return bpm


def _onsets(env):
    """Onset times in seconds: local maxima above the envelope's own mean."""
    import numpy as np  # noqa: PLC0415

    if env.size < 3:
        return []
    threshold = float(env.mean() + env.std() * 0.6)
    peaks: list[float] = []
    last = -3
    for index in range(1, env.size - 1):
        if env[index] < threshold:
            continue
        if env[index] < env[index - 1] or env[index] < env[index + 1]:
            continue
        if index - last < 3:  # ~70 ms: one onset, not a flam
            continue
        peaks.append(index / FRAME_RATE)
        last = index
    return peaks


def _refine_tempo(onsets: list[float], bpm: float):
    """Fit a line through the onsets that land on the grid.

    ``bpm`` is the autocorrelation's ballpark.  The phase that puts the most
    onsets on the grid is found first, then a least-squares line through
    ``time = phase + beat * period`` gives a period precise enough that a
    10-minute track does not drift off its own grid.
    """
    import numpy as np  # noqa: PLC0415

    period = 60.0 / bpm
    times = np.asarray(onsets, dtype="float64")
    best_phase, best_score = 0.0, -1.0
    # 60 candidates over one beat is finer than the onset resolution and cheap.
    for step in range(60):
        phase = period * step / 60.0
        index = np.round((times - phase) / period)
        residual = np.abs(times - (phase + index * period))
        score = float(np.sum(np.exp(-((residual / (period * 0.08)) ** 2)))
                      ) if residual.size else 0.0
        if score > best_score:
            best_phase, best_score = phase, score
    index = np.round((times - best_phase) / period)
    residual = np.abs(times - (best_phase + index * period))
    inlier = residual < period * 0.15
    if int(inlier.sum()) < 4:
        return 0.0, 0.0
    x = index[inlier]
    y = times[inlier]
    if float(x.std()) < 1.0:
        return 0.0, 0.0
    slope, _intercept = np.polyfit(x, y, 1)
    if slope <= 0:
        return 0.0, 0.0
    return 60.0 / slope, float(inlier.mean())


def _grid(flux, onsets: list[float], bpm: float):
    """The phase that puts the most onset energy on the beat, in seconds."""
    import numpy as np  # noqa: PLC0415

    if bpm <= 0:
        return 0.0, 0.0
    period = 60.0 / bpm
    if not onsets:
        return 0.0, period
    times = np.asarray(onsets, dtype="float64")
    best_phase, best_score = 0.0, -1.0
    for step in range(80):
        phase = period * step / 80.0
        index = np.round((times - phase) / period)
        residual = np.abs(times - (phase + index * period))
        score = float(np.sum(np.exp(-((residual / (period * 0.08)) ** 2))))
        if score > best_score:
            best_phase, best_score = phase, score
    # A phase near the end of the bar is the same grid as a phase at its start;
    # report the earliest beat so "first beat" means what it says.
    phase = best_phase % period
    return phase, period


def _key(chroma, bass_root=None):
    """Krumhansl-Schmuckler key finding, returned as musical + Camelot codes."""
    import numpy as np  # noqa: PLC0415

    values = np.asarray(chroma, dtype="float64")
    if values.size != 12 or float(values.std()) <= 0:
        return "", "", 0.0
    values = (values - values.mean()) / values.std()

    best = (-2.0, -1.0, 0)
    second = -2.0
    for root in range(12):
        for mode, profile in ((1, MAJOR_PROFILE), (0, MINOR_PROFILE)):
            template = np.roll(np.asarray(profile, dtype="float64"), root)
            template = (template - template.mean()) / template.std()
            # A small bonus when the candidate's tonic is the lowest strong
            # bass note, which resolves the relative-major/minor ties a triad
            # leaves behind.
            correlation = float(np.mean(values * template))
            if bass_root is not None and root == int(bass_root):
                correlation += 0.25
            if correlation > best[0]:
                second = best[0]
                best = (correlation, mode, root)
            elif correlation > second:
                second = correlation
    correlation, mode, root = best
    if correlation < MIN_KEY_CORRELATION:
        # Broadband percussion correlates with *some* profile; below this the
        # answer is noise dressed as a key, and a blank is more honest.
        return "", "", round(max(0.0, correlation), 3)
    name = KEY_NAMES[root] + ("m" if not mode else "")
    code = camelot.parse_key(name)
    confidence = float(min(1.0, max(0.0, correlation - max(second, 0.0))))
    return name, code or "", round(confidence, 3)


def ffmpeg_missing() -> bool:
    return not status()["ffmpeg_ok"]
