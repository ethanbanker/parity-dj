"""Tests for :mod:`parity.ingest`.

The name tidying and the planning are pure standard library, so those run
everywhere.  The conversion, the tag check and the silence measurement need
ffmpeg, so those cases skip themselves on a machine without it -- which is the
same way the feature itself degrades: names still get fixed, ffmpeg still needs
installing.
"""

import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from parity import ingest

def have_ffmpeg() -> bool:
    """The same search the feature does, not just $PATH.

    The feature looks in the standard install locations as well (see
    ``parity.audio``), so a gate that only asked ``shutil.which`` would skip
    the conversion cases on a machine where the conversion would have worked.
    """
    try:
        ingest.ffmpeg_path()
        return True
    except ingest.IngestUnavailable:
        return False


HAVE_FFMPEG = have_ffmpeg()

SILENCE_LOG = """
  Duration: 00:03:12.40, start: 0.02, bitrate: 192 kb/s
[Parsed_silencedetect_0 @ 0x1] silence_start: 0
[Parsed_silencedetect_0 @ 0x1] silence_end: 0.250021 | silence_duration: 0.250021
[Parsed_silencedetect_0 @ 0x1] silence_start: 191.90
"""


class SanitizeTest(unittest.TestCase):
    """The filename rules, applied directly -- no files, no ffmpeg."""

    def tidy(self, name: str, extension: str = ".mp3"):
        return ingest.sanitize(name, extension)

    def test_a_download_name_becomes_a_name_a_cdj_can_show(self):
        raw = "01_Ada_Kaleh_-_Night_Ferry_🎵_[Official_Video]_[FREE_DL]_320kbps"
        new, changes = self.tidy(raw)
        self.assertEqual(new, "01 Ada Kaleh - Night Ferry")
        self.assertTrue(changes)

    def test_illegal_characters_become_dashes_or_go(self):
        new, changes = self.tidy("AC/DC: Back in Black?")
        self.assertEqual(new, "AC-DC - Back in Black")
        self.assertIn("illegal", [change.kind for change in changes])

    def test_emoji_and_invisible_characters_go(self):
        new, changes = self.tidy("Night Ferry \U0001f3b5\u200b\ufe0f")
        self.assertEqual(new, "Night Ferry")
        self.assertEqual([change.kind for change in changes], ["emoji"])

    def test_letters_from_any_alphabet_survive(self):
        for name in ("Café Été - Nuit", "東京ドリーム", "Луна под утро"):
            with self.subTest(name=name):
                self.assertEqual(self.tidy(name)[0], name)

    def test_junk_brackets_go_but_a_genre_tag_stays(self):
        new, _ = self.tidy("Night Ferry (Official Audio) [Hardgroove] [HD]")
        self.assertEqual(new, "Night Ferry [Hardgroove]")

    def test_a_link_and_a_bitrate_go(self):
        new, changes = self.tidy("www.example.com Night Ferry 128kbps")
        self.assertEqual(new, "Night Ferry")
        self.assertEqual({change.kind for change in changes}, {"format"})

    def test_a_clean_name_is_left_alone(self):
        new, changes = self.tidy("Ada Kaleh - Night Ferry")
        self.assertEqual(new, "Ada Kaleh - Night Ferry")
        self.assertEqual(changes, [])

    def test_a_hyphen_inside_a_word_is_not_a_separator(self):
        self.assertEqual(self.tidy("lo-fi beats")[0], "lo-fi beats")
        self.assertEqual(self.tidy("A  -  B")[0], "A - B")

    def test_reserved_device_names_are_moved_out_of_the_way(self):
        new, changes = self.tidy("CON")
        self.assertEqual(new, "CON_")
        self.assertEqual(changes[0].kind, "reserved")

    def test_a_long_name_is_shortened_and_the_extension_still_fits(self):
        new, changes = self.tidy("x" * 400, ".aiff")
        self.assertLessEqual(len((new + ".aiff").encode("utf-8")), ingest.MAX_NAME_BYTES)
        self.assertEqual(changes[-1].kind, "length")

    def test_a_name_with_nothing_readable_left_is_still_a_name(self):
        new, changes = self.tidy("\U0001f3b5\U0001f525")
        self.assertEqual(new, "untitled")
        self.assertEqual({change.kind for change in changes}, {"emoji", "format"})

    def test_web_escapes_are_decoded(self):
        self.assertEqual(self.tidy("Ada%20Kaleh%20-%20Night%20Ferry")[0], "Ada Kaleh - Night Ferry")

    def test_every_reason_is_one_the_report_can_show(self):
        known = {"illegal", "emoji", "format", "reserved", "length", "collision"}
        raw = "01_Ada_-_🎵_[Free Download]_www.x.com_320kbps.mp3"
        for change in self.tidy(raw)[1]:
            self.assertIn(change.kind, known)
            self.assertTrue(change.detail)


