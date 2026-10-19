import tempfile
import unittest
from pathlib import Path

from parity import watchfolder
from parity.store import Store


def touch(path: Path, content: bytes = b"not really audio") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


class M3UParseTest(unittest.TestCase):
    def test_extinf_names_relative_paths_and_playlist(self):
        parsed = watchfolder.parse_m3u(
            "#EXTM3U\n#PLAYLIST:Friday\n"
            "#EXTINF:372,Ada Kaleh - Night Ferry\nada.mp3\n"
            "#EXTINF:0,Someone - Other\nsub/other.wav\n",
            base_dir="/music/sets",
        )
        self.assertEqual(parsed["name"], "Friday")
        self.assertEqual(len(parsed["entries"]), 2)
        first = parsed["entries"][0]
        self.assertEqual(first["artist"], "Ada Kaleh")
        self.assertEqual(first["title"], "Night Ferry")
        self.assertEqual(first["duration"], 372.0)
        self.assertEqual(first["path"], "/music/sets/ada.mp3")

    def test_streaming_urls_are_kept_and_marked(self):
        parsed = watchfolder.parse_m3u("https://example.com/track.mp3\n")
        self.assertTrue(parsed["entries"][0]["streaming"])
        self.assertEqual(parsed["entries"][0]["path"], "https://example.com/track.mp3")

    def test_file_urls_are_decoded(self):
        parsed = watchfolder.parse_m3u("file://localhost/music/a%20b.mp3\n")
        self.assertEqual(parsed["entries"][0]["path"], "/music/a b.mp3")

    def test_comments_are_ignored(self):
        parsed = watchfolder.parse_m3u("#EXTM3U\n# a note\n\n/music/a.mp3\n")
        self.assertEqual(len(parsed["entries"]), 1)


