"""Tests for :mod:`parity.audio`.

The scoring and the feature maths are tested directly; the decode path is tested
against real WAV files written by the standard library, which needs ffmpeg, so
those cases skip themselves on a machine without it -- exactly as the feature
degrades rather than failing.
"""

import math
import os
import struct
import sys
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from parity import audio

def have_ffmpeg() -> bool:
    """The same search the feature does, not just $PATH.

    The feature now finds ffmpeg in the standard install locations as well, so a
    gate that only asked ``shutil.which`` would skip the decode cases on a
    machine that can decode perfectly well -- a skip that looks like a pass.
    """
    try:
        audio.ffmpeg_path()
        return True
    except audio.AudioUnavailable:
        return False


HAVE_FFMPEG = have_ffmpeg()


def write_wav(path: Path, samples: list[float], rate: int = audio.SAMPLE_RATE) -> Path:
    """Write 16-bit mono PCM: the one format every Python can produce itself."""
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"".join(
            struct.pack("<h", max(-32768, min(32767, int(value * 32767)))) for value in samples
        ))
    return path


def sine(seconds: float, frequency: float, amplitude: float, rate: int = audio.SAMPLE_RATE):
    count = int(seconds * rate)
    return [
        amplitude * math.sin(2 * math.pi * frequency * index / rate) for index in range(count)
    ]


