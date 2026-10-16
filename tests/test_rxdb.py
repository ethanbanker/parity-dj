import datetime
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from parity import rxdb


class Related:
    def __init__(self, name):
        self.Name = name


class Key:
    def __init__(self, scale):
        self.ScaleName = scale


def content(
    content_id,
    folder_path,
    *,
    title="A Track",
    artist=None,
    genre=None,
    key=None,
    bpm=12800,
    length=300,
    analysed=105,
    analysis_path="/PIONEER/USBANLZ/abc/ANLZ0000.DAT",
    file_type=11,
    rating=204,
):
    return SimpleNamespace(
        ID=content_id,
        Title=title,
        Artist=Related(artist) if artist else None,
        Album=Related("An Album"),
        Genre=Related(genre) if genre else None,
        Label=Related("A Label"),
        Key=Key(key) if key else None,
        Commnt="E7",
        BPM=bpm,
        Length=length,
        Rating=rating,
        DJPlayCount=12,
        ReleaseYear=2024,
        TrackNo=3,
        ColorID=0,
        DateCreated=datetime.datetime(2026, 9, 25, 12, 0, 0),
        Analysed=analysed,
        AnalysisDataPath=analysis_path,
        FolderPath=folder_path,
        FileType=file_type,
    )


class FakeDb:
    """Duck-typed stand-in for pyrekordbox's MasterDatabase."""

    def __init__(self, contents, cues=(), playlists=(), anlz=None):
        self._contents = list(contents)
        self._cues = list(cues)
        self._playlists = list(playlists)
        self._anlz = anlz or {}

    def get_content(self):
        return list(self._contents)

    def get_cue(self):
        return list(self._cues)

    def get_playlist(self):
        return list(self._playlists)

    def get_playlist_contents(self, row):
        return list(getattr(row, "contents", []))

    def get_anlz_paths(self, item):
        return {}


def cue(kind, msec, content_id, comment="", out=-1, loop=0):
    return SimpleNamespace(
        Kind=kind,
        InMsec=msec,
        OutMsec=out,
        Comment=comment,
        ActiveLoop=loop,
        ColorTableIndex=0,
        ContentID=content_id,
    )


class LibraryConversionTest(unittest.TestCase):
    def setUp(self):
        self.local = content(
            "1",
            "/Volumes/EXT SSD/RekordBox Library/Syd Bday Playlist/one.wav",
            title="I Run",
            artist="HAVEN.",
            genre="House",
            key="2A",
        )
        self.cloud = content(
            "2",
            "soundcloud:tracks:862894612",
            title="Cloud Edit",
            file_type=19,
            analysed=0,
            analysis_path="",
        )
        self.db = FakeDb([self.local, self.cloud], [cue(1, 1846, "1", "1.1Bars")])
        self.library = rxdb._library_from_db(self.db, source="/tmp/master.db", product={"Name": "rekordbox"})

    def test_tracks_are_read(self):
        self.assertEqual(set(self.library.tracks), {"1", "2"})
        self.assertEqual(self.library.source_kind, "db")
        self.assertEqual(self.library.product["Name"], "rekordbox")

    def test_local_track_fields(self):
        track = self.library.tracks["1"]
        self.assertEqual(track.title, "I Run")
        self.assertEqual(track.artist, "HAVEN.")
        self.assertEqual(track.genre, "House")
        self.assertEqual(track.label, "A Label")
        self.assertEqual(track.tonality, "2A")
        self.assertEqual(track.bpm, 128.0)  # stored as BPM * 100
        self.assertEqual(track.duration, 300.0)
        self.assertEqual(track.rating, 204)
        self.assertEqual(track.play_count, 12)
        self.assertEqual(track.year, 2024)
        self.assertEqual(track.track_number, 3)
        self.assertEqual(track.date_added, "2026-09-25")
        self.assertEqual(track.kind, "WAV File")
        self.assertFalse(track.is_streaming)
        self.assertTrue(track.analysed)
        self.assertEqual(
            track.path,
            "/Volumes/EXT SSD/RekordBox Library/Syd Bday Playlist/one.wav",
        )
        self.assertEqual(
            track.folder, "/Volumes/EXT SSD/RekordBox Library/Syd Bday Playlist"
        )

    def test_streaming_entries_are_flagged(self):
        track = self.library.tracks["2"]
        self.assertEqual(track.path, "")
        self.assertTrue(track.is_streaming)
        self.assertFalse(track.analysed)

    def test_cue_kind_maps_to_the_xml_slot(self):
        track = self.library.tracks["1"]
        self.assertEqual(len(track.cues), 1)
        # Kind 1 is a memory cue, Kind 6 (verified against a real export) is
        # hot cue E -> Num 4.
        self.assertEqual(track.cues[0]["num"], -1)
        self.assertEqual(track.cues[0]["kind"], "memory")
        self.assertEqual(track.cues[0]["start"], 1.846)
        self.assertEqual(track.cues[0]["name"], "1.1Bars")

    def test_hot_cue_kinds_map_to_numbers(self):
        rows = rxdb._cue_rows(
            {"1": [cue(6, 82076, "1"), cue(2, 5000, "1"), cue(9, 1000, "1")]}, "1"
        )
        by_num = {row["num"]: row for row in rows}
        self.assertEqual(sorted(by_num), [0, 4, 7])
        self.assertEqual(by_num[4]["start"], 82.076)
        self.assertEqual(by_num[4]["kind"], "hot")
        self.assertEqual(by_num[7]["num"], 7)  # clamped to hot cue H

    def test_to_dict_carries_the_new_fields(self):
        data = self.library.tracks["1"].to_dict()
        self.assertTrue(data["analysed"])
        self.assertEqual(data["folder"], self.library.tracks["1"].folder)
        self.assertIn("cues", data)
        self.assertFalse(data["streaming"])


