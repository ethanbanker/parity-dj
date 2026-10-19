import math
import struct
import tempfile
import unittest
import urllib.parse
import wave
from pathlib import Path

from parity import analysis, audio

SAMPLE_RATE = analysis.SAMPLE_RATE


def clicks(bpm: float, seconds: float = 20.0):
    """A synthetic pulse: a short noise burst exactly on every beat."""
    try:
        import numpy as np
    except Exception:  # pragma: no cover - numpy is a test dependency here
        raise unittest.SkipTest("numpy is not installed")
    samples = np.zeros(int(SAMPLE_RATE * seconds), dtype="float32")
    generator = np.random.default_rng(1)
    period = 60.0 / bpm
    time = 0.0
    while time < seconds - 0.02:
        start = int(time * SAMPLE_RATE)
        length = int(0.012 * SAMPLE_RATE)
        burst = generator.normal(0, 0.6, length).astype("float32")
        burst *= np.hanning(length).astype("float32")
        samples[start : start + length] += burst
        time += period
    return samples


def chord(root: float, seconds: float = 8.0):
    try:
        import numpy as np
    except Exception:  # pragma: no cover
        raise unittest.SkipTest("numpy is not installed")
    time = np.arange(int(SAMPLE_RATE * seconds)) / SAMPLE_RATE
    samples = np.zeros_like(time, dtype="float32")
    for multiplier, amplitude in ((1.0, 0.5), (5 / 4, 0.4), (3 / 2, 0.4), (2.0, 0.2)):
        samples += amplitude * np.sin(2 * math.pi * root * multiplier * time).astype("float32")
    return samples


def mixed(bpm: float = 128.0, seconds: float = 16.0):
    """A pulse with a C major chord over it -- one file, both questions."""
    try:
        import numpy as np
    except Exception:  # pragma: no cover
        raise unittest.SkipTest("numpy is not installed")
    samples = clicks(bpm, seconds).copy()
    time = np.arange(samples.size) / SAMPLE_RATE
    for multiplier, amplitude in ((1.0, 0.12), (5 / 4, 0.10), (3 / 2, 0.10)):
        samples += (amplitude * np.sin(2 * math.pi * 261.63 * multiplier * time)).astype("float32")
    return samples


@unittest.skipUnless(audio.status()["numpy_ok"], "numpy is not installed")
class TempoKeyTest(unittest.TestCase):
    def test_tempo_is_found_within_a_fraction_of_a_bpm(self):
        for bpm in (90, 120, 128, 140, 174):
            with self.subTest(bpm=bpm):
                result = analysis.analyse_samples(clicks(bpm))
                self.assertAlmostEqual(result.bpm, bpm, delta=0.2)
                self.assertGreater(result.bpm_confidence, 0.5)
                self.assertGreater(result.beats, 10)

    def test_the_grid_interval_matches_the_tempo(self):
        result = analysis.analyse_samples(clicks(128))
        self.assertAlmostEqual(result.interval_ms, 60000.0 / 128.0, delta=0.5)
        self.assertGreaterEqual(result.first_beat_ms, 0)
        self.assertLess(result.first_beat_ms, result.interval_ms + 1)

    def test_a_major_triad_is_named_and_mapped_to_camelot(self):
        for root, key, code in (
            (261.63, "C", "8B"),
            (220.00, "Am", "8A"),
            (174.61, "F", "7B"),
            (196.00, "G", "9B"),
        ):
            with self.subTest(root=root):
                result = analysis.analyse_samples(chord(root))
                self.assertEqual(result.key, key)
                self.assertEqual(result.camelot, code)

    def test_a_click_track_has_no_tonal_centre_and_says_so(self):
        result = analysis.analyse_samples(clicks(128))
        self.assertEqual(result.key, "")
        self.assertIn("tonal centre", result.note)

    def test_too_little_audio_is_refused(self):
        with self.assertRaises(analysis.AnalysisUnavailable):
            analysis.analyse_samples([0.0] * 100)


def wav(path: Path, samples) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(
            b"".join(struct.pack("<h", max(-32767, min(32767, int(sample * 32767)))) for sample in samples)
        )
    return path


