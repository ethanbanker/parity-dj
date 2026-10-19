import math
import shutil
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from parity import audio, metadata


def wav(path: Path, seconds: float = 0.5) -> Path:
    rate = 44100
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(
            b"".join(
                struct.pack("<h", int(6000 * math.sin(2 * math.pi * 440 * i / rate)))
                for i in range(int(rate * seconds))
            )
        )
    return path


class NormaliseTest(unittest.TestCase):
    def test_aliases_are_the_stored_record_names(self):
        values = metadata.normalise({"comments": "nice", "tonality": "8A"})
        self.assertEqual(values, {"comments": "nice", "tonality": "Am"})

    def test_blank_clears_and_missing_leaves_alone(self):
        self.assertEqual(metadata.normalise({"genre": "  "}), {"genre": ""})

    def test_numbers_are_validated(self):
        values = metadata.normalise({"bpm": "127.5", "year": "2024", "track_number": "4"})
        self.assertEqual(values["bpm"], 127.5)
        self.assertEqual(values["year"], 2024)
        self.assertEqual(values["track_number"], 4)
        for bad in ({"year": "soon"}, {"bpm": "fast"}, {"bpm": 900}, {"track_number": -1}):
            with self.assertRaises(metadata.MetadataError):
                metadata.normalise(bad)

    def test_unknown_fields_are_refused(self):
        with self.assertRaises(metadata.MetadataError):
            metadata.normalise({"titel": "typo"})

    def test_empty_edit_is_refused(self):
        with self.assertRaises(metadata.MetadataError):
            metadata.normalise({})

    def test_a_camelot_key_is_stored_as_its_musical_name(self):
        self.assertEqual(metadata.normalise({"key": "8A"})["tonality"], "Am")
        # A key outside the wheel is still kept as the user typed it.
        self.assertEqual(metadata.normalise({"key": "H minor"})["tonality"], "H minor")


class TagCommandTest(unittest.TestCase):
    def test_the_audio_is_copied_not_re_encoded(self):
        args = metadata.command(Path("/music/a.mp3"), Path("/music/tmp.mp3"), {"title": "X"})
        self.assertIn("-c", args)
        self.assertEqual(args[args.index("-c") + 1], "copy")
        self.assertIn("title=X", args)

    def test_aiff_and_wav_are_asked_for_id3(self):
        args = metadata.command(Path("/music/a.wav"), Path("/music/tmp.wav"), {"artist": "A"})
        self.assertIn("-write_id3v2", args)
        self.assertIn("-id3v2_version", args)

    def test_a_camelot_key_becomes_a_musical_tag(self):
        values = metadata.tag_values(metadata.normalise({"key": "8A"}))
        self.assertEqual(values["key"], "Am")


@unittest.skipUnless(audio.status()["ffmpeg_ok"], "ffmpeg is not installed")
class WriteTagsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_mp3_tags_round_trip_and_the_audio_survives(self):
        source = self.dir / "track.mp3"
        audio.ffmpeg_path()
        import subprocess

        subprocess.run(
            [audio.ffmpeg_path(), "-v", "error", "-y", "-f", "lavfi",
             "-i", "sine=frequency=440:duration=0.5", "-c:a", "libmp3lame", str(source)],
            check=True,
        )
        before = source.stat().st_size
        fields = metadata.normalise(
            {"title": "Night Ferry", "artist": "Ada Kaleh", "genre": "House",
             "comment": "banger", "year": "2024", "bpm": "122", "key": "8A"}
        )
        result = metadata.write_tags(source, fields)
        self.assertEqual(result["missing"], [])
        from parity.ingest import probe

        tags = probe(source)["tags"]
        self.assertEqual(tags["title"], "Night Ferry")
        self.assertEqual(tags["artist"], "Ada Kaleh")
        self.assertEqual(tags["genre"], "House")
        self.assertEqual(tags["bpm"], "122")
        self.assertEqual(tags["key"], "Am")
        # A re-mux, not a re-encode: the file keeps roughly its size and the
        # original samples (checked by ffprobe reading it back at all).
        self.assertGreater(source.stat().st_size, before // 2)

    def test_an_aiff_carries_the_tags_too(self):
        source = self.dir / "track.aiff"
        import subprocess

        subprocess.run(
            [audio.ffmpeg_path(), "-v", "error", "-y", "-f", "lavfi",
             "-i", "sine=frequency=440:duration=0.5", "-c:a", "pcm_s16be", str(source)],
            check=True,
        )
        metadata.write_tags(source, metadata.normalise({"title": "Ferry", "artist": "Ada"}))
        from parity.ingest import probe

        tags = probe(source)["tags"]
        self.assertEqual(tags["title"], "Ferry")
        self.assertEqual(tags["artist"], "Ada")

    def test_a_wav_reports_what_its_container_cannot_hold(self):
        source = wav(self.dir / "track.wav")
        result = metadata.write_tags(
            source, metadata.normalise({"title": "Ferry", "bpm": "128", "key": "8A"})
        )
        from parity.ingest import probe

        self.assertEqual(probe(source)["tags"]["title"], "Ferry")
        # WAV's INFO chunk has no BPM or key slot; reporting it is the honest
        # answer rather than claiming a write that never landed.
        self.assertIn("bpm", result["missing"])
        self.assertIn("key", result["missing"])

    def test_a_missing_file_is_a_message(self):
        with self.assertRaises(metadata.MetadataError):
            metadata.write_tags(self.dir / "nope.mp3", metadata.normalise({"title": "X"}))

    def test_an_unreadable_file_leaves_the_original_alone(self):
        source = self.dir / "broken.mp3"
        source.write_bytes(b"not really an mp3")
        with self.assertRaises(metadata.MetadataError):
            metadata.write_tags(source, metadata.normalise({"title": "X"}))
        self.assertEqual(source.read_bytes(), b"not really an mp3")
        self.assertEqual(list(self.dir.glob(".parity-tags-*")), [])