class PlaylistTreeTest(unittest.TestCase):
    def test_folders_and_playlists_are_nested(self):
        one = content("1", "/x/one.wav")
        two = content("2", "/x/two.wav")
        folder = SimpleNamespace(ID="F1", Name="Crates", ParentID="root", contents=[one])
        child = SimpleNamespace(ID="P1", Name="Syd Bday", ParentID="F1", contents=[two])
        folder.children = [child]
        db = FakeDb([one, two], [], [folder, child])
        library = rxdb._library_from_db(db, source="s", product={})
        self.assertEqual(len(library.playlists), 1)
        root = library.playlists[0]
        self.assertEqual(root.name, "Crates")
        self.assertEqual(root.track_ids, ["1"])
        self.assertEqual([child.name for child in root.children], ["Syd Bday"])
        self.assertEqual(root.children[0].track_ids, ["2"])
        self.assertEqual(root.total_tracks(), 2)


class LocationTest(unittest.TestCase):
    def test_local_paths_are_file_urls(self):
        self.assertEqual(
            rxdb._location("/Volumes/EXT SSD/a b.wav"),
            "file://localhost/Volumes/EXT%20SSD/a%20b.wav",
        )

    def test_streaming_ids_stay_recognisable(self):
        location = rxdb._location("soundcloud:tracks:862894612")
        self.assertEqual(location, "file://localhostsoundcloud:tracks:862894612")
        self.assertTrue(rxdb.Track(track_id="1", location=location).is_streaming)

    def test_empty_location(self):
        self.assertEqual(rxdb._location(""), "")

    def test_file_kind_from_the_extension(self):
        self.assertEqual(rxdb._file_kind("/x/a.wav"), "WAV File")
        self.assertEqual(rxdb._file_kind("/x/a.MP3"), "MP3 File")
        self.assertEqual(rxdb._file_kind(""), "")


class AnlzPathTest(unittest.TestCase):
    def test_relative_analysis_path_is_joined_to_the_share_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            share = Path(tmp) / "share"
            dat = share / "PIONEER" / "USBANLZ" / "abc" / "ANLZ0000.DAT"
            dat.parent.mkdir(parents=True)
            dat.write_bytes(b"x")
            dat.with_suffix(".EXT").write_bytes(b"x")

            item = content("1", "/x/a.wav", analysis_path="/PIONEER/USBANLZ/abc/ANLZ0000.DAT")
            found = rxdb._anlz_paths(FakeDb([]), item, share)
            self.assertEqual(found["DAT"], str(dat))
            self.assertEqual(found["EXT"], str(dat.with_suffix(".EXT")))

    def test_missing_analysis_yields_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = content("1", "/x/a.wav", analysis_path="/PIONEER/nope.DAT")
            self.assertEqual(rxdb._anlz_paths(FakeDb([]), item, Path(tmp)), {})

    def test_no_analysis_path_at_all(self):
        item = content("1", "/x/a.wav", analysis_path="")
        self.assertEqual(rxdb._anlz_paths(FakeDb([]), item, Path("/nope")), {})


