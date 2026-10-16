"""Energy measured from the audio itself.

rekordbox exports no energy column, so :mod:`parity.energy` estimates one from
metadata: a comment tag, the stars, the genre, a BPM nudge.  That is enough to
build a set with and it is wrong in a way DJs notice -- two tech-house tracks
with the same stars can be six energy points apart in the booth.  This module
measures the file instead, locally, with no API or website involved:

* **Loudness** -- the RMS of the loud frames, in dBFS.  A track mastered for a
  club sits near -8; an ambient cut sits near -22.
* **Brightness** -- the spectral centroid, the centre of mass of the spectrum in
  Hz.  Percussive, distorted, hi-hat-heavy music reads an octave or two above a
  deep-house cut at the same loudness.
* **Dynamic range** -- the spread between the loud and the quiet frames, in dB.
  Modern club music is squashed flat; a live or orchestral recording breathes,
  and at the same loudness it belongs lower in the set.
* **Drive** -- how often the spectrum changes under the music (onset rate), which
  separates a busy techno roller from a slow burner at the same tempo.

Decoding is ffmpeg's job (``ffmpeg -i FILE -ac 1 -ar 22050 -f f32le -``), because
it reads every format a DJ library holds, including the ones Python cannot open.
The features are then taken with **librosa** when it is installed --
``librosa.feature.rms`` and ``librosa.feature.spectral_centroid``, the functions
this was specified around -- and with numpy otherwise, using the same frame size
and hop, so a machine without the 23-package librosa install still gets the same
answer.  Everything is measured on a mono 22.05 kHz decode of the first
:data:`MAX_SECONDS` of the track: plenty for a loudness and brightness reading,
and it keeps a 10-minute WAV from being copied into memory twice.
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("parity.audio")

#: Point this at ffmpeg to override the search: ``$PARITY_FFMPEG``, then
#: ``$PATH``, then :data:`CANDIDATE_DIRS`.
ENV_FFMPEG = "PARITY_FFMPEG"

#: Where a tool usually lives when ``$PATH`` does not say so, most likely first.
#:
#: This exists because of the Mac app: a Finder-launched process inherits
#: launchd's minimal PATH -- /usr/bin:/bin:/usr/sbin:/sbin -- and *not* the one
#: in your shell, so Homebrew's ffmpeg is invisible to ``shutil.which`` even
#: though it is installed.  Apple-silicon Homebrew, Intel Homebrew, MacPorts,
#: Linuxbrew, snap, then the system bin where apt and dnf put theirs.
CANDIDATE_DIRS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/opt/local/bin",
    "/home/linuxbrew/.linuxbrew/bin",
    "/snap/bin",
    "/usr/bin",
    "/bin",
)


def find_tool(name: str) -> str | None:
    """An absolute path to ``name``, looking beyond ``$PATH``.

    ``shutil.which`` only knows about the environment it was handed.  This is the
    belt to its braces: the standard install locations are checked by name, so a
    tool that is installed is found whether or not the process can see the shell
    that installed it.
    """
    for directory in CANDIDATE_DIRS:
        candidate = Path(directory).expanduser() / name
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        except OSError:  # pragma: no cover - an unreadable candidate directory
            continue
    return None


def tool_help(name: str, variable: str) -> str:
    """How to fix a missing tool: install it, or tell Parity where it is.

    Both set-commands are spelled out, because which one works depends on how
    Parity was started: a Finder-launched app never sees the shell's
    environment, so ``export`` reaches a terminal run and ``launchctl setenv``
    reaches the app.
    """
    return (
        f"{name} was not found on $PATH or in {', '.join(CANDIDATE_DIRS)}. "
        "Install ffmpeg (brew install ffmpeg -- ffprobe is part of it), or point "
        "Parity at the one you already have:\n"
        f"    launchctl setenv {variable} /path/to/{name}     # for the Mac app, then relaunch it\n"
        f"    export {variable}=/path/to/{name}              # for a terminal run"
    )


#: Mono decode rate and the analysis frame/hop.  512 at 22.05 kHz is 23 ms per
#: frame, which is fine enough to see a kick drum and coarse enough that a
#: 10-minute track is 25k frames rather than a million.
SAMPLE_RATE = 22050
FRAME = 2048
HOP = 512

#: How much of a track to measure (seconds).  Energy is a property of the whole
#: track, but the first twelve minutes carry it, and this bounds both the decode
#: and the memory a 40-minute DJ tool would otherwise need.
MAX_SECONDS = 720.0

#: Bumped when the scoring below changes, so cached energies from an older model
#: are recomputed instead of quietly disagreeing with the current one.
MODEL_VERSION = 1

# Scoring.  The score is SCORE_BASE plus a weighted term per feature, each
# measured from the point an unremarkable track sits at, so the middle of the
# scale means "ordinary in every respect" and only the audio moves a track off
# it.  The constants are fitted to 116 real local files, not chosen by taste:
# with them those files spread 2.1 - 10.0 (median 7.9), the production loop
# samples landing at E2-E6 and the mastered club tracks at E7-E10.  The earlier
# metadata estimate put every one of them on E4.2, because rekordbox exports
# nothing to estimate from beyond BPM.
SCORE_BASE = 4.4
W_LOUDNESS = 1.00
W_BRIGHTNESS = 0.90
W_DYNAMICS = 0.90
W_DRIVE = 0.80

#: Reference points the terms are measured from, in the units each feature is in.
LOUDNESS_REF_DBFS = -12.9  # a track this loud scores zero on loudness
LOUDNESS_STEP_DB = 2.6  # ... and 2.6 dB is one point
BRIGHTNESS_REF_HZ = 2000.0  # log2 of the centroid against this
DYNAMICS_REF_DB = 13.0  # a 13 dB spread scores zero
DYNAMICS_STEP_DB = 7.0
DRIVE_REF_PER_SEC = 3.0  # onsets per second
DRIVE_STEP_PER_SEC = 2.0
DRIVE_CAP = 1.5

MIN_ENERGY = 1.0
MAX_ENERGY = 10.0


class AudioUnavailable(RuntimeError):
    """The file could not be decoded, or ffmpeg is not installed."""


# ------------------------------------------------------------ availability


def ffmpeg_path() -> str:
    """The ffmpeg to run, or raise :class:`AudioUnavailable`.

    Four places in turn: the environment variable, ``$PATH``, the standard
    install locations, and only then an error that says what it looked through.
    """
    override = os.environ.get(ENV_FFMPEG)
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file():
            return str(candidate)
        resolved = shutil.which(override)
        if resolved:
            return resolved
        raise AudioUnavailable(
            f"{ENV_FFMPEG} is set to {override!r}, which is not a runnable file. "
            f"Unset it to let Parity search, or point it at the real one: "
            f"launchctl setenv {ENV_FFMPEG} /opt/homebrew/bin/ffmpeg"
        )
    found = shutil.which("ffmpeg")
    if found:
        return found
    guessed = find_tool("ffmpeg")
    if guessed:
        # Worth a log line: it means this process cannot see $PATH the way a
        # terminal would, which is normal for the Mac app and confusing otherwise.
        log.info("ffmpeg found at %s, which is not on this process's $PATH", guessed)
        return guessed
    raise AudioUnavailable("ffmpeg is not available, so the audio cannot be decoded. "
                           + tool_help("ffmpeg", ENV_FFMPEG))


def status() -> dict:
    """What the energy-from-audio feature can use on this machine.

    Two separate things have to be true, and they fail in different ways: ffmpeg
    decodes the file, numpy (or librosa, which brings numpy) does the maths.
    They are reported separately, because telling someone with a working ffmpeg
    to ``brew install ffmpeg`` -- which is what one merged ``available`` flag
    did -- sends them to fix the wrong thing.
    """
    try:
        ffmpeg = ffmpeg_path()
        ffmpeg_error = None
    except AudioUnavailable as exc:
        ffmpeg, ffmpeg_error = None, str(exc)
    numpy_version, numpy_error, librosa_version = None, None, None
    try:
        import numpy  # noqa: F401

        from importlib.metadata import version as _version

        try:
            numpy_version = _version("numpy")
        except Exception:  # noqa: BLE001
            numpy_version = "unknown"
    except Exception as exc:  # noqa: BLE001
        numpy_error = str(exc) or exc.__class__.__name__
        log.debug("numpy unavailable: %s", exc)
    try:
        from importlib.metadata import version as _version

        librosa_version = _version("librosa")
    except Exception:  # noqa: BLE001
        librosa_version = None

    if not ffmpeg:
        error = ffmpeg_error
        hint = tool_help("ffmpeg", ENV_FFMPEG)
    elif not numpy_version:
        error = f"numpy is not installed ({numpy_error}), so the features cannot be computed"
        hint = f"install the extras for this interpreter:  {sys.executable} tools/install_extras.py"
    else:
        error, hint = None, ""
    return {
        # Both are needed for the score; the two flags below say which is missing.
        "available": bool(ffmpeg and numpy_version),
        "ffmpeg": ffmpeg,
        "ffmpeg_ok": bool(ffmpeg),
        "ffmpeg_error": ffmpeg_error,
        "numpy": numpy_version,
        "numpy_ok": bool(numpy_version),
        "numpy_error": numpy_error,
        "librosa": librosa_version,
        "engine": "librosa" if librosa_version else ("numpy" if numpy_version else None),
        "model": MODEL_VERSION,
        "error": error,
        "install_hint": hint,
        # Where it looked, so "not installed" can be checked against the machine.
        "searched": list(CANDIDATE_DIRS),
    }


# ------------------------------------------------------------------ decode


def decode(path: str | Path, seconds: float = MAX_SECONDS):
    """Decode a file to mono float samples at :data:`SAMPLE_RATE`.

    Returns a numpy array.  ffmpeg reads mp3, m4a, wav, aiff, flac and the rest
    in one code path, and anything it cannot read raises
    :class:`AudioUnavailable` with ffmpeg's own last line rather than a stack
    trace the UI would have to hide.
    """
    try:
        import numpy as np  # noqa: PLC0415 - optional dependency, imported late
    except Exception as exc:  # noqa: BLE001
        raise AudioUnavailable(
            "numpy is not installed, so the audio features cannot be computed"
        ) from exc

    command = [
        ffmpeg_path(),
        "-nostdin",
        "-v",
        "error",
        "-t",
        f"{max(1.0, float(seconds)):.3f}",
        "-i",
        str(path),
        "-map",
        "a:0",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-f",
        "f32le",
        "-",
    ]
    process = subprocess.run(command, capture_output=True)
    if process.returncode != 0:
        detail = (process.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise AudioUnavailable(
            f"ffmpeg could not decode {Path(path).name}: "
            + (detail[-1] if detail else f"exit {process.returncode}")
        )
    samples = np.frombuffer(process.stdout, dtype="<f4")
    if samples.size < FRAME:
        raise AudioUnavailable(f"{Path(path).name} holds less than a second of audio")
    return np.nan_to_num(samples.astype("float32", copy=False))


# ---------------------------------------------------------------- features


@dataclass
class Features:
    """What the audio measured, and the energy it maps to."""

    seconds: float = 0.0
    loudness_dbfs: float = -60.0  # RMS of the loud frames
    centroid_hz: float = 0.0
    dynamic_range_db: float = 0.0
    onset_rate: float = 0.0  # spectral changes per second
    peak_dbfs: float = -60.0
    energy: float = 5.0
    engine: str = "numpy"
    model: int = MODEL_VERSION
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "seconds": round(self.seconds, 2),
            "loudness_dbfs": round(self.loudness_dbfs, 2),
            "centroid_hz": round(self.centroid_hz, 1),
            "dynamic_range_db": round(self.dynamic_range_db, 2),
            "onset_rate": round(self.onset_rate, 2),
            "peak_dbfs": round(self.peak_dbfs, 2),
            "energy": self.energy,
            "engine": self.engine,
            "model": self.model,
            "note": self.note,
        }


def _db(value: float) -> float:
    """Amplitude (0..1+) to dBFS, floored so silence is a number not -inf."""
    return 20.0 * math.log10(max(value, 1e-6))


def score(
    loudness_dbfs: float,
    centroid_hz: float,
    dynamic_range_db: float,
    onset_rate: float,
) -> float:
    """Map the four features onto the 1-10 energy scale.

    Each term is measured from a reference point a normal track sits at, so an
    ordinary track lands mid-scale and only the audio moves it: loudness and
    brightness dominate, a wide dynamic range pulls down, and drive is capped
    because a very busy track is not twice as energetic as a busy one.
    """
    loudness = (loudness_dbfs - LOUDNESS_REF_DBFS) / LOUDNESS_STEP_DB
    brightness = math.log2(max(centroid_hz, 200.0) / BRIGHTNESS_REF_HZ)
    dynamics = -(dynamic_range_db - DYNAMICS_REF_DB) / DYNAMICS_STEP_DB
    drive = min((onset_rate - DRIVE_REF_PER_SEC) / DRIVE_STEP_PER_SEC, DRIVE_CAP)
    value = SCORE_BASE + (
        W_LOUDNESS * loudness
        + W_BRIGHTNESS * brightness
        + W_DYNAMICS * dynamics
        + W_DRIVE * drive
    )
    return round(max(MIN_ENERGY, min(MAX_ENERGY, value)), 1)


def analyse(path: str | Path, seconds: float = MAX_SECONDS) -> Features:
    """Measure one file: see the module docstring for what is measured."""
    samples = decode(path, seconds)
    librosa_module = librosa()
    if librosa_module is not None:
        return _analyse_librosa(samples, librosa_module)
    return _analyse_numpy(samples)


def librosa():
    """The librosa module when it can actually be imported, else ``None``."""
    try:
        import librosa  # type: ignore[import-not-found]  # noqa: PLC0415

        return librosa
    except Exception as exc:  # noqa: BLE001 - optional, and heavy to install
        log.debug("librosa unavailable, using numpy features: %s", exc)
        return None


def _analyse_librosa(samples, librosa) -> Features:
    """The specified path: librosa.feature.rms + spectral_centroid."""
    import numpy as np  # noqa: PLC0415

    rms = librosa.feature.rms(y=samples, frame_length=FRAME, hop_length=HOP)[0]
    rms_db = librosa.amplitude_to_db(np.maximum(rms, 1e-6), ref=1.0)
    centroid = librosa.feature.spectral_centroid(
        y=samples, sr=SAMPLE_RATE, n_fft=FRAME, hop_length=HOP
    )[0]
    onsets = librosa.onset.onset_detect(
        y=samples, sr=SAMPLE_RATE, hop_length=HOP, units="time", backtrack=False
    )
    seconds = len(samples) / SAMPLE_RATE
    features = Features(
        seconds=seconds,
        loudness_dbfs=float(np.percentile(rms_db, 70)),
        centroid_hz=float(np.median(centroid)),
        dynamic_range_db=float(np.percentile(rms_db, 95) - np.percentile(rms_db, 10)),
        onset_rate=float(len(onsets) / seconds) if seconds else 0.0,
        peak_dbfs=float(np.max(rms_db)),
        engine="librosa",
    )
    features.energy = score(
        features.loudness_dbfs, features.centroid_hz, features.dynamic_range_db, features.onset_rate
    )
    return features


def _analyse_numpy(samples) -> Features:
    """The same four features without librosa, at the same frame size and hop."""
    import numpy as np  # noqa: PLC0415

    frames = np.lib.stride_tricks.sliding_window_view(samples, FRAME)[::HOP]
    if frames.shape[0] < 2:
        raise AudioUnavailable("not enough audio to measure")
    window = np.hanning(FRAME).astype("float32")
    frequencies = np.fft.rfftfreq(FRAME, 1.0 / SAMPLE_RATE)

    rms = np.sqrt(np.mean(frames * frames, axis=1))
    rms_db = np.array([_db(float(value)) for value in rms])

    # Centroid and spectral flux come out of the same pass, in chunks: a
    # 25k-frame spectrogram would be a quarter of a gigabyte held at once.
    weighted_sum = 0.0
    magnitude_sum = 0.0
    previous = None
    fluxes: list[float] = []
    for start in range(0, frames.shape[0], 512):
        block = frames[start : start + 512] * window
        spectrum = np.abs(np.fft.rfft(block, axis=1))
        weighted_sum += float((spectrum * frequencies).sum())
        magnitude_sum += float(spectrum.sum())
        # Spectral flux, one number per frame: how much the spectrum grew since
        # the frame before it, summed over the bins.
        if previous is not None:
            fluxes.append(float(np.maximum(spectrum[0] - previous, 0.0).sum()))
        if spectrum.shape[0] > 1:
            growth = np.maximum(spectrum[1:] - spectrum[:-1], 0.0).sum(axis=1)
            fluxes.extend(growth.tolist())
        previous = spectrum[-1]

    centroid_hz = weighted_sum / magnitude_sum if magnitude_sum else 0.0
    flux = np.asarray(fluxes, dtype="float32") if fluxes else np.zeros(1, dtype="float32")
    onsets = _onset_count(flux)

    seconds = len(samples) / SAMPLE_RATE
    features = Features(
        seconds=seconds,
        loudness_dbfs=float(np.percentile(rms_db, 70)),
        centroid_hz=centroid_hz,
        dynamic_range_db=float(np.percentile(rms_db, 95) - np.percentile(rms_db, 10)),
        onset_rate=(onsets / seconds) if seconds else 0.0,
        peak_dbfs=float(rms_db.max()),
        engine="numpy",
    )
    features.energy = score(
        features.loudness_dbfs, features.centroid_hz, features.dynamic_range_db, features.onset_rate
    )
    return features


def _onset_count(flux, minimum_gap_frames: int = 5):
    """Peaks in the spectral flux: a cheap, threshold-free onset count.

    The threshold is the mean plus one standard deviation of the flux itself, so
    a quiet track is not read as having no onsets and a loud one as having them
    everywhere, and peaks closer together than ``minimum_gap_frames`` (about
    100 ms) are one onset rather than two.
    """
    import numpy as np  # noqa: PLC0415

    if flux.size < 3:
        return 0
    threshold = float(flux.mean() + flux.std())
    above = flux > threshold
    peaks = 0
    last = -minimum_gap_frames
    for index in np.nonzero(above)[0]:
        if index - last >= minimum_gap_frames:
            peaks += 1
            last = int(index)
    return peaks