def noise_bursts(seconds: float, amplitude: float, rate: int = audio.SAMPLE_RATE):
    """A bright, percussive signal: alternating full and near-silent frames."""
    import random

    rng = random.Random(7)
    samples = []
    burst = int(0.05 * rate)
    count = int(seconds * rate)
    for index in range(count):
        samples.append(rng.uniform(-amplitude, amplitude) if (index // burst) % 2 == 0 else 0.0)
    return samples


class ScoreTest(unittest.TestCase):
    """The mapping from features to 1-10, with no files involved."""

    def test_louder_is_higher(self):
        quiet = audio.score(-16.0, 2500.0, 12.0, 3.0)
        loud = audio.score(-4.0, 2500.0, 12.0, 3.0)
        self.assertLess(quiet, loud)

    def test_brighter_is_higher(self):
        dark = audio.score(-6.0, 1500.0, 12.0, 3.0)
        bright = audio.score(-6.0, 4000.0, 12.0, 3.0)
        self.assertLess(dark, bright)

    def test_wide_dynamics_read_lower(self):
        squashed = audio.score(-6.0, 2500.0, 6.0, 3.0)
        breathing = audio.score(-6.0, 2500.0, 24.0, 3.0)
        self.assertLess(breathing, squashed)

    def test_busier_is_higher(self):
        sparse = audio.score(-6.0, 2500.0, 12.0, 1.5)
        busy = audio.score(-6.0, 2500.0, 12.0, 4.5)
        self.assertLess(sparse, busy)

    def test_drive_is_capped(self):
        self.assertEqual(
            audio.score(-6.0, 2500.0, 12.0, 9.0),
            audio.score(-6.0, 2500.0, 12.0, 40.0),
        )

    def test_the_scale_is_clamped(self):
        self.assertEqual(audio.score(3.0, 9000.0, 1.0, 12.0), 10.0)
        self.assertEqual(audio.score(-60.0, 100.0, 60.0, 0.0), 1.0)

    def test_an_ordinary_track_lands_mid_scale(self):
        self.assertAlmostEqual(audio.score(-12.9, 2000.0, 13.0, 3.0), 4.4, places=1)

    def test_a_mastered_club_track_reads_high_and_a_loop_reads_low(self):
        # Both rows are measured files from a real library: a mastered edit and a
        # bare production loop.
        club = audio.score(-5.13, 2867.0, 13.43, 3.33)
        loop = audio.score(-18.30, 4497.0, 21.66, 3.39)
        self.assertGreaterEqual(club, 7.0)
        self.assertLessEqual(loop, 4.0)


class FeatureMathTest(unittest.TestCase):
    def test_decibels_are_floored(self):
        self.assertEqual(audio._db(0.0), -120.0)
        self.assertAlmostEqual(audio._db(1.0), 0.0, places=6)
        self.assertAlmostEqual(audio._db(0.5), -6.02, places=2)

    def test_onset_count_finds_separated_peaks(self):
        import numpy as np

        flux = np.zeros(60, dtype="float32")
        for index in (10, 20, 30, 40):
            flux[index] = 10.0
        self.assertEqual(audio._onset_count(flux), 4)

    def test_onset_count_merges_peak_pairs(self):
        import numpy as np

        flux = np.zeros(60, dtype="float32")
        for index in (10, 11, 12, 30):
            flux[index] = 10.0
        # 10 and 11 are one drum hit, not two.
        self.assertEqual(audio._onset_count(flux), 2)

    def test_onset_count_on_a_flat_spectrum(self):
        import numpy as np

        self.assertEqual(audio._onset_count(np.zeros(20, dtype="float32")), 0)


class FindToolTest(unittest.TestCase):
    """Finding ffmpeg without a usable $PATH -- the Mac app's normal case.

    A Finder-launched app gets launchd's minimal PATH, so ``shutil.which`` finds
    nothing even on a machine with Homebrew's ffmpeg installed.  These cases pin
    the search that makes that work, and the message when it truly is absent.
    """

    def setUp(self):
        self.dir = TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.bindir = Path(self.dir.name) / "bin"
        self.bindir.mkdir()

    def fake(self, name: str, executable: bool = True) -> Path:
        path = self.bindir / name
        path.write_bytes(b"#!/bin/sh\nexit 0\n")
        path.chmod(0o755 if executable else 0o644)
        return path

    def test_a_tool_off_path_is_found_by_its_standard_location(self):
        path = self.fake("ffmpeg")
        with mock.patch("shutil.which", lambda name: None), \
                mock.patch.object(audio, "CANDIDATE_DIRS", (str(self.bindir),)):
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(audio.ffmpeg_path(), str(path))

    def test_a_file_that_is_not_executable_is_not_a_tool(self):
        self.fake("ffmpeg", executable=False)
        with mock.patch("shutil.which", lambda name: None), \
                mock.patch.object(audio, "CANDIDATE_DIRS", (str(self.bindir),)):
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(audio.AudioUnavailable):
                    audio.ffmpeg_path()

    def test_the_variable_wins_when_it_points_at_a_real_binary(self):
        path = self.fake("ffmpeg")
        with mock.patch.dict(os.environ, {audio.ENV_FFMPEG: str(path)}):
            self.assertEqual(audio.ffmpeg_path(), str(path))

    def test_a_variable_pointing_nowhere_says_so_instead_of_falling_back(self):
        with mock.patch.dict(os.environ, {audio.ENV_FFMPEG: "/no/such/ffmpeg"}):
            with self.assertRaises(audio.AudioUnavailable) as caught:
                audio.ffmpeg_path()
        self.assertIn("launchctl setenv", str(caught.exception))

    def test_nothing_found_names_where_it_looked_and_both_commands(self):
        with mock.patch("shutil.which", lambda name: None), \
                mock.patch.object(audio, "CANDIDATE_DIRS", (str(self.bindir),)), \
                mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(audio.AudioUnavailable) as caught:
                audio.ffmpeg_path()
        message = str(caught.exception)
        self.assertIn(str(self.bindir), message)
        # The app needs launchctl; a terminal run needs export.  Both are given.
        self.assertIn(f"launchctl setenv {audio.ENV_FFMPEG}", message)
        self.assertIn(f"export {audio.ENV_FFMPEG}=", message)

    def test_status_reports_where_it_looked(self):
        with mock.patch.object(audio, "CANDIDATE_DIRS", (str(self.bindir),)):
            status = audio.status()
        self.assertEqual(status["searched"], [str(self.bindir)])


class StatusTest(unittest.TestCase):
    def test_status_reports_what_is_installed(self):
        status = audio.status()
        self.assertIn("available", status)
        self.assertIn("engine", status)
        self.assertEqual(status["model"], audio.MODEL_VERSION)
        if status["available"]:
            self.assertEqual(status["engine"], "librosa" if status["librosa"] else "numpy")

    def test_a_present_ffmpeg_without_numpy_is_not_reported_as_missing_ffmpeg(self):
        """The two requirements fail differently, so they are named separately.

        One merged flag told someone who *had* ffmpeg to brew install ffmpeg and
        said nothing about numpy, which is the half that was actually missing and
        was never declared anywhere you could install it from.
        """
        with mock.patch.object(audio, "ffmpeg_path", lambda: "/usr/local/bin/ffmpeg"):
            with mock.patch.dict(sys.modules, {"numpy": None}):
                status = audio.status()
        self.assertTrue(status["ffmpeg_ok"])
        self.assertFalse(status["numpy_ok"])
        self.assertFalse(status["available"])
        self.assertIn("numpy", status["error"])
        self.assertIn("install_extras", status["install_hint"])

    def test_status_names_ffmpeg_when_ffmpeg_is_the_one_missing(self):
        with mock.patch.object(audio, "ffmpeg_path", side_effect=audio.AudioUnavailable("no ffmpeg")):
            status = audio.status()
        self.assertFalse(status["ffmpeg_ok"])
        self.assertFalse(status["available"])
        self.assertIn("ffmpeg", status["error"])
        # The hint is about ffmpeg and nothing else: the install line, where it
        # looked, and both set-commands (the app needs launchctl, a terminal run
        # needs export).  It must not send someone with working ffmpeg to the
        # numpy extras instead.
        hint = status["install_hint"]
        self.assertIn("brew install ffmpeg", hint)
        self.assertIn("launchctl setenv " + audio.ENV_FFMPEG, hint)
        self.assertIn("export " + audio.ENV_FFMPEG + "=", hint)
        self.assertNotIn("install_extras", hint)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is not installed")
class DecodeTest(unittest.TestCase):
    """End to end on generated files: decode, measure, score."""

    def setUp(self):
        self.dir = TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def path(self, name: str) -> Path:
        return Path(self.dir.name) / name

    def test_a_sine_is_measured_as_a_sine(self):
        path = write_wav(self.path("a440.wav"), sine(2.0, 440.0, 0.6))
        features = audio.analyse(path)
        self.assertEqual(features.engine in {"numpy", "librosa"}, True)
        self.assertAlmostEqual(features.centroid_hz, 440.0, delta=120.0)
        # A 0.6 peak sine has an RMS of 0.6/sqrt(2), which is -7.4 dBFS -- the
        # loudness feature is an RMS, so it sits 3 dB under the peak level.
        self.assertAlmostEqual(features.loudness_dbfs, -7.44, delta=0.5)
        self.assertLess(features.dynamic_range_db, 1.0)
        self.assertAlmostEqual(features.seconds, 2.0, delta=0.05)

    def test_a_quiet_track_scores_below_a_loud_one(self):
        loud = write_wav(self.path("loud.wav"), sine(2.0, 800.0, 0.7))
        quiet = write_wav(self.path("quiet.wav"), sine(2.0, 800.0, 0.05))
        self.assertGreater(audio.analyse(loud).energy, audio.analyse(quiet).energy)

    def test_a_bright_dense_signal_scores_above_a_dark_quiet_one(self):
        dark = write_wav(self.path("dark.wav"), sine(2.0, 200.0, 0.05))
        dense = write_wav(self.path("dense.wav"), noise_bursts(2.0, 0.8))
        self.assertGreater(audio.analyse(dense).energy, audio.analyse(dark).energy)

    def test_a_file_that_is_not_audio_is_refused_by_name(self):
        path = self.path("notes.txt")
        path.write_text("this is not a track")
        with self.assertRaises(audio.AudioUnavailable) as caught:
            audio.analyse(path)
        self.assertIn("notes.txt", str(caught.exception))

    def test_a_missing_file_is_refused(self):
        with self.assertRaises(audio.AudioUnavailable):
            audio.analyse(self.path("nothing-here.wav"))

    def test_silence_does_not_divide_by_zero(self):
        path = write_wav(self.path("silence.wav"), [0.0] * (audio.SAMPLE_RATE * 2))
        features = audio.analyse(path)
        self.assertEqual(features.energy, 1.0)
        self.assertLess(features.loudness_dbfs, -60.0)


if __name__ == "__main__":
    unittest.main()
