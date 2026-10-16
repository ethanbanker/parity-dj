import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from parity import writedb


# --------------------------------------------------------------- fakes


class FakeContent:
    def __init__(self, content_id, folder_path, file_name="track.wav", uuid="uuid-1", comment=None):
        self.ID = content_id
        self.FolderPath = folder_path
        self.FileNameL = file_name
        self.UUID = uuid
        self.CueUpdated = None
        # ``DjmdContent.Commnt``: the field the set tags are merged into.
        self.Commnt = comment


def cue(kind, msec, content_id, comment="", cue_id="cue-1"):
    return SimpleNamespace(
        ID=cue_id,
        ContentID=content_id,
        Kind=kind,
        InMsec=msec,
        Comment=comment,
        ContentUUID="uuid-1",
    )


def playlist(playlist_id, name, attribute=0):
    return SimpleNamespace(
        ID=playlist_id, Name=name, Attribute=attribute, ParentID="root", Seq=0
    )


def song(song_id, playlist_id, content_id, track_no, title="A Track"):
    return SimpleNamespace(
        ID=song_id,
        PlaylistID=playlist_id,
        ContentID=content_id,
        TrackNo=track_no,
        Content=SimpleNamespace(Title=title),
    )


class FakeDatabase:
    """The writer's own database surface, backed by lists.

    ``write_set(database=...)`` is the injectable seam: the same code path the
    real ``RekordboxDb`` drives is exercised here without SQLCipher.
    """

    def __init__(self, contents, cues=(), playlists=(), songs=()):
        self._contents = list(contents)
        self.cues = list(cues)
        self.playlists = list(playlists)
        self.songs = [dict(row) for row in songs]
        self.added_cues = []
        self.deleted_cues = []
        self.added_songs = []
        self.removed_songs = []
        self.created = []
        self.marked = []
        self.comments_changed = []
        self.committed = 0
        self.rolled_back = 0
        self.closed = 0
        self._next_id = 1000

    # -- reading ---------------------------------------------------------

    def contents(self):
        return list(self._contents)

    def playlist_by_name(self, name):
        for row in self.playlists:
            if row.Name.lower() == name.lower():
                return row
        return None

    def playlist_songs(self, playlist_row):
        rows = [row for row in self.songs if row["PlaylistID"] == playlist_row.ID]
        rows.sort(key=lambda row: row["TrackNo"])
        return [
            song(row["ID"], row["PlaylistID"], row["ContentID"], row["TrackNo"])
            for row in rows
        ]

    def cues_for(self, content_id):
        return [row for row in self.cues if row.ContentID == content_id]

    # -- writing ---------------------------------------------------------

    def create_playlist(self, name):
        row = playlist(str(900000 + len(self.playlists)), name)
        self.playlists.append(row)
        self.created.append(name)
        return row

    def add_playlist_song(self, playlist_row, content_id, track_no):
        self.added_songs.append((playlist_row.ID, content_id, track_no))
        self.songs.append(
            {
                "ID": f"song-{len(self.songs) + 1}",
                "PlaylistID": playlist_row.ID,
                "ContentID": str(content_id),
                "TrackNo": track_no,
            }
        )
        # Keep the simulated order truthful: everything sorts from TrackNo.
        return self.songs[-1]

    def delete_playlist_song(self, song_row):
        self.removed_songs.append((song_row.PlaylistID, song_row.ContentID))
        self.songs = [row for row in self.songs if row["ID"] != song_row.ID]

    def next_cue_id(self):
        self._next_id += 1
        return str(self._next_id)

    def add_cue(self, content, *, kind, msec, comment, colour_index):
        row = SimpleNamespace(
            ID=self.next_cue_id(),
            ContentID=str(content.ID),
            Kind=kind,
            InMsec=msec,
            Comment=comment,
            ColorTableIndex=colour_index,
        )
        self.cues.append(row)
        self.added_cues.append(row)
        return row

    def delete_cue(self, row):
        self.deleted_cues.append(row)
        self.cues = [item for item in self.cues if item is not row]

    def mark_cues_updated(self, content):
        content.CueUpdated = writedb.CUES_UPDATED_FLAG
        self.marked.append(str(content.ID))

    def set_comment(self, content, comment):
        content.Commnt = comment
        self.comments_changed.append((str(content.ID), comment))

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        self.closed += 1


