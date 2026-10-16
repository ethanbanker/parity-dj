"""Tests for :mod:`parity.preflight`.

Every case here is a fact about a file on disk or about the set itself, so the
tests use real temporary files: a check that only works against a mocked
``Path.is_file`` would not prove that an unplugged drive is noticed.
"""

import tempfile
import unittest
from pathlib import Path

from parity import preflight


def track(key, path="", *, streaming=False, cues=True, title=None, artist="A"):
    return {
        "key": key,
        "title": title or f"Track {key}",
        "artist": artist,
        "path": path,
        "streaming": streaming,
        "cues": {"analysed": cues},
    }


class PreflightTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.one = self.dir / "one.wav"
        self.one.write_bytes(b"audio")
        self.two = self.dir / "two.wav"
        self.two.write_bytes(b"audio")

    def kinds(self, report):
        return [issue["kind"] for issue in report["issues"]]

    # -- the clean case ---------------------------------------------------

    def test_a_healthy_set_is_all_clear(self):
        report = preflight.inspect([track("a", str(self.one)), track("b", str(self.two))])
        self.assertTrue(report["ok"])
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["checked"], 2)
        self.assertEqual(report["counts"], {"error": 0, "warning": 0})
        self.assertIn("All clear", report["text"])
        self.assertIn("2 tracks checked", report["text"])

    def test_tracks_with_no_key_are_not_a_duplicate_of_each_other(self):
        """A row with no key is missing data, not the same track twice.

        Grouping them under the empty string reported the second keyless row as
        a duplicate of the first, which is a claim about the music that the
        data does not make.
        """
        report = preflight.inspect(
            [track("", str(self.one), title="One"), track("", str(self.two), title="Two")]
        )
        self.assertEqual(report["summary"]["duplicate_track"], 0)
        self.assertNotIn("duplicate_track", self.kinds(report))
        self.assertTrue(report["ok"])

    # -- missing files ----------------------------------------------------

    def test_a_file_that_is_gone_is_an_error(self):
        report = preflight.inspect([track("a", str(self.dir / "gone.wav"))])
        self.assertFalse(report["ok"])
        self.assertEqual(self.kinds(report), ["missing_file"])
        self.assertEqual(report["counts"]["error"], 1)
        self.assertIn("no file at", report["issues"][0]["detail"])
        self.assertEqual(report["issues"][0]["positions"], [1])

    def test_a_track_with_no_path_at_all_is_reported(self):
        report = preflight.inspect([track("a")])
        self.assertEqual(self.kinds(report), ["missing_file"])
        self.assertIn("no file path", report["issues"][0]["detail"])

    def test_a_missing_file_is_reported_once_per_track(self):
        """Two different tracks on one dead path are two missing files, but not
        also a duplicate-file issue: the same file twice says nothing new."""
        gone = str(self.dir / "gone.wav")
        report = preflight.inspect([track("a", gone), track("b", gone)])
        self.assertEqual(self.kinds(report), ["missing_file", "missing_file"])

    def test_a_streaming_track_is_a_warning_not_a_missing_file(self):
        report = preflight.inspect([track("a", "", streaming=True)])
        self.assertEqual(self.kinds(report), ["streaming"])
        self.assertEqual(report["counts"], {"error": 0, "warning": 1})
        self.assertTrue(report["ok"])

    # -- duplicates -------------------------------------------------------

    def test_the_same_track_twice_is_an_error(self):
        report = preflight.inspect([track("a", str(self.one)), track("a", str(self.one))])
        self.assertIn("duplicate_track", self.kinds(report))
        issue = next(i for i in report["issues"] if i["kind"] == "duplicate_track")
        self.assertEqual(issue["positions"], [1, 2])
        self.assertIn("tracks 1 and 2", issue["detail"])
        self.assertFalse(report["ok"])

    def test_two_tracks_on_one_file_is_an_error(self):
        report = preflight.inspect(
            [track("a", str(self.one), title="One"), track("b", str(self.one), title="One Again")]
        )
        issue = next(i for i in report["issues"] if i["kind"] == "duplicate_file")
        self.assertEqual(issue["positions"], [1, 2])
        self.assertIn(str(self.one), issue["detail"])

    def test_the_same_artist_and_title_from_two_files_is_a_warning(self):
        report = preflight.inspect(
            [
                track("a", str(self.one), title="Same", artist="Who"),
                track("b", str(self.two), title="Same", artist="Who"),
            ]
        )
        kinds = self.kinds(report)
        self.assertIn("duplicate_name", kinds)
        self.assertNotIn("duplicate_file", kinds)
        self.assertTrue(report["ok"])  # a warning, not an error

    def test_the_same_title_from_one_file_is_not_reported_twice(self):
        report = preflight.inspect(
            [
                track("a", str(self.one), title="Same"),
                track("b", str(self.one), title="Same"),
            ]
        )
        self.assertEqual(self.kinds(report).count("duplicate_name"), 0)

    # -- cues -------------------------------------------------------------

    def test_a_track_with_no_cue_plan_is_a_warning(self):
        report = preflight.inspect([track("a", str(self.one), cues=False)])
        self.assertEqual(self.kinds(report), ["no_cues"])
        self.assertTrue(report["ok"])

    def test_cues_are_not_checked_when_they_are_not_being_exported(self):
        report = preflight.inspect([track("a", str(self.one), cues=False)], check_cues=False)
        self.assertEqual(report["issues"], [])

    # -- the report -------------------------------------------------------

    def test_the_text_report_lists_the_problems_by_severity(self):
        report = preflight.inspect(
            [
                track("a", str(self.one), title="One"),
                track("b", str(self.dir / "gone.wav"), title="Gone", cues=False),
                track("c", "", streaming=True, title="Stream"),
            ],
            set_name="Friday Set",
        )
        text = report["text"]
        self.assertIn("Friday Set", text)
        self.assertIn("ERRORS", text)
        self.assertIn("WARNINGS", text)
        self.assertIn("Tracks with no file behind them", text)
        self.assertIn("Streaming-only tracks", text)
        self.assertIn("nothing here blocks", text.lower())
        # one missing file; the streaming stub and the track with no cue plan.
        self.assertEqual(report["counts"], {"error": 1, "warning": 2})
        self.assertFalse(report["ok"])

    def test_errors_are_listed_before_warnings(self):
        report = preflight.inspect(
            [
                track("a", str(self.dir / "gone.wav")),
                track("b", "", streaming=True),
            ]
        )
        severities = [issue["severity"] for issue in report["issues"]]
        self.assertEqual(severities, sorted(severities, key=lambda s: preflight.SEVERITIES.index(s)))

    def test_the_summary_counts_every_kind(self):
        report = preflight.inspect([track("a", str(self.dir / "gone.wav"))])
        self.assertEqual(sorted(report["summary"]), sorted(preflight.ISSUE_KINDS))
        self.assertEqual(report["summary"]["missing_file"], 1)
        self.assertEqual(report["summary"]["duplicate_track"], 0)


if __name__ == "__main__":
    unittest.main()