def xml_for(path: Path, *, bpm: float = 0, tonality: str = "") -> str:
    location = "file://localhost" + urllib.parse.quote(str(path))
    return (
        '<DJ_PLAYLISTS Version="1.0.0"><PRODUCT Name="rekordbox" Version="7.0.0"/>'
        f'<COLLECTION Entries="1"><TRACK TrackID="1" Name="Track" Artist="Someone" '
        f'AverageBpm="{bpm}" Tonality="{tonality}" Location="{location}"/>'
        '</COLLECTION><PLAYLISTS><NODE Type="0" Name="ROOT" Count="1">'
        '<NODE Name="P" Type="1" KeyType="0" Entries="1"><TRACK Key="1"/></NODE>'
        '</NODE></PLAYLISTS></DJ_PLAYLISTS>'
    )


@unittest.skipUnless(audio.status()["available"], "ffmpeg and numpy are needed")
class AnalysisApiTest(unittest.TestCase):
    def setUp(self):
        from parity.server import Api
        from parity.store import Store

        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.file = wav(self.dir / "track.wav", mixed())
        self.store = Store(self.dir / "data")
        self.api = Api(self.store)
        self.api.import_xml(xml_for(self.file), "fixture")
        self.key = next(iter(self.store.tracks_by_key()))

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_measured_track_fills_the_pool_and_is_cached(self):
        report = self.api.analyse_track_audio({"keys": [self.key]})
        self.assertEqual(report["analysed"], 1)
        self.assertEqual(report["skipped"], [])
        row = report["results"][0]
        self.assertAlmostEqual(row["bpm"], 128.0, delta=0.3)
        self.assertEqual(row["camelot"], "8B")
        track = self.api.tracks(None)["tracks"][0]
        self.assertAlmostEqual(track["bpm"], 128.0, delta=0.3)
        self.assertEqual(track["camelot"], "8B")
        self.assertEqual(track["bpm_source"], "analysed")
        self.assertEqual(track["key_source"], "analysed")
        self.assertEqual(report["status"]["analysed"], 1)
        again = self.api.analyse_track_audio({"keys": [self.key]})
        self.assertEqual(again["analysed"], 0)
        self.assertEqual(again["cached_keys"], [self.key])

    def test_library_values_win_over_the_measurement(self):
        self.api.import_xml(xml_for(self.file, bpm=124, tonality="Am"), "fixture")
        key = next(iter(self.store.tracks_by_key()))
        self.api.analyse_track_audio({"keys": [key]})
        track = self.api.tracks(None)["tracks"][0]
        self.assertEqual(track["bpm"], 124.0)
        self.assertEqual(track["camelot"], "8A")
        self.assertEqual(track["bpm_source"], "library")
        # The measurement is still there, beside what the library says.
        self.assertAlmostEqual(track["analysed_bpm"], 128.0, delta=0.3)

    def test_the_measured_grid_reaches_the_export(self):
        self.api.analyse_track_audio({"keys": [self.key]})
        content, _name, _type = self.api.export(
            {"order": [self.key], "name": "Set", "format": "rekordbox", "cues": False}
        )
        import xml.etree.ElementTree as ET

        node = ET.fromstring(content).find("COLLECTION").find("TRACK").find("TEMPO")
        self.assertIsNotNone(node)
        self.assertAlmostEqual(float(node.attrib["Bpm"]), 128.0, delta=0.3)
        self.assertTrue(node.attrib["Inizio"])

    def test_a_dry_run_reports_without_storing(self):
        report = self.api.analyse_track_audio({"keys": [self.key], "dry_run": True})
        self.assertTrue(report["dry_run"])
        self.assertEqual(self.store.track_analysis(), {})
        self.assertEqual(self.api.tracks(None)["tracks"][0]["bpm_source"], "")

    def test_clear_forgets_the_measurement(self):
        self.api.analyse_track_audio({"keys": [self.key]})
        self.store.clear_track_analysis()
        self.api._analysis_version = -1
        self.assertEqual(self.api.tracks(None)["tracks"][0]["bpm_source"], "")