def plan(slots=(("A", 0, "red", 1500), ("B", 1, "yellow", 30000))):
    hot = [
        {
            "slot": slot,
            "num": num,
            "name": f"cue {slot}",
            "colour": colour,
            "ms": ms,
            "seconds": ms / 1000.0,
        }
        for slot, num, colour, ms in slots
    ]
    return {"hot_cues": hot, "memory_cues": [{"name": "Chorus 2", "ms": 60000}]}


def track(key="k1", content_id="44211410", path="/Volumes/DJ/one.wav", title="One"):
    return {"key": key, "id": content_id, "path": path, "title": title, "artist": "A"}


# ----------------------------------------------------------------- tests


class PaletteTest(unittest.TestCase):
    def test_parity_colours_map_to_the_rekordbox_palette(self):
        self.assertEqual(writedb.palette_index("red"), 2)
        self.assertEqual(writedb.palette_index("yellow"), 4)
        self.assertEqual(writedb.palette_index("green"), 5)
        self.assertEqual(writedb.palette_index("blue"), 7)
        self.assertEqual(writedb.palette_index("CYAN"), 6)  # alias for aqua
        self.assertEqual(writedb.palette_index("chartreuse"), 0)


class ProcessGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "master.db"
        self.db_path.write_bytes(b"encrypted")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_running_app_blocks_the_write(self):
        running = [{"pid": 4242, "command": "/Applications/rekordbox 7/rekordbox.app/Contents/MacOS/rekordbox"}]
        with mock.patch.object(writedb, "running_processes", return_value=running):
            with self.assertRaises(writedb.WriteBlocked) as caught:
                writedb.assert_closed("rekordbox")
        self.assertIn("4242", str(caught.exception))
        self.assertIn("Quit rekordbox", str(caught.exception))

    def test_a_closed_app_does_not_block(self):
        with mock.patch.object(writedb, "running_processes", return_value=[]):
            writedb.assert_closed("rekordbox")  # must not raise

    def test_our_own_command_line_never_matches(self):
        own = f"{__file__} --from-rekordbox"
        with mock.patch.object(
            writedb, "_process_list", return_value=[(999999999, f"python3 {own}")]
        ):
            self.assertEqual(writedb.running_processes(writedb.REKORDBOX_PATTERNS), [])

    def test_the_app_binary_is_matched(self):
        rows = [
            (1, "/sbin/launchd"),
            (77, "/Applications/rekordbox 7/rekordbox.app/Contents/MacOS/rekordbox --flag"),
        ]
        with mock.patch.object(writedb, "_process_list", return_value=rows):
            found = writedb.running_processes(writedb.REKORDBOX_PATTERNS)
        self.assertEqual([row["pid"] for row in found], [77])
        self.assertNotIn("--flag", found[0]["command"])

    def test_write_set_refuses_while_the_app_runs(self):
        fake = FakeDatabase([FakeContent("44211410", "/Volumes/DJ")])
        with mock.patch.object(
            writedb, "assert_closed", side_effect=writedb.WriteBlocked("rekordbox is running (pid 1).")
        ):
            with self.assertRaises(writedb.WriteBlocked):
                writedb.write_set(
                    [track()],
                    {"k1": plan()},
                    db_path=self.db_path,
                    backup_root=Path(self.tmp.name) / "backups",
                )
        self.assertEqual(fake.committed, 0)

    def test_status_reports_a_blocked_write(self):
        running = [{"pid": 7, "command": "…/rekordbox.app/Contents/MacOS/rekordbox"}]
        with mock.patch.object(writedb, "running_processes", return_value=running):
            status = writedb.status(self.db_path, Path(self.tmp.name) / "backups")
        self.assertTrue(status["blocked"])
        self.assertFalse(status["writable"])
        self.assertEqual(status["running"][0]["pid"], 7)


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db_path = self.dir / "master.db"
        self.db_path.write_bytes(b"db")
        (self.dir / "master.db-wal").write_bytes(b"wal")
        (self.dir / "masterPlaylists6.xml").write_text("<DJ_PLAYLISTS/>", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_backup_copies_the_database_sidecars_and_playlist_xml(self):
        root = self.dir / "parity-writes"
        target = writedb.backup(self.db_path, root)
        self.assertTrue((target / "master.db").is_file())
        self.assertTrue((target / "master.db-wal").is_file())
        self.assertTrue((target / "masterPlaylists6.xml").is_file())

    def test_old_backups_are_pruned(self):
        root = self.dir / "parity-writes"
        for index in range(3):
            (root / f"2026010{index}-000000").mkdir(parents=True)
        writedb.backup(self.db_path, root, keep=2)
        self.assertEqual(len(list(root.iterdir())), 2)


class CueWriteTest(unittest.TestCase):
    def setUp(self):
        self.content = FakeContent("44211410", "/Volumes/DJ")
        self.other = FakeContent("999", "/Volumes/DJ", file_name="two.wav")
        self.db = FakeDatabase([self.content, self.other])
        self.tracks = [track()]

    def write(self, plans=None, **kwargs):
        options = {"playlist": "", "cues": True}
        options.update(kwargs)
        return writedb.write_set(
            self.tracks, plans if plans is not None else {"k1": plan()}, database=self.db, **options
        )

    def test_hot_cues_land_in_slots_a_to_h(self):
        report = self.write({"k1": plan(slots=(
            ("A", 0, "red", 1500),
            ("B", 1, "orange", 15000),
            ("C", 2, "yellow", 30000),
            ("D", 3, "green", 61000),
            ("E", 4, "aqua", 90000),
            ("F", 5, "purple", 120000),
            ("G", 6, "pink", 150000),
            ("H", 7, "blue", 180000),
        ))})
        kinds = [row.Kind for row in self.db.added_cues]
        self.assertEqual(kinds, [2, 3, 4, 5, 6, 7, 8, 9])
        self.assertEqual(
            [row.InMsec for row in self.db.added_cues],
            [1500, 15000, 30000, 61000, 90000, 120000, 150000, 180000],
        )
        # rekordbox palette ids: red, orange, yellow, green, aqua, purple, pink, blue.
        self.assertEqual([row.ColorTableIndex for row in self.db.added_cues], [2, 3, 4, 5, 6, 8, 1, 7])
        self.assertEqual(report["cues"]["created"], 8)
        self.assertEqual(report["cues"]["tracks"], 1)

    def test_the_content_is_flagged_as_having_new_cues(self):
        self.write()
        self.assertEqual(self.db.marked, ["44211410"])
        self.assertEqual(self.content.CueUpdated, "1")
        self.assertIsNone(self.other.CueUpdated)

    def test_frames_and_comments_follow_rekordbox_conventions(self):
        self.write()
        first = self.db.added_cues[0]
        self.assertEqual(first.Comment, "cue A")
        # 1500 ms at rekordbox's 150 fps for uncompressed audio.
        self.assertEqual(round(1500 * writedb.FRAMES_PER_SECOND / 1000.0), 225)

    def test_all_eight_hot_cue_slots_are_paritys_and_memory_cues_are_kept(self):
        self.db.cues = [
            cue(2, 999, "44211410", "old A", cue_id="old-a"),
            cue(6, 82076, "44211410", "hand placed E", cue_id="user-e"),
            cue(1, 0, "44211410", "1.1Bars", cue_id="memory"),
        ]
        report = self.write()
        # A-H all belong to Parity now, so a previous E is replaced too; the
        # memory cue is only touched when memory cues are written as well.
        self.assertEqual([row.ID for row in self.db.deleted_cues], ["old-a", "user-e"])
        self.assertEqual(report["cues"]["replaced"], 2)
        self.assertIn("memory", [row.ID for row in self.db.cues])

    def test_memory_cues_are_opt_in(self):
        self.db.cues = [cue(1, 0, "44211410", "1.1Bars", cue_id="memory")]
        self.write()
        self.assertEqual(self.db.deleted_cues, [])
        self.assertEqual([row.Kind for row in self.db.added_cues], [2, 3])

        fresh = FakeDatabase([self.content], cues=[cue(1, 0, "44211410", "1.1Bars", cue_id="memory")])
        report = writedb.write_set(
            self.tracks, {"k1": plan()}, playlist="", cues=True, memory=True, database=fresh
        )
        self.assertEqual([row.Kind for row in fresh.added_cues], [2, 3, 1])
        self.assertEqual(report["cues"]["memory"], 1)
        self.assertEqual([row.ID for row in fresh.deleted_cues], ["memory"])
        self.assertTrue(report["warnings"])

    def test_a_rekordbox_memory_cue_on_a_hot_cue_is_removed(self):
        """rekordbox puts a memory cue named ``1.1Bars`` on the first beat of
        most analysed tracks -- exactly where hot cue A goes.  The two used to
        sit on top of each other, so the memory cue goes and the pad stays."""
        self.db.cues = [cue(1, 0, "44211410", "1.1Bars", cue_id="memory")]
        report = self.write({"k1": plan(slots=(("A", 0, "red", 0),))})
        self.assertEqual([row.ID for row in self.db.deleted_cues], ["memory"])
        self.assertEqual(report["cues"]["removed_memory"], 1)
        self.assertEqual(report["cues"]["duplicates"][0]["comment"], "1.1Bars")
        self.assertEqual([row.InMsec for row in self.db.added_cues], [0])

    def test_a_memory_cue_anywhere_else_is_left_alone(self):
        self.db.cues = [cue(1, 60_000, "44211410", "hand placed", cue_id="memory")]
        report = self.write({"k1": plan(slots=(("A", 0, "red", 1500),))})
        self.assertEqual(self.db.deleted_cues, [])
        self.assertEqual(report["cues"]["removed_memory"], 0)

    def test_tracks_outside_the_library_are_reported_not_written(self):
        report = writedb.write_set(
            [track(key="k2", content_id="missing", path="/gone.wav")],
            {"k2": plan()},
            playlist="",
            database=self.db,
        )
        self.assertEqual(report["cues"]["created"], 0)
        self.assertEqual(report["cues"]["skipped"][0]["reason"], "not in rekordbox")

    def test_tracks_without_a_plan_are_skipped(self):
        report = self.write({"other-key": plan()})
        self.assertEqual(report["cues"]["created"], 0)
        self.assertEqual(report["cues"]["skipped"][0]["reason"], "no cue plan")

    # -- tracks that already have cues ------------------------------------

    def test_keep_leaves_a_track_the_user_cued_exactly_as_it_is(self):
        self.db.cues = [
            cue(2, 4000, "44211410", "my intro", cue_id="user-a"),
            cue(4, 61000, "44211410", "" , cue_id="user-c"),
            cue(1, 0, "44211410", "1.1Bars", cue_id="memory"),
        ]
        report = self.write({"k1": plan()}, cue_policy="keep")
        self.assertEqual(self.db.deleted_cues, [])
        self.assertEqual(self.db.added_cues, [])
        self.assertEqual(self.db.marked, [])
        self.assertEqual(report["cues"]["created"], 0)
        self.assertEqual(report["cues"]["kept"], [
            {
                "key": "k1",
                "label": "A - One",
                "cues": 2,
                "names": ["my intro", ""],
                "seconds": [4.0, 61.0],
                "reason": "already has cues",
            }
        ])
        self.assertEqual([row.Kind for row in self.db.cues if row.Kind == 2], [2])

    def test_keep_still_replaces_the_cues_parity_wrote_itself(self):
        """Otherwise choosing keep would freeze the library on the first write."""
        self.db.cues = [
            cue(2, 1500, "44211410", "First Downbeat", cue_id="ours-a"),
            cue(5, 30000, "44211410", "Main Drop", cue_id="ours-d"),
        ]
        report = self.write({"k1": plan()}, cue_policy="keep")
        self.assertEqual([row.ID for row in self.db.deleted_cues], ["ours-a", "ours-d"])
        self.assertEqual([row.Kind for row in self.db.added_cues], [2, 3])
        self.assertEqual(report["cues"]["kept"], [])
        self.assertEqual(report["cues"]["created"], 2)

    def test_a_lone_memory_cue_does_not_count_as_the_users_cues(self):
        """rekordbox puts one on nearly every analysed track; counting it would
        make "keep" mean "never cue anything again"."""
        self.db.cues = [cue(1, 0, "44211410", "1.1Bars", cue_id="memory")]
        report = self.write({"k1": plan()}, cue_policy="keep")
        self.assertEqual(report["cues"]["kept"], [])
        self.assertEqual([row.Kind for row in self.db.added_cues], [2, 3])

    def test_the_old_four_cue_layout_counts_as_paritys_own(self):
        self.db.cues = [cue(6, 30000, "44211410", "Mix-Out / Breakdown", cue_id="ours")]
        report = self.write({"k1": plan()}, cue_policy="keep")
        self.assertEqual(report["cues"]["kept"], [])
        self.assertEqual([row.ID for row in self.db.deleted_cues], ["ours"])

    def test_a_per_track_policy_beats_the_default(self):
        self.db.cues = [cue(2, 4000, "44211410", "my intro", cue_id="user-a")]
        report = self.write({"k1": plan()}, cue_policy="overwrite", cue_policies={"k1": "keep"})
        self.assertEqual([entry["key"] for entry in report["cues"]["kept"]], ["k1"])
        self.assertEqual(self.db.added_cues, [])

        other = FakeDatabase([self.content], cues=[cue(2, 4000, "44211410", "my intro", cue_id="user-a")])
        kept = writedb.write_set(
            self.tracks,
            {"k1": plan()},
            playlist="",
            cue_policy="keep",
            cue_policies={"k1": "overwrite"},
            database=other,
        )
        self.assertEqual(kept["cues"]["kept"], [])
        self.assertTrue(other.added_cues)

    def test_an_unknown_policy_falls_back_to_overwriting(self):
        self.db.cues = [cue(2, 4000, "44211410", "my intro", cue_id="user-a")]
        report = self.write({"k1": plan()}, cue_policy="keep-everything")
        self.assertEqual(report["cue_policy"], "overwrite")
        self.assertEqual(self.db.added_cues and [row.Kind for row in self.db.added_cues], [2, 3])

    def test_keep_still_writes_the_playlist_order(self):
        """"Keep their cues" is about cues -- the set still gets built."""
        self.db.cues = [cue(2, 4000, "44211410", "my intro", cue_id="user-a")]
        report = self.write({"k1": plan()}, cue_policy="keep", playlist="Friday Set")
        self.assertEqual(report["cues"]["kept"][0]["key"], "k1")
        self.assertEqual(report["playlist"]["tracks"], 1)
        self.assertTrue(self.db.added_songs)

    def test_dry_run_rolls_back_and_writes_nothing(self):
        report = self.write({"k1": plan()}, dry_run=True)
        self.assertTrue(report["dry_run"])
        self.assertEqual(self.db.committed, 0)
        self.assertEqual(self.db.rolled_back, 1)
        self.assertIsNone(report["backup"])

    def test_a_real_write_commits_once(self):
        report = self.write({"k1": plan()}, dry_run=False)
        self.assertEqual(self.db.committed, 1)
        self.assertEqual(self.db.rolled_back, 0)
        self.assertTrue(report["cues"]["rows"])

    def test_a_live_dry_run_uses_a_snapshot_and_never_the_live_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "master.db"
            db_path.write_bytes(b"encrypted")
            seen = {}

            class RecordingDb(FakeDatabase):
                def __init__(self, path):
                    super().__init__([FakeContent("44211410", "/Volumes/DJ")])
                    seen["opened"] = Path(path)

            with mock.patch.object(writedb, "RekordboxDb", RecordingDb):
                report = writedb.write_set(
                    self.tracks, {"k1": plan()}, dry_run=True, db_path=db_path
                )
            self.assertTrue(report["dry_run"])
            self.assertNotEqual(seen["opened"], db_path)
            self.assertEqual(seen["opened"].name, "master.db")


class TagWriteTest(unittest.TestCase):
    """The set tags, written into ``DjmdContent.Commnt``.

    The rule the whole feature rests on: a comment the DJ typed is kept, and
    Parity's own tag from an earlier write is replaced rather than stacked.
    """

    def setUp(self):
        self.content = FakeContent("44211410", "/Volumes/DJ")
        self.other = FakeContent("999", "/Volumes/DJ", file_name="two.wav", comment="my own note")
        self.db = FakeDatabase([self.content, self.other])
        self.tracks = [
            track(key="k1", content_id="44211410", path="/Volumes/DJ/one.wav", title="One"),
            track(key="k2", content_id="999", path="/Volumes/DJ/two.wav", title="Two"),
        ]

    def write(self, tags, **kwargs):
        options = {"playlist": "", "cues": False}
        options.update(kwargs)
        return writedb.write_set(self.tracks, {}, tags=tags, database=self.db, **options)

    def test_a_tag_is_appended_to_the_tracks_comment(self):
        report = self.write({"k2": "[E7] [peak] 2/2"})
        self.assertEqual(self.other.Commnt, "my own note [E7] [peak] 2/2")
        self.assertEqual(self.content.Commnt, None)  # no tag asked for this one
        self.assertEqual(report["tags"]["changed"], 1)
        self.assertEqual(report["tags"]["rows"][0]["tag"], "[E7] [peak] 2/2")
        self.assertEqual(report["tags"]["rows"][0]["before"], "my own note")
        self.assertEqual(report["tags"]["rows"][0]["label"], "A - Two")

    def test_our_own_previous_tag_is_replaced(self):
        self.other.Commnt = "my own note [E5] [build] 2/9"
        self.write({"k2": "[E7] [peak] 2/2"})
        self.assertEqual(self.other.Commnt, "my own note [E7] [peak] 2/2")

    def test_a_track_already_tagged_this_way_is_left_alone(self):
        self.other.Commnt = "my own note [E7] [peak] 2/2"
        report = self.write({"k2": "[E7] [peak] 2/2"})
        self.assertEqual(report["tags"]["changed"], 0)
        self.assertEqual(report["tags"]["unchanged"], 1)
        self.assertEqual(self.db.comments_changed, [])

    def test_a_track_with_no_tag_is_reported_not_written(self):
        report = self.write({"k1": "[E4] [opener] 1/2"})
        self.assertEqual([row["key"] for row in report["tags"]["skipped"]], ["k2"])
        self.assertEqual(self.other.Commnt, "my own note")

    def test_no_tags_means_nothing_is_written(self):
        report = writedb.write_set(
            self.tracks, {}, playlist="", cues=False, database=self.db
        )
        self.assertEqual(report["tags"]["changed"], 0)
        self.assertEqual(self.db.comments_changed, [])
        self.assertEqual(self.other.Commnt, "my own note")
        self.assertIsNone(self.content.Commnt)

    def test_a_dry_run_rolls_the_comments_back(self):
        report = self.write({"k2": "[E7] [peak] 2/2"}, dry_run=True)
        self.assertEqual(self.db.comments_changed, [("999", "my own note [E7] [peak] 2/2")])
        self.assertEqual(self.db.rolled_back, 1)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["tags"]["changed"], 1)

    def test_a_comment_past_the_column_limit_is_warned_about(self):
        self.other.Commnt = "x" * 250
        report = self.write({"k2": "[E7] [peak] 2/2"})
        self.assertEqual(report["tags"]["changed"], 1)
        self.assertEqual([row["key"] for row in report["tags"]["over_limit"]], ["k2"])
        self.assertTrue(report["warnings"])
        self.assertIn(str(writedb.COMMENT_LIMIT), report["warnings"][0])

    def test_a_short_comment_stays_off_the_warning_list(self):
        report = self.write({"k2": "[E7] [peak] 2/2"})
        self.assertEqual(report["tags"]["over_limit"], [])
        self.assertEqual(report["warnings"], [])

    def test_tags_ride_along_with_the_cue_write_in_one_transaction(self):
        report = writedb.write_set(
            self.tracks,
            {"k1": plan()},
            playlist="Friday Set",
            cues=True,
            tags={"k1": "[E4] [opener] 1/2", "k2": "[E7] [closer] 2/2"},
            database=self.db,
        )
        self.assertEqual(report["cues"]["created"], 2)
        self.assertEqual(report["tags"]["changed"], 2)
        self.assertEqual(self.content.Commnt, "[E4] [opener] 1/2")
        self.assertTrue(report["playlist"]["created"])
        self.assertEqual(self.db.committed, 1)


