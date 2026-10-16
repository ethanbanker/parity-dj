"""Waveform peaks for the cue editor.

A DJ waveform is not the audio, it is a few thousand numbers: loud enough to
show where the drop is, small enough to send in one request and keep on a laptop
for every track in the library.  The cue editor draws those numbers on a canvas;
this module is the part that reads the file and produces them.

Three bands, split the way rekordbox draws them, because that is what makes a
waveform readable at a glance -- a kick and a hi-hat look the same in one
envelope and nothing alike in three:

* **low** (below 250 Hz) -- the kick and the bassline, what the mirrored body of
  the waveform actually shows;
* **mid** (250 Hz - 4 kHz) -- vocals and synths, where the track's body is;
* **high** (4 kHz and up) -- hats, snares and the top of a distorted lead.

Decoding is :mod:`parity.audio`'s job, so the two features that read files
share one ffmpeg code path and one set of install instructions.  The spectrum is
taken with **librosa**'s ``stft`` when it is installed and with numpy's ``rfft``
otherwise -- the same Hann window at the same frame size and hop either way, so
the picture does not change with the packages a machine happens to have.  Both
walk the track in blocks, because a spectrogram of a six-minute track is a
hundred megabytes held at once and this needs one column per pixel, not one per
23 ms.

What comes back is 0-255 per band per column: the values a canvas wants.  Each
band is scaled against its own 99.5th percentile rather than its loudest single
frame, so one click does not flatten the rest of the track into a line, and the
same track always scales the same way because the number is a property of the
file rather than of the window it is drawn in.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from . import audio

log = logging.getLogger("parity.waveform")

#: One column per band.  A 1320-pixel window on a six-minute track is about
#: 270 ms per pixel, so 1000 columns over the whole track is roughly one per
#: pixel at full zoom-out and leaves nothing to invent when the view zooms in.
PEAKS_PER_BAND = 1000

#: Band names in drawing order, and the range each one covers in Hz.  A ``None``
#: top means "up to Nyquist", which is what a decode at
#: :data:`parity.audio.SAMPLE_RATE` actually contains.
BANDS = ("low", "mid", "high")
BAND_EDGES: tuple[tuple[float, float | None], ...] = (
    (0.0, 250.0),
    (250.0, 4000.0),
    (4000.0, None),
)

#: Frames of the spectrogram computed at a time (about 12 seconds of audio).
BLOCK_FRAMES = 512

#: Peaks are stored as a byte per column, which is all a display needs.
PEAK_MAX = 255

#: The band's own loud frames set the scale, not its single loudest one.
ROBUST_PERCENTILE = 99.5

#: How much of a file is turned into peaks.  Half an hour is longer than any
#: track a set is built from, and it bounds the decode and the cache entry.
MAX_SECONDS = 1800.0

#: Bumped when the peaks change shape, so a cached profile from an older version
#: is re-read instead of drawn beside cues placed against the old one.
MODEL_VERSION = 1


@dataclass
class Waveform:
    """The peaks for one track, and how they were made."""

    peaks: dict[str, list[int]] = field(default_factory=dict)
    seconds: float = 0.0  # audio covered by the peaks
    duration_ms: int = 0  # the same, in milliseconds, as the track is timed
    peaks_per_band: int = 0
    truncated: bool = False  # the file is longer than MAX_SECONDS
    engine: str = "numpy"
    model: int = MODEL_VERSION

    def to_dict(self) -> dict:
        return {
            "peaks": self.peaks,
            "seconds": round(self.seconds, 3),
            "duration_ms": int(self.duration_ms),
            "peaks_per_band": int(self.peaks_per_band),
            "truncated": bool(self.truncated),
            "engine": self.engine,
            "model": self.model,
        }


# ------------------------------------------------------------------ bands


def bin_masks(frequencies) -> list:
    """One boolean mask per band, over the bins of an ``rfft``/``stft`` row.

    ``frequencies`` is the numpy array of bin frequencies the caller already
    built (``rfftfreq`` or ``librosa.fft_frequencies``); the masks are made of
    the same comparisons, so nothing is imported here.  The edges are half-open
    -- ``[low, high)`` -- so a bin belongs to exactly one band and a 4 kHz hat
    cannot be double-counted into the mid band.
    """
    masks = []
    for low, high in BAND_EDGES:
        if high is None:
            masks.append(frequencies >= low)
        else:
            masks.append((frequencies >= low) & (frequencies < high))
    return masks


def band_frames(samples, librosa=None):
    """Per-frame energy in each band, shape ``(len(BANDS), frames)``.

    One number per band per frame, which is all the peaks need: keeping the
    spectrum itself would be a hundred megabytes for a six-minute track.

    The spectrum is a magnitude spectrogram in both paths -- ``librosa.stft``
    when librosa is importable, ``numpy.fft.rfft`` over a sliding window when it
    is not, both with a Hann window at :data:`parity.audio.FRAME` and
    :data:`parity.audio.HOP` and both unpadded (``center=False``), so a frame
    index means the same millisecond whichever ran.
    """
    import numpy as np  # noqa: PLC0415 - optional dependency, imported late

    if samples.size < audio.FRAME:
        raise audio.AudioUnavailable("the file is too short to draw")
    frames = 1 + (samples.size - audio.FRAME) // audio.HOP
    frequencies = (
        librosa.fft_frequencies(sr=audio.SAMPLE_RATE, n_fft=audio.FRAME)
        if librosa is not None
        else np.fft.rfftfreq(audio.FRAME, 1.0 / audio.SAMPLE_RATE)
    )
    masks = bin_masks(frequencies)
    window = np.hanning(audio.FRAME).astype("float32")

    energies = np.zeros((len(BANDS), frames), dtype="float32")
    for start in range(0, frames, BLOCK_FRAMES):
        count = min(BLOCK_FRAMES, frames - start)
        offset = start * audio.HOP
        block = samples[offset : offset + (count - 1) * audio.HOP + audio.FRAME]
        if librosa is not None:
            # librosa returns (bins, frames); the numpy branch below is
            # transposed to match, so the band sums see one shape.
            spectrum = np.abs(
                librosa.stft(
                    y=block,
                    n_fft=audio.FRAME,
                    hop_length=audio.HOP,
                    window="hann",
                    center=False,
                )
            )
        else:
            windowed = (
                np.lib.stride_tricks.sliding_window_view(block.astype("float32"), audio.FRAME)[
                    :: audio.HOP
                ]
                * window
            )
            spectrum = np.abs(np.fft.rfft(windowed, axis=1)).T
        for index, mask in enumerate(masks):
            # Summed, not averaged: a wide snare should light up the mid and high
            # bands together, and an average over 350 bins would bury it.
            energies[index, start : start + count] = spectrum[mask].sum(axis=0)
    return energies


def peak_envelope(energies, count: int = PEAKS_PER_BAND) -> dict[str, list[int]]:
    """Reduce per-frame band energy to ``count`` byte-sized columns per band.

    Each column is the loudest frame inside it, so zooming out keeps the
    transients instead of averaging them away -- the same thing the ear does
    with a compressed overview.  A track with fewer frames than columns gets one
    column per frame rather than a tail of zeros.
    """
    import numpy as np  # noqa: PLC0415 - optional dependency, imported late

    frames = int(energies.shape[1])
    if frames < 1:
        raise audio.AudioUnavailable("the file is too short to draw")
    count = max(1, min(int(count), frames))
    edges = np.linspace(0, frames, count + 1)
    starts = np.floor(edges[:-1]).astype(int)

    peaks: dict[str, list[int]] = {}
    for index, name in enumerate(BANDS):
        values = np.maximum.reduceat(energies[index].astype("float64"), starts)
        scale = float(np.percentile(values, ROBUST_PERCENTILE))
        if not scale > 0:
            scale = float(values.max()) or 1.0
        scaled = np.clip(values / scale, 0.0, 1.0)
        peaks[name] = [int(round(value * PEAK_MAX)) for value in scaled]
    return peaks


# ------------------------------------------------------------------ reading


def analyse(path, seconds: float | None = None, peaks: int = PEAKS_PER_BAND) -> Waveform:
    """Peaks for one file: decode with ffmpeg, split into bands, quantise.

    ``seconds`` is how much audio the caller believes there is -- the caller
    passes the track's own length, so a track longer than :data:`MAX_SECONDS`
    comes back marked ``truncated`` rather than silently drawn short.  Omit it
    and the whole cap is decoded.
    """
    requested = max(1.0, float(seconds)) if seconds else MAX_SECONDS
    samples = audio.decode(path, min(requested, MAX_SECONDS))
    librosa_module = audio.librosa()
    energies = band_frames(samples, librosa_module)
    envelope = peak_envelope(energies, peaks)
    covered = len(samples) / audio.SAMPLE_RATE
    return Waveform(
        peaks=envelope,
        seconds=covered,
        duration_ms=int(round(covered * 1000)),
        peaks_per_band=len(envelope.get(BANDS[0], [])),
        truncated=requested > MAX_SECONDS + 0.5,
        engine="librosa" if librosa_module is not None else "numpy",
    )
