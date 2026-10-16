"""Tests for :mod:`parity.waveform`.

The band split, the column reduction and the scaling are tested directly on
synthetic audio, so they run everywhere.  The read path needs ffmpeg, and the
librosa branch needs librosa, so those cases skip themselves on a machine
without either -- the same way the feature degrades rather than failing.
"""

import math
import struct
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from parity import audio, waveform

import numpy as np


def have_ffmpeg() -> bool:
    try:
        audio.ffmpeg_path()
        return True
    except audio.AudioUnavailable:
        return False


HAVE_FFMPEG = have_ffmpeg()
HAVE_LIBROSA = audio.librosa() is not None


def write_wav(path: Path, samples, rate: int = audio.SAMPLE_RATE) -> Path:
    """16-bit mono PCM, the one format every Python can produce itself."""
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"".join(
            struct.pack("<h", max(-32768, min(32767, int(value * 32767)))) for value in samples
        ))
    return path


def tone(seconds: float, frequency: float, amplitude: float = 0.6, rate: int = audio.SAMPLE_RATE):
    count = int(seconds * rate)
    return [amplitude * math.sin(2 * math.pi * frequency * index / rate) for index in range(count)]


def samples_for(signal) -> np.ndarray:
    return np.asarray(signal, dtype="float32")


class BandTest(unittest.TestCase):
    """The three bands a waveform is drawn from, with no files involved."""

    def test_a_bass_tone_lands_in_the_low_band(self):
        energies = waveform.band_frames(samples_for(tone(2.0, 100.0)))
        low, mid, high = (float(energies[index].mean()) for index in range(3))
        self.assertGreater(low, mid * 100)
        self.assertGreater(low, high * 100)

    def test_a_vocal_tone_lands_in_the_mid_band(self):
        energies = waveform.band_frames(samples_for(tone(2.0, 1000.0)))
        low, mid, high = (float(energies[index].mean()) for index in range(3))
        self.assertGreater(mid, low * 100)
        self.assertGreater(mid, high * 100)

    def test_a_hat_tone_lands_in_the_high_band(self):
        energies = waveform.band_frames(samples_for(tone(2.0, 8000.0)))
        low, mid, high = (float(energies[index].mean()) for index in range(3))
        self.assertGreater(high, low * 100)
        self.assertGreater(high, mid * 100)

    def test_the_edges_are_half_open(self):
        """A bin on an edge belongs to one band, not two: 250 Hz is mid, not low."""
        frequencies = np.array([0.0, 249.0, 250.0, 3999.0, 4000.0, 8000.0])
        low, mid, high = waveform.bin_masks(frequencies)
        self.assertEqual(list(low), [True, True, False, False, False, False])
        self.assertEqual(list(mid), [False, False, True, True, False, False])
        self.assertEqual(list(high), [False, False, False, False, True, True])

    def test_a_frame_is_one_hop_and_a_frame(self):
        samples = samples_for(tone(2.0, 100.0))
        energies = waveform.band_frames(samples)
        expected = 1 + (len(samples) - audio.FRAME) // audio.HOP
        self.assertEqual(energies.shape, (len(waveform.BANDS), expected))

    def test_a_file_too_short_to_frame_is_refused(self):
        with self.assertRaises(audio.AudioUnavailable):
            waveform.band_frames(np.zeros(64, dtype="float32"))


class EnvelopeTest(unittest.TestCase):
    """Columns, scaling and the numbers a canvas consumes."""

    def energies(self, values) -> np.ndarray:
        row = np.asarray(values, dtype="float32")
        return np.vstack([row, row * 0.5, row * 0.25])

    def test_a_long_track_is_reduced_to_the_column_count(self):
        peaks = waveform.peak_envelope(self.energies([1.0] * 5000), count=1000)
        self.assertEqual(len(peaks["low"]), 1000)

    def test_a_short_track_gets_one_column_per_frame(self):
        peaks = waveform.peak_envelope(self.energies([1.0] * 18), count=1000)
        self.assertEqual(len(peaks["low"]), 18)

    def test_columns_are_bytes(self):
        peaks = waveform.peak_envelope(self.energies([float(index % 7) for index in range(3000)]))
        for band in waveform.BANDS:
            self.assertTrue(all(0 <= value <= waveform.PEAK_MAX for value in peaks[band]))
            self.assertTrue(all(isinstance(value, int) for value in peaks[band]))

    def test_a_quiet_half_reads_quieter_than_a_loud_one(self):
        """The point of the peaks: an intro is drawn shorter than a drop."""
        values = [0.05] * 2000 + [1.0] * 2000
        peaks = waveform.peak_envelope(self.energies(values))
        first = sum(peaks["low"][:400]) / 400
        second = sum(peaks["low"][-400:]) / 400
        self.assertLess(first, second / 4)

    def test_one_spike_does_not_flatten_the_track(self):
        """Scaled against a high percentile, not the single loudest frame."""
        values = [1.0] * 1999 + [900.0]
        peaks = waveform.peak_envelope(self.energies(values), count=2000)
        loud = sum(1 for value in peaks["low"] if value > 200)
        self.assertGreater(loud, 1900)

    def test_silence_is_a_flat_line_not_a_division_by_zero(self):
        peaks = waveform.peak_envelope(self.energies([0.0] * 500), count=100)
        self.assertEqual(set(peaks["low"]), {0})
        self.assertEqual(set(peaks["mid"]), {0})

    def test_a_column_reads_its_loudest_frame_not_its_average(self):
        """Zooming out keeps the transient: one loud frame fills its column.

        Two columns of four frames each: the first holds a single 1.0 and the
        second holds four.  Taking the maximum, both columns are full height --
        which is the whole point, because an average would draw the transient
        at a quarter of the body it sits in and lose the kick.
        """
        frames = [1.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0]
        peaks = waveform.peak_envelope(self.energies(frames), count=2)
        self.assertEqual(peaks["low"], [waveform.PEAK_MAX, waveform.PEAK_MAX])