class PlaylistWriteTest(unittest.TestCase):
    def setUp(self):
        self.content = FakeContent("44211410", "/Volumes/DJ")
        self.second = FakeContent("999", "/Volumes/DJ", file_name="two.wav")
        self.third = FakeContent("888", "/Volumes/DJ", file_name="three.wav")
        self.db = FakeDatabase([self.content, self.second, self.third])
        self.tracks = [
            track(key="k1", content_id="999", path="/Volumes/DJ/two.wav", title="Two"),
            track(key="k2", content_id="44211410", path="/Volumes/DJ/one.wav", title="One"),
        ]

    def test_a_missing_playlist_is_created_and_ordered(self):
        report = writedb.write_set(
            self.tracks, {}, playlist="Friday Set", database=self.db, cues=False
        )
        self.assertTrue(report["playlist"]["created"])
        self.assertEqual([row["ContentID"] for row in self.db.songs], ["999", "44211410"])
        self.assertEqual([row["TrackNo"] for row in self.db.songs], [1, 2])
        self.assertEqual(self.db.created, ["Friday Set"])

    def test_an_existing_playlist_is_reordered(self):
        existing = playlist("2235858641", "Friday Set")
        self.db.playlists.append(existing)
        self.db.songs = [
            {"ID": "s1", "PlaylistID": existing.ID, "ContentID": "44211410", "TrackNo": 1},
            {"ID": "s2", "PlaylistID": existing.ID, "ContentID": "999", "TrackNo": 2},
        ]
        report = writedb.write_set(
            self.tracks, {}, playlist="Friday Set", database=self.db, cues=False
        )
        self.assertTrue(report["playlist"]["rewritten"])
        self.assertEqual(report["playlist"]["removed"], 2)
        self.assertEqual([row["ContentID"] for row in self.db.songs], ["999", "44211410"])
        self.assertEqual(self.db.created, [])

    def test_a_playlist_already_in_order_is_left_alone(self):
        existing = playlist("2235858641", "Friday Set")
        self.db.playlists.append(existing)
        self.db.songs = [
            {"ID": "s1", "PlaylistID": existing.ID, "ContentID": "999", "TrackNo": 1},
            {"ID": "s2", "PlaylistID": existing.ID, "ContentID": "44211410", "TrackNo": 2},
        ]
        report = writedb.write_set(
            self.tracks, {}, playlist="Friday Set", database=self.db, cues=False
        )
        self.assertFalse(report["playlist"]["rewritten"])
        self.assertEqual(report["playlist"]["added"], 0)
        self.assertEqual(report["playlist"]["removed"], 0)
        self.assertEqual(self.db.added_songs, [])

    def test_songs_outside_the_set_are_removed(self):
        existing = playlist("2235858641", "Friday Set")
        self.db.playlists.append(existing)
        self.db.songs = [
            {"ID": "s1", "PlaylistID": existing.ID, "ContentID": "888", "TrackNo": 1},
        ]
        report = writedb.write_set(
            self.tracks, {}, playlist="Friday Set", database=self.db, cues=False
        )
        self.assertEqual(report["playlist"]["removed"], 1)
        self.assertEqual([row["ContentID"] for row in self.db.songs], ["999", "44211410"])

    def test_a_folder_is_refused(self):
        self.db.playlists.append(playlist("12", "Friday Set", attribute=1))
        with self.assertRaises(writedb.WriteBlocked) as caught:
            writedb.write_set(self.tracks, {}, playlist="Friday Set", database=self.db, cues=False)
        self.assertIn("folder", str(caught.exception))

    def test_no_playlist_name_means_no_playlist_write(self):
        report = writedb.write_set(self.tracks, {}, playlist="", database=self.db, cues=False)
        self.assertIsNone(report["playlist"])
        self.assertEqual(self.db.songs, [])

    def _out_of_order(self):
        existing = playlist("2235858641", "Friday Set")
        self.db.playlists.append(existing)
        self.db.songs = [
            {"ID": "s1", "PlaylistID": existing.ID, "ContentID": "44211410", "TrackNo": 1},
            {"ID": "s2", "PlaylistID": existing.ID, "ContentID": "999", "TrackNo": 2},
        ]
        return existing

    def test_the_reorder_is_one_transaction(self):
        """Every row moves in the caller's commit, not one commit per row."""
        self._out_of_order()
        report = writedb.write_set(
            self.tracks, {}, playlist="Friday Set", database=self.db, cues=False
        )
        self.assertEqual(self.db.committed, 1)
        self.assertEqual(self.db.rolled_back, 0)
        self.assertEqual(report["playlist"]["removed"], 2)
        self.assertEqual(report["playlist"]["added"], 2)

    def test_a_dry_run_reorder_commits_nothing(self):
        """A dry run that reorders the playlist still must not commit.

        This is the regression for the dry run that used to die with an unhandled
        ``RuntimeError`` from pyrekordbox whenever rekordbox was open: the reorder
        went through ``remove_from_playlist``, which commits internally, and
        pyrekordbox's ``commit`` refuses to run while the app is up.
        """
        self._out_of_order()
        report = writedb.write_set(
            self.tracks, {}, playlist="Friday Set", database=self.db, cues=False, dry_run=True
        )
        self.assertTrue(report["playlist"]["rewritten"])
        self.assertEqual(self.db.committed, 0)
        self.assertEqual(self.db.rolled_back, 1)