class SilenceTest(unittest.TestCase):
    """The arithmetic behind the reported offset, with no decoder involved."""

    def test_leading_silence_is_the_offset_the_music_starts_at(self):
        padding = ingest.parse_silence(SILENCE_LOG, 192.4)
        self.assertAlmostEqual(padding["leading_ms"], 250.0, delta=1.0)

    def test_an_unclosed_silence_at_the_end_is_trailing_padding(self):
        padding = ingest.parse_silence(SILENCE_LOG, 192.4)
        self.assertAlmostEqual(padding["trailing_ms"], 500.0, delta=1.0)

    def test_no_silence_at_all_reads_as_zero_not_missing(self):
        padding = ingest.parse_silence("  Duration: 00:03:00.00, start: 0.0", 180.0)
        self.assertEqual(padding["leading_ms"], 0.0)
        self.assertEqual(padding["trailing_ms"], 0.0)

    def test_a_wholly_silent_file_is_leading_silence_only(self):
        log = "  Duration: 00:00:05.00, start: 0.0\n" \
              "[Parsed_silencedetect_0 @ 0x1] silence_start: 0\n" \
              "[Parsed_silencedetect_0 @ 0x1] silence_end: 5.0 | silence_duration: 5.0\n"
        padding = ingest.parse_silence(log, 5.0)
        self.assertAlmostEqual(padding["leading_ms"], 5000.0, delta=1.0)
        self.assertEqual(padding["trailing_ms"], 0.0)