class PathsTest(unittest.TestCase):
    def test_env_override_wins(self):
        previous = os.environ.get(rxdb.ENV_MASTER_DB)
        os.environ[rxdb.ENV_MASTER_DB] = "/tmp/custom.db"
        try:
            self.assertEqual(rxdb.master_db_path(), Path("/tmp/custom.db"))
        finally:
            if previous is None:
                os.environ.pop(rxdb.ENV_MASTER_DB, None)
            else:
                os.environ[rxdb.ENV_MASTER_DB] = previous

    def test_default_is_the_rekordbox_folder(self):
        self.assertEqual(rxdb.master_db_path().name, "master.db")
        self.assertIn("Pioneer", str(rxdb.master_db_path()))
        self.assertEqual(rxdb.share_dir().name, "share")

    def test_extras_status_shape(self):
        status = rxdb.extras_status()
        for field in ("available", "version", "error", "interpreter", "hint"):
            self.assertIn(field, status)
        self.assertIsInstance(status["available"], bool)


class SnapshotTest(unittest.TestCase):
    def test_database_and_sidecars_are_copied(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "rekordbox"
            root.mkdir()
            (root / "master.db").write_bytes(b"db")
            (root / "master.db-wal").write_bytes(b"wal")
            (root / "master.db-shm").write_bytes(b"shm")
            (root / "masterPlaylists6.xml").write_text("<x/>")
            destination = Path(tmp) / "copy"

            copy = rxdb.snapshot(root / "master.db", destination)
            self.assertEqual(copy.name, "master.db")
            self.assertEqual(copy.read_bytes(), b"db")
            self.assertTrue((destination / "master.db-wal").exists())
            self.assertTrue((destination / "master.db-shm").exists())
            self.assertTrue((destination / "masterPlaylists6.xml").exists())

    def test_missing_sidecars_are_tolerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "master.db").write_bytes(b"db")
            destination = Path(tmp) / "out"
            copy = rxdb.snapshot(root / "master.db", destination)
            self.assertTrue(copy.exists())


class ReadLibraryTest(unittest.TestCase):
    def test_missing_database_raises_with_a_useful_message(self):
        with self.assertRaises(rxdb.MasterDbUnavailable) as caught:
            rxdb.read_library(Path("/definitely/not/here/master.db"))
        self.assertIn("No rekordbox database", str(caught.exception))

    def test_fallback_to_xml_when_the_database_cannot_be_opened(self):
        fixture = Path(__file__).parent / "fixtures" / "collection.xml"
        library, kind, detail = rxdb.read_library_or_xml(
            fixture, Path("/definitely/not/here/master.db")
        )
        self.assertEqual(kind, "xml")
        self.assertEqual(len(library.tracks), 12)
        self.assertIn("master.db unavailable", detail)

    def test_no_fallback_raises(self):
        with self.assertRaises(rxdb.MasterDbUnavailable):
            rxdb.read_library_or_xml(None, Path("/definitely/not/here/master.db"))


class DbStatusTest(unittest.TestCase):
    def test_reports_a_missing_database(self):
        status = rxdb.db_status(Path("/definitely/not/here/master.db"))
        self.assertFalse(status["exists"])
        self.assertFalse(status["readable"])
        self.assertIn("No rekordbox database", status["error"])

    def test_reports_a_present_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "master.db"
            path.write_bytes(b"x" * 32)
            status = rxdb.db_status(path)
            self.assertTrue(status["exists"])
            self.assertEqual(status["size"], 32)
            self.assertIsNotNone(status["modified"])
            self.assertIn("path", status)
            self.assertIn("pyrekordbox", status)


if __name__ == "__main__":
    unittest.main()