class ReadTest(unittest.TestCase):
    """The whole path: a real file, ffmpeg, and the peaks that come out."""

    def setUp(self):
        if not HAVE_FFMPEG:
            self.skipTest("ffmpeg is not installed")
        self.folder = TemporaryDirectory()
        self.path = Path(self.folder.name) / "track.wav"

    def tearDown(self):
        self.folder.cleanup()

    def test_a_quiet_intro_reads_shorter_than_a_loud_drop(self):
        # Long enough to be reduced to columns rather than one per frame: 26
        # seconds is 1111 frames against a 1000-column profile.
        write_wav(self.path, tone(13.0, 60.0, 0.05) + tone(13.0, 60.0, 0.95))
        result = waveform.analyse(self.path)
        low = result.peaks["low"]
        intro = sum(low[: len(low) // 4]) / max(1, len(low) // 4)
        drop = sum(low[-len(low) // 4:]) / max(1, len(low) // 4)
        self.assertLess(intro, drop / 4)
        self.assertEqual(result.peaks_per_band, waveform.PEAKS_PER_BAND)
        self.assertFalse(result.truncated)

    def test_the_length_reported_is_the_audio_that_was_read(self):
        write_wav(self.path, tone(3.0, 200.0))
        result = waveform.analyse(self.path)
        self.assertAlmostEqual(result.seconds, 3.0, places=1)
        self.assertAlmostEqual(result.duration_ms / 1000.0, 3.0, places=1)
        # Three seconds is fewer frames than columns, so the profile is one
        # column per frame rather than mostly zeros.
        self.assertEqual(result.peaks_per_band, len(result.peaks["low"]))
        self.assertLess(result.peaks_per_band, waveform.PEAKS_PER_BAND)

    def test_a_track_longer_than_the_cap_says_so(self):
        write_wav(self.path, tone(2.0, 200.0))
        result = waveform.analyse(self.path, seconds=waveform.MAX_SECONDS + 60.0)
        self.assertTrue(result.truncated)
        self.assertIn(result.engine, {"numpy", "librosa"})

    def test_the_payload_is_json_shaped(self):
        write_wav(self.path, tone(2.0, 400.0))
        payload = waveform.analyse(self.path).to_dict()
        self.assertEqual(sorted(payload["peaks"]), sorted(waveform.BANDS))
        self.assertEqual(payload["model"], waveform.MODEL_VERSION)
        self.assertIsInstance(payload["duration_ms"], int)
        self.assertLessEqual(max(payload["peaks"]["high"]), waveform.PEAK_MAX)

    def test_a_file_that_is_not_audio_is_reported_plainly(self):
        bogus = Path(self.folder.name) / "notes.txt"
        bogus.write_text("not audio", encoding="utf-8")
        with self.assertRaises(audio.AudioUnavailable):
            waveform.analyse(bogus)


class EngineTest(unittest.TestCase):
    """librosa and numpy have to draw the same track the same way."""

    def setUp(self):
        if not HAVE_FFMPEG:
            self.skipTest("ffmpeg is not installed")
        self.folder = TemporaryDirectory()
        self.path = Path(self.folder.name) / "both.wav"
        write_wav(self.path, tone(4.0, 80.0, 0.7) + tone(4.0, 3000.0, 0.3))

    def tearDown(self):
        self.folder.cleanup()

    def test_the_declared_engine_follows_librosa_being_importable(self):
        with mock.patch.object(audio, "librosa", lambda: None):
            result = waveform.analyse(self.path)
        self.assertEqual(result.engine, "numpy")

    @unittest.skipUnless(HAVE_LIBROSA, "librosa is not installed")
    def test_the_two_engines_draw_the_same_picture(self):
        import librosa as real_librosa

        with mock.patch.object(audio, "librosa", lambda: None):
            numpy_peaks = waveform.analyse(self.path).peaks
        with mock.patch.object(audio, "librosa", lambda: real_librosa):
            librosa_peaks = waveform.analyse(self.path).peaks
        for band in waveform.BANDS:
            self.assertEqual(len(numpy_peaks[band]), len(librosa_peaks[band]))
            drift = max(
                abs(left - right) for left, right in zip(numpy_peaks[band], librosa_peaks[band])
            )
            self.assertLessEqual(drift, 1, f"{band} band differs between engines")

    @unittest.skipUnless(HAVE_LIBROSA, "librosa is not installed")
    def test_the_librosa_engine_is_reported(self):
        import librosa as real_librosa

        with mock.patch.object(audio, "librosa", lambda: real_librosa):
            self.assertEqual(waveform.analyse(self.path).engine, "librosa")


if __name__ == "__main__":
    unittest.main()