class PlanTest(unittest.TestCase):
    """Scanning, planning and collisions.  No decoder: ``measure=False``."""

    def setUp(self):
        self.dir = TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name) / "staging"
        self.root.mkdir()

    def touch(self, name: str, content: bytes = b"audio") -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_it_finds_audio_and_ignores_the_rest(self):
        self.touch("Ada Kaleh - Night Ferry.mp3")
        self.touch("sub/Deep House.wav")
        self.touch("cover.jpg")
        self.touch("notes.txt")
        plan = ingest.scan(self.root, None, "aiff", measure=False)
        self.assertEqual([item.relative.name for item in plan.items],
                         ["Ada Kaleh - Night Ferry.mp3", "Deep House.wav"])
        self.assertTrue(any("not audio" in note for note in plan.notes))

    def test_converted_files_take_the_target_extension(self):
        self.touch("Ada Kaleh - Night Ferry.mp3")
        plan = ingest.scan(self.root, None, "aiff", measure=False)
        self.assertEqual(plan.items[0].name, "Ada Kaleh - Night Ferry.aiff")
        self.assertTrue(plan.items[0].converts)

    def test_a_file_already_in_the_format_is_copied_not_converted(self):
        self.touch("Ada Kaleh - Night Ferry.wav")
        plan = ingest.scan(self.root, None, "wav", measure=False)
        self.assertFalse(plan.items[0].converts)
        self.assertEqual(plan.items[0].action, "copy")

    def test_two_files_that_tidy_to_the_same_name_get_a_number(self):
        self.touch("artist - title.mp3")
        self.touch("artist - title.m4a")
        plan = ingest.scan(self.root, None, "aiff", measure=False)
        names = sorted(item.name for item in plan.items)
        self.assertEqual(names, ["artist - title (2).aiff", "artist - title.aiff"])
        second = [item for item in plan.items if "(2)" in item.name][0]
        self.assertTrue(any(change.kind == "collision" for change in second.changes))

    def test_subfolders_are_kept_and_can_be_turned_off(self):
        self.touch("sub/deep/late night.wav")
        deep = ingest.scan(self.root, None, "wav", measure=False, recursive=True)
        self.assertEqual(deep.items[0].target,
                         deep.destination / "sub" / "deep" / "late night.wav")
        shallow = ingest.scan(self.root, None, "wav", measure=False, recursive=False)
        self.assertEqual(shallow.items, [])

    def test_it_never_scans_its_own_output(self):
        self.touch("late night.wav")
        self.touch("converted/late night.wav")
        plan = ingest.scan(self.root, None, "wav", measure=False)
        self.assertEqual([item.relative.name for item in plan.items], ["late night.wav"])

    def test_a_file_that_is_already_in_the_destination_is_flagged(self):
        self.touch("late night.mp3")
        self.touch("converted/late night.aiff")
        plan = ingest.scan(self.root, None, "aiff", measure=False)
        self.assertEqual(plan.items[0].status, "exists")
        self.assertIn("already", plan.items[0].reason)
        with_overwrite = ingest.scan(self.root, None, "aiff", measure=False, overwrite=True)
        self.assertEqual(with_overwrite.items[0].status, "ready")

    def test_a_named_destination_is_used(self):
        self.touch("late night.wav")
        out = Path(self.dir.name) / "ready for rekordbox"
        plan = ingest.scan(self.root, out, "wav", measure=False)
        self.assertEqual(plan.destination, out.resolve())
        self.assertEqual(plan.items[0].target.parent, out.resolve())

    def test_a_missing_folder_is_refused(self):
        with self.assertRaises(ingest.IngestError):
            ingest.scan(Path(self.dir.name) / "nope")

    def test_no_folder_at_all_is_refused(self):
        with self.assertRaises(ingest.IngestError):
            ingest.scan("")

    def test_an_unknown_format_is_refused(self):
        with self.assertRaises(ingest.IngestError):
            ingest.scan(self.root, None, "ogg")

    def test_the_plan_serialises_and_counts_itself(self):
        self.touch("01_Ada_Kaleh_-_Night_Ferry_🎵_[Official_Video]_320kbps.mp3")
        self.touch("late night.wav")
        payload = ingest.scan(self.root, None, "aiff", measure=False).to_dict()
        json.dumps(payload)  # the API sends exactly this
        self.assertEqual(payload["summary"]["found"], 2)
        # One name moved.  The .wav only changed extension on the way to AIFF,
        # which the rename count deliberately does not call a rename.
        self.assertEqual(payload["summary"]["renames"], 1)
        self.assertEqual(payload["summary"]["converts"], 2)
        self.assertEqual(payload["summary"]["by_source_format"], {"mp3": 1, "wav": 1})

    def test_a_dry_run_writes_nothing(self):
        self.touch("late night.wav")
        plan = ingest.scan(self.root, None, "aiff", measure=False)
        report = ingest.run(plan, dry_run=True)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["summary"]["planned"], 1)
        self.assertFalse(plan.destination.exists())

    def test_status_reports_the_formats(self):
        status = ingest.status()
        self.assertIn("available", status)
        self.assertEqual([entry["key"] for entry in status["formats"]], ["aiff", "wav"])
        self.assertEqual(status["default_format"], "aiff")


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is not installed")
class ConvertTest(unittest.TestCase):
    """End to end through ffmpeg on files built here, so the numbers are known."""

    LEAD_MS = 250

    def setUp(self):
        self.dir = TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name) / "staging"
        self.root.mkdir()
        self.source = self.root / "01_Ada_Kaleh_-_Night_Ferry_🎵_[Official_Video]_320kbps.mp3"
        self.ffmpeg(
            "-f", "lavfi", "-i", "anoisesrc=d=2:c=pink:a=0.2",
            "-af", f"adelay={self.LEAD_MS}|{self.LEAD_MS}",
            "-c:a", "libmp3lame", "-b:a", "192k",
            "-metadata", "artist=Ada Kaleh",
            "-metadata", "title=Night Ferry",
            "-metadata", "album=Salt",
            "-metadata", "genre=Techno",
            str(self.source),
        )

    def ffmpeg(self, *args) -> None:
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", *args],
            check=True, capture_output=True,
        )

    def convert_one(self, **kwargs):
        plan = ingest.scan(self.root, None, "aiff")
        report = ingest.run(plan, dry_run=False, **kwargs)
        self.assertEqual(report["failed"], [])
        return plan, report, report["written"][0]

    def test_the_scan_reports_the_leading_silence_before_anything_is_written(self):
        plan = ingest.scan(self.root, None, "aiff")
        padding = plan.items[0].padding
        self.assertAlmostEqual(padding["leading_ms"], self.LEAD_MS, delta=25)
        # A file shorter than the scan window was still read end to end.
        self.assertTrue(padding["complete"])

    def test_the_conversion_keeps_the_tags_and_reports_the_offset(self):
        _, _, row = self.convert_one()
        self.assertEqual(row["name"], "01 Ada Kaleh - Night Ferry.aiff")
        self.assertAlmostEqual(row["padding"]["leading_ms"], self.LEAD_MS, delta=25)
        self.assertTrue(row["padding"]["complete"])
        self.assertEqual(row["tags"]["lost"], [])
        self.assertEqual(
            sorted(row["tags"]["after"]), ["album", "artist", "genre", "title"]
        )

    def test_the_written_file_is_real_aiff_audio(self):
        _, report, row = self.convert_one()
        self.assertEqual(report["summary"]["converted"], 1)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,sample_fmt",
             "-of", "json", row["target"]],
            capture_output=True, text=True, check=True,
        )
        stream = json.loads(probe.stdout)["streams"][0]
        self.assertEqual(stream["codec_name"], "pcm_s16be")
        self.assertEqual(stream["sample_fmt"], "s16")

    def test_the_original_is_left_exactly_as_it_was(self):
        before = self.source.read_bytes()
        self.convert_one()
        self.assertEqual(self.source.read_bytes(), before)

    def test_running_again_leaves_the_written_file_alone(self):
        _, _, first = self.convert_one()
        written = Path(first["target"])
        stamp = written.stat().st_mtime
        plan = ingest.scan(self.root, None, "aiff")
        second = ingest.run(plan, dry_run=False)
        self.assertEqual(second["summary"]["converted"], 0)
        self.assertEqual(len(second["skipped"]), 1)
        self.assertEqual(written.stat().st_mtime, stamp)

    def test_trimming_takes_the_leading_silence_out(self):
        _, _, row = self.convert_one(trim_silence=True)
        self.assertAlmostEqual(row["trimmed_ms"], self.LEAD_MS, delta=25)
        self.assertLess(row["padding"]["leading_ms"], 30.0)

    def test_a_file_already_in_the_target_format_is_copied_untouched(self):
        wav = self.root / "Late Night.wav"
        self.ffmpeg("-f", "lavfi", "-i", "anoisesrc=d=1:c=pink:a=0.2", "-c:a", "pcm_s16le", str(wav))
        plan = ingest.scan(self.root, None, "wav")
        report = ingest.run(plan, dry_run=False)
        copied = [row for row in report["copied"] if row["name"] == "Late Night.wav"]
        self.assertEqual(len(copied), 1)
        self.assertEqual(Path(copied[0]["target"]).read_bytes(), wav.read_bytes())

    def test_container_bookkeeping_does_not_follow_an_m4a_into_the_new_file(self):
        m4a = self.root / "Ada Kaleh - Night Ferry.m4a"
        self.ffmpeg(
            "-f", "lavfi", "-i", "anoisesrc=d=1:c=pink:a=0.2", "-c:a", "aac",
            "-metadata", "artist=Ada Kaleh", "-metadata", "title=Night Ferry",
            str(m4a),
        )
        plan = ingest.scan(self.root, None, "aiff")
        report = ingest.run(plan, dry_run=False)
        rows = [row for row in report["written"] if row["name"] == "Ada Kaleh - Night Ferry.aiff"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        # artist and title travel; the MP4 brand fields do not.
        self.assertIn("artist", row["tags"]["after"])
        for noise in ingest.CONTAINER_TAGS:
            self.assertNotIn(noise, row["tags"]["after"])

    def cover_source(self, name: str = "Ada Kaleh - Covered.mp3") -> Path:
        """An MP3 carrying an attached picture, built here so the art is known."""
        picture = self.root / "artwork.png"
        self.ffmpeg("-f", "lavfi", "-i", "color=c=red:s=64x64", "-frames:v", "1", str(picture))
        source = self.root / name
        self.ffmpeg(
            "-i", str(self.source), "-i", str(picture),
            "-map", "0:a", "-map", "1:v", "-c:a", "copy", "-c:v", "copy",
            "-disposition:v", "attached_pic",
            "-metadata", "artist=Ada Kaleh",
            str(source),
        )
        return source

    def attached_pictures(self, path) -> int:
        """How many streams in the file are cover art."""
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream_disposition=attached_pic",
             "-of", "json", str(path)],
            capture_output=True, text=True, check=True,
        )
        streams = json.loads(probe.stdout)["streams"]
        return sum(1 for stream in streams if stream["disposition"]["attached_pic"])

    def test_cover_art_travels_into_the_aiff(self):
        source = self.cover_source()
        self.assertEqual(self.attached_pictures(source), 1)

        plan = ingest.scan(self.root, None, "aiff")
        report = ingest.run(plan, dry_run=False)
        row = next(row for row in report["written"] if row["name"] == "Ada Kaleh - Covered.aiff")
        self.assertFalse(row["cover_dropped"])
        self.assertEqual(self.attached_pictures(row["target"]), 1)
        # The APIC frame is what rekordbox reads the picture out of.
        self.assertIn(b"APIC", Path(row["target"]).read_bytes())

    def test_a_format_that_cannot_hold_artwork_says_so(self):
        self.cover_source()
        plan = ingest.scan(self.root, None, "wav")
        report = ingest.run(plan, dry_run=False)
        row = next(row for row in report["written"] if row["name"] == "Ada Kaleh - Covered.wav")
        self.assertTrue(row["cover_dropped"])
        self.assertEqual(self.attached_pictures(row["target"]), 0)

    def test_a_file_with_no_artwork_does_not_claim_to_have_lost_any(self):
        plan = ingest.scan(self.root, None, "aiff")
        report = ingest.run(plan, dry_run=False)
        row = next(row for row in report["written"] if row["name"] == "01 Ada Kaleh - Night Ferry.aiff")
        self.assertFalse(row["cover_dropped"])

    def test_a_file_that_cannot_be_decoded_is_reported_not_raised(self):
        broken = self.root / "broken.mp3"
        broken.write_bytes(b"this is not an mp3")
        plan = ingest.scan(self.root, None, "aiff")
        report = ingest.run(plan, dry_run=False)
        self.assertEqual(len(report["failed"]), 1)
        self.assertIn("broken.mp3", report["failed"][0]["source"])
        self.assertTrue(report["summary"]["converted"] >= 1)


if __name__ == "__main__":
    unittest.main()