class M3UImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir / "data")
        self.music = self.dir / "music"
        touch(self.music / "01_Ada_Kaleh_-_Night_Ferry.mp3")
        touch(self.music / "Soha - Rivet.wav")
        self.playlist = self.dir / "friday.m3u8"
        self.playlist.write_text(
            "#EXTM3U\n#PLAYLIST:Friday\n"
            "#EXTINF:300,Ada Kaleh - Night Ferry\nmusic/01_Ada_Kaleh_-_Night_Ferry.mp3\n"
            "#EXTINF:300,Soha - Rivet\nmusic/Soha - Rivet.wav\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_m3u_can_be_the_first_library(self):
        report = watchfolder.import_m3u(self.store, path=self.playlist)
        self.assertEqual(report["playlist"], "Friday")
        self.assertEqual(report["added"], 2)
        self.assertEqual(report["matched"], 0)
        library = self.store.library()
        self.assertEqual(library["track_count"], 2)
        self.assertEqual(library["source_kind"], "files")
        self.assertEqual(library["playlists"][0]["name"], "Friday")
        self.assertEqual(library["playlists"][0]["count"], 2)
        titles = sorted(record["title"] for record in library["tracks"].values())
        self.assertEqual(titles, ["Night Ferry", "Rivet"])

    def test_re_import_updates_the_playlist_without_duplicating_tracks(self):
        watchfolder.import_m3u(self.store, path=self.playlist)
        again = watchfolder.import_m3u(self.store, path=self.playlist)
        self.assertEqual(again["added"], 0)
        self.assertEqual(again["matched"], 2)
        library = self.store.library()
        self.assertEqual(library["track_count"], 2)
        self.assertEqual(len(library["playlists"]), 1)
        self.assertEqual(library["playlists"][0]["count"], 2)

    def test_an_existing_library_row_is_reused(self):
        watchfolder.import_m3u(self.store, path=self.playlist)
        library = self.store.library()
        before = set(library["tracks"])
        # A second playlist naming one of the same files must point at the
        # track already in the library, not add a second row for it.
        other = self.dir / "one.m3u"
        other.write_text("music/Soha - Rivet.wav\n", encoding="utf-8")
        report = watchfolder.import_m3u(self.store, path=other)
        self.assertEqual(report["added"], 0)
        self.assertEqual(report["matched"], 1)
        self.assertEqual(set(self.store.library()["tracks"]), before)

    def test_missing_files_are_reported_not_added(self):
        broken = self.dir / "broken.m3u"
        broken.write_text("music/nope.mp3\n", encoding="utf-8")
        report = watchfolder.import_m3u(self.store, path=broken)
        self.assertEqual(report["added"], 0)
        self.assertEqual(len(report["missing"]), 1)
        self.assertIn("nope.mp3", report["missing"][0]["path"])

    def test_a_missing_playlist_is_a_message(self):
        with self.assertRaises(watchfolder.PlaylistError):
            watchfolder.import_m3u(self.store, path=self.dir / "nope.m3u")

    def test_dropped_text_is_accepted_with_a_base_folder(self):
        report = watchfolder.import_m3u(
            self.store,
            text="music/Soha - Rivet.wav\n",
            name="Dropped",
            base_dir=self.dir,
        )
        self.assertEqual(report["playlist"], "Dropped")
        self.assertEqual(report["added"], 1)


class WatchFolderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir / "data")
        self.folder = self.dir / "watch"
        touch(self.folder / "01_Ada_Kaleh_-_Night_Ferry.mp3")
        touch(self.folder / "sub" / "Soha - Rivet.wav")
        touch(self.folder / "cover.jpg")

    def tearDown(self):
        self.tmp.cleanup()

    def configure(self, **extra):
        payload = {"path": str(self.folder), "enabled": True, "recursive": True}
        payload.update(extra)
        return watchfolder.configure(self.store, payload)

    def test_a_scan_imports_every_audio_file_once(self):
        self.configure()
        first = watchfolder.scan(self.store)
        self.assertEqual(first["scanned"], 2)
        self.assertEqual(first["added"], 2)
        self.assertEqual(first["playlist"], "Watch: watch")
        library = self.store.library()
        self.assertEqual(library["track_count"], 2)
        self.assertEqual(library["playlists"][0]["count"], 2)

        second = watchfolder.scan(self.store)
        self.assertEqual(second["added"], 0)
        self.assertEqual(self.store.library()["track_count"], 2)

    def test_a_new_file_is_picked_up_on_the_next_pass(self):
        self.configure()
        watchfolder.scan(self.store)
        touch(self.folder / "New Track.mp3")
        second = watchfolder.scan(self.store)
        self.assertEqual(second["added"], 1)
        self.assertEqual(second["added_tracks"][0]["label"], "New Track")
        self.assertEqual(self.store.library()["playlists"][0]["count"], 3)

    def test_non_recursive_skips_subfolders(self):
        self.configure(recursive=False)
        report = watchfolder.scan(self.store)
        self.assertEqual(report["scanned"], 1)
        self.assertEqual(report["added"], 1)

    def test_a_folder_that_vanishes_is_a_report_not_a_crash(self):
        self.configure()
        self.folder.rename(self.dir / "gone")
        report = watchfolder.scan(self.store)
        self.assertEqual(report["added"], 0)
        self.assertIn("No folder", report["note"])

    def test_switching_folders_forgets_what_the_old_one_saw(self):
        self.configure()
        other = self.dir / "other"
        touch(other / "Late Night.mp3")
        watchfolder.configure(self.store, {"path": str(other), "enabled": True})
        report = watchfolder.scan(self.store)
        self.assertEqual(report["added"], 1)

    def test_a_folder_that_is_not_there_is_refused(self):
        with self.assertRaises(watchfolder.PlaylistError):
            watchfolder.configure(self.store, {"path": str(self.dir / "nope"), "enabled": True})

    def test_a_symlinked_path_is_the_same_file(self):
        # /tmp is a symlink to /private/tmp on macOS; an import by one name must
        # not become a second row when the folder is scanned by the other.
        self.assertEqual(
            watchfolder._normal(str(self.folder)),
            watchfolder._normal(str(self.folder.resolve())),
        )

    def test_status_reports_the_settings(self):
        self.configure(interval=30)
        watchfolder.scan(self.store)
        status = watchfolder.status(self.store)
        self.assertTrue(status["enabled"])
        self.assertTrue(status["exists"])
        self.assertEqual(status["interval"], 30.0)
        self.assertEqual(status["seen"], 2)
        self.assertEqual(status["last_added"], 2)
