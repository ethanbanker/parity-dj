import unittest

from parity import poolfilter


def view(key, path, **extra):
    data = {
        "key": key,
        "label": f"Track {key}",
        "title": f"Track {key}",
        "path": path,
        "location": "file://localhost" + path if path else "",
        "folder": path.rsplit("/", 1)[0] if path else "",
        "streaming": False,
    }
    data.update(extra)
    return data


SSD = "/Volumes/EXT SSD/RekordBox Library/Syd Bday Playlist"

VIEWS = [
    view("local-1", f"{SSD}/one.wav"),
    view("local-2", f"{SSD}/two.mp3"),
    view("local-3", "/Volumes/nas/DJ Library/other/three.aiff"),
    view("cloud-1", "", streaming=True, location="file://localhostsoundcloud:tracks:1"),
    view("cloud-2", "", streaming=True, location="https://beatport.com/track/x"),
    view("gone-1", f"{SSD}/missing.wav"),
]

EXISTS = {
    f"{SSD}/one.wav",
    f"{SSD}/two.mp3",
    "/Volumes/nas/DJ Library/other/three.aiff",
}


def present(path):
    return path in EXISTS


class RemoteTest(unittest.TestCase):
    def test_http_and_service_locations_are_remote(self):
        for location in (
            "file://localhostsoundcloud:tracks:1",
            "https://beatport.com/track/x",
            "spotify:track:abc",
            "tidal:123",
        ):
            self.assertTrue(poolfilter.is_remote(location), location)

    def test_local_locations_are_not_remote(self):
        for location in ("file://localhost/Volumes/x/a.wav", "/Volumes/x/a.wav", ""):
            self.assertFalse(poolfilter.is_remote(location), location)


class FolderTest(unittest.TestCase):
    def test_prefix_match(self):
        self.assertTrue(poolfilter.in_folder(f"{SSD}/a.wav", SSD))
        self.assertTrue(poolfilter.in_folder(f"{SSD}/sub/a.wav", SSD))
        self.assertFalse(poolfilter.in_folder(f"{SSD}/sub/a.wav", SSD, include_subfolders=False))
        self.assertFalse(poolfilter.in_folder("/Volumes/other/a.wav", SSD))

    def test_a_sibling_folder_with_the_same_prefix_is_not_inside(self):
        self.assertFalse(poolfilter.in_folder(f"{SSD} Extra/a.wav", SSD))

    def test_no_folder_keeps_everything(self):
        self.assertTrue(poolfilter.in_folder("/anything", ""))

    def test_volume_warning_names_the_right_volume(self):
        self.assertEqual(poolfilter.volume_warning(""), "")
        self.assertEqual(poolfilter.volume_warning("/Users/me/Music"), "")
        self.assertEqual(poolfilter.volume_warning("/Volumes"), "")

        warning = poolfilter.volume_warning("/Volumes/definitely-not-mounted/crates/a.wav")
        self.assertIn("/Volumes/definitely-not-mounted", warning)
        self.assertNotIn("/Volumes/Volumes", warning)
        self.assertIn("not mounted", warning)


class ApplyFilterTest(unittest.TestCase):
    """``missing_file`` is patched because these paths are not on the test machine."""

    def setUp(self):
        self._original = poolfilter.missing_file
        poolfilter.missing_file = lambda path, exists=None: not present(path)
        self.addCleanup(self._restore)

    def _restore(self):
        poolfilter.missing_file = self._original

    def test_streaming_is_dropped_with_a_reason(self):
        result = poolfilter.apply_filter(
            VIEWS, poolfilter.FilterConfig(require_file=False), None
        )
        kept = {item["key"] for item in result.kept}
        self.assertNotIn("cloud-1", kept)
        self.assertNotIn("cloud-2", kept)
        reasons = result.reasons
        self.assertEqual(reasons.get("streaming"), 2)
        examples = {item.key: item for item in result.removed}
        self.assertEqual(examples["cloud-2"].reason, "streaming")
        self.assertIn("beatport", examples["cloud-2"].detail)
        self.assertEqual(examples["cloud-1"].detail, "file://localhostsoundcloud:tracks:1")

    def test_missing_files_are_dropped(self):
        result = poolfilter.apply_filter(VIEWS, poolfilter.FilterConfig(), None)
        kept = {item["key"] for item in result.kept}
        self.assertEqual(kept, {"local-1", "local-2", "local-3"})
        self.assertEqual(result.reasons.get("missing_file"), 1)

    def test_folder_selection(self):
        result = poolfilter.apply_filter(VIEWS, poolfilter.FilterConfig(folder=SSD), None)
        self.assertEqual({item["key"] for item in result.kept}, {"local-1", "local-2"})

    def test_playlist_selection(self):
        result = poolfilter.apply_filter(
            VIEWS,
            poolfilter.FilterConfig(require_file=False, local_only=False),
            playlist_keys={"local-2", "local-3"},
        )
        self.assertEqual({item["key"] for item in result.kept}, {"local-2", "local-3"})

    def test_explicit_keys_win(self):
        result = poolfilter.apply_filter(
            VIEWS,
            poolfilter.FilterConfig(keys=["local-1"], require_file=False, local_only=False),
            playlist_keys={"local-2"},
        )
        self.assertEqual([item["key"] for item in result.kept], ["local-1"])

    def test_duplicate_paths_are_dropped(self):
        rows = [view("a", "/Volumes/x/same.wav"), view("b", "/Volumes/x/same.wav")]
        result = poolfilter.apply_filter(
            rows, poolfilter.FilterConfig(require_file=False), None
        )
        self.assertEqual([item["key"] for item in result.kept], ["a"])
        self.assertEqual(result.reasons.get("duplicate"), 1)

    def test_report_counts_everything(self):
        result = poolfilter.apply_filter(
            VIEWS, poolfilter.FilterConfig(require_file=False), None
        )
        report = result.report()
        self.assertEqual(report["scanned"], len(VIEWS))
        self.assertEqual(report["kept"] + report["removed"], len(VIEWS))
        self.assertEqual(report["reasons"]["streaming"], 2)


class FolderOptionsTest(unittest.TestCase):
    def test_counts_local_and_streaming_by_folder(self):
        options = poolfilter.folder_options(VIEWS)
        by_folder = {entry["folder"]: entry for entry in options}
        self.assertEqual(by_folder[SSD]["local"], 3)  # one, two, and the missing one
        self.assertEqual(by_folder[SSD]["total"], 3)
        self.assertEqual(by_folder[SSD]["streaming"], 0)

    def test_tracks_without_a_folder_are_ignored(self):
        self.assertTrue(all(entry["folder"] for entry in poolfilter.folder_options(VIEWS)))


class ConfigRoundTripTest(unittest.TestCase):
    def test_to_and_from_dict(self):
        config = poolfilter.FilterConfig(folder="/x", local_only=False, keys=["a", "b"])
        again = poolfilter.FilterConfig.from_dict(config.to_dict())
        self.assertEqual(again.folder, "/x")
        self.assertFalse(again.local_only)
        self.assertEqual(again.keys, ["a", "b"])

    def test_unknown_fields_are_ignored(self):
        config = poolfilter.FilterConfig.from_dict({"folder": "/y", "nonsense": 1})
        self.assertEqual(config.folder, "/y")
        self.assertTrue(config.local_only)


if __name__ == "__main__":
    unittest.main()