class StubDjmdSongPlaylist:
    """``models.DjmdSongPlaylist`` stand-in that records the rows it is given."""

    rows: list = []

    @classmethod
    def create(cls, **kwargs):
        row = SimpleNamespace(**kwargs)
        cls.rows.append(row)
        return row


class StubMasterDb:
    """A pyrekordbox ``MasterDatabase`` that only records session traffic."""

    def __init__(self, path=None, db_dir=None):
        self.added: list = []
        self.deleted: list = []
        self.commits = 0

    def add(self, row):
        self.added.append(row)

    def delete(self, row):
        self.deleted.append(row)

    def commit(self):
        self.commits += 1


class PlaylistAdapterTest(unittest.TestCase):
    """The adapter's playlist rows stay in the caller's transaction.

    pyrekordbox's own ``remove_from_playlist`` commits once per row, which would
    split the rewrite into N transactions *and* make a dry run depend on a write.
    """

    def setUp(self):
        models = SimpleNamespace(DjmdSongPlaylist=StubDjmdSongPlaylist)
        StubDjmdSongPlaylist.rows = []
        with mock.patch.object(
            writedb, "_load_pyrekordbox", return_value=(StubMasterDb, models, None)
        ):
            self.handle = writedb.RekordboxDb(Path("/tmp/master.db"))

    def test_deleting_a_song_goes_through_the_session_without_committing(self):
        row = SimpleNamespace(ID="s1", PlaylistID="p1", ContentID="44211410", TrackNo=1)
        self.handle.delete_playlist_song(row)
        self.assertEqual(self.handle.db.deleted, [row])
        self.assertEqual(self.handle.db.commits, 0)

    def test_adding_a_song_fills_the_rekordbox_columns_without_committing(self):
        list_row = SimpleNamespace(ID="91499145", Name="Friday Set")
        row = self.handle.add_playlist_song(list_row, "44211410", 3)
        self.assertEqual(self.handle.db.commits, 0)
        self.assertEqual(self.handle.db.added, [row])
        self.assertEqual(row.PlaylistID, "91499145")
        self.assertEqual(row.ContentID, "44211410")
        self.assertEqual(row.TrackNo, 3)
        # ID and UUID are fresh per row, exactly as pyrekordbox writes them.
        self.assertTrue(row.ID)
        self.assertTrue(row.UUID)
        self.assertNotEqual(row.ID, row.UUID)
        self.assertIsNotNone(row.created_at)
        self.assertIsNotNone(row.updated_at)
