import unittest
from pathlib import Path

from parity import camelot, rekordbox

FIXTURE = Path(__file__).parent / "fixtures" / "collection.xml"


def load():
    return rekordbox.parse_collection(FIXTURE.read_text(encoding="utf-8"), source=str(FIXTURE))


#: The shortest "billion laughs": three entity definitions that expand to nine
#: lines of text each time they are referenced, nested.
ENTITY_XML = """<?xml version="1.0"?>
<!DOCTYPE DJ_PLAYLISTS [
  <!ENTITY a "aaaaaaaaaa">
  <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
  <!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">
]>
<DJ_PLAYLISTS Version="1.0.0"><COLLECTION Entries="0">&c;</COLLECTION></DJ_PLAYLISTS>
"""


class XmlGuardTest(unittest.TestCase):
    """An import refuses a document that brings its own entity declarations."""

    def test_a_document_with_entities_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            rekordbox.parse_collection(ENTITY_XML, source="hostile.xml")
        # The message is the one the API hands back as a 400, so it has to read
        # as advice rather than as a traceback.
        self.assertEqual(str(caught.exception), rekordbox.XML_REFUSED)
        self.assertIn("entities", str(caught.exception))

    def test_a_document_with_a_doctype_is_refused(self):
        with self.assertRaises(ValueError):
            rekordbox.parse_collection('<!DOCTYPE x [ ]><DJ_PLAYLISTS/>')

    def test_a_normal_export_still_parses(self):
        self.assertEqual(len(load().tracks), 12)

    def test_an_entity_declaration_needs_no_doctype_to_be_refused(self):
        # The check is a scan of the text the parser is about to be handed, not
        # a look at its first characters.
        text = '<!ENTITY x "y">' + FIXTURE.read_text(encoding="utf-8")
        with self.assertRaises(ValueError):
            rekordbox.parse_collection(text)


class ParserTest(unittest.TestCase):
    def setUp(self):
        self.library = load()

    def test_root_node_is_flattened(self):
        self.assertEqual(
            [node.name for node in self.library.playlists],
            ["Friday Set", "Peak material", "Folders"],
        )

    def test_collection_is_read(self):
        self.assertEqual(len(self.library.tracks), 12)
        self.assertEqual(self.library.product.get("Name"), "rekordbox")

    def test_track_fields(self):
        track = self.library.tracks["101"]
        self.assertEqual(track.title, "Deep Intro")
        self.assertEqual(track.artist, "Marlow")
        self.assertEqual(track.genre, "Deep House")
        self.assertEqual(track.bpm, 122.0)
        self.assertEqual(track.duration, 372.0)
        self.assertEqual(track.stars, 3.0)
        self.assertEqual(camelot.parse_key(track.tonality), "8A")
        self.assertEqual(track.path, "/Users/dj/Music/forge/deep intro.mp3")

    def test_location_decoding(self):
        self.assertEqual(
            rekordbox.location_to_path("file://localhost/Users/dj/Music/a%20b.mp3"),
            "/Users/dj/Music/a b.mp3",
        )
        self.assertEqual(rekordbox.location_to_path(""), "")
        self.assertEqual(rekordbox.location_to_path("/plain/path.mp3"), "/plain/path.mp3")

    def test_tempo_fallback_and_missing_fields(self):
        track = self.library.tracks["112"]  # no tonality, rating 0, ambient
        self.assertEqual(track.tonality, "")
        self.assertEqual(track.rating, 0)
        self.assertIsNone(camelot.parse_key(track.tonality))

    def test_track_key_is_stable_across_ids(self):
        first = load().tracks["101"]
        self.assertEqual(first.key, self.library.tracks["101"].key)
        self.assertNotEqual(first.key, self.library.tracks["102"].key)
        self.assertEqual(len(first.key), 16)


class PlaylistTest(unittest.TestCase):
    def setUp(self):
        self.library = load()

    def find(self, name):
        stack = list(self.library.playlists)
        while stack:
            node = stack.pop()
            if node.name == name:
                return node
            stack.extend(node.children)
        return None

    def test_flat_playlist(self):
        playlist = self.find("Friday Set")
        self.assertIsNotNone(playlist)
        self.assertEqual(len(playlist.track_ids), 8)
        self.assertEqual(playlist.track_ids[:3], ["101", "102", "103"])

    def test_folder_and_nested_playlists(self):
        folder = self.find("Folders")
        self.assertTrue(folder.is_folder)
        self.assertEqual(folder.total_tracks(), 5)
        self.assertEqual([child.name for child in folder.children], ["Warm up", "Closers"])

    def test_playlist_lookup_by_id(self):
        playlist = self.find("Peak material")
        ids = self.library.playlist_track_ids(playlist.playlist_id)
        self.assertEqual(ids, ["105", "106", "110", "111"])
        keys = self.library.track_keys(playlist.playlist_id)
        self.assertEqual(len(keys), 4)
        self.assertEqual(len(set(keys)), 4)

    def test_unknown_playlist(self):
        self.assertEqual(self.library.playlist_track_ids("pl:nope"), [])
        self.assertIsNone(self.library.playlist("pl:nope"))

    def test_playlist_ids_are_stable_across_imports(self):
        playlist_id = self.find("Friday Set").playlist_id
        other = load()
        self.assertEqual(playlist_id, other.playlist(playlist_id).playlist_id)
        self.assertEqual(other.playlist(playlist_id).name, "Friday Set")


class StreamingTest(unittest.TestCase):
    """rekordbox collection exports include streaming entries with no file."""

    def track(self, location):
        return rekordbox.Track(track_id="1", location=location)

    def test_local_files_are_not_streaming(self):
        for location in (
            "file://localhost/Users/dj/Music/a%20b.mp3",
            "file:///Volumes/nas/DJ%20Library/track.aiff",
            "/plain/path.mp3",
            "C:\\\\Music\\\\track.mp3",
        ):
            self.assertFalse(self.track(location).is_streaming, location)

    def test_soundcloud_entries_are_streaming(self):
        track = self.track("file://localhostsoundcloud:tracks:862894612")
        self.assertEqual(track.path, "")
        self.assertTrue(track.is_streaming)

    def test_other_services_are_streaming(self):
        for location in ("http://example.com/a.mp3", "soundcloud:tracks:1", "spotify:track:abc"):
            self.assertTrue(self.track(location).is_streaming, location)

    def test_no_location_is_not_streaming(self):
        self.assertFalse(self.track("").is_streaming)
        self.assertFalse(self.track(None).is_streaming)

    def test_to_dict_reports_streaming(self):
        cloud = self.track("file://localhostsoundcloud:tracks:1").to_dict()
        local = self.track("file://localhost/Users/dj/Music/a.mp3").to_dict()
        self.assertTrue(cloud["streaming"])
        self.assertEqual(cloud["location"], "file://localhostsoundcloud:tracks:1")
        self.assertFalse(local["streaming"])


class BadInputTest(unittest.TestCase):
    def test_non_xml_raises(self):
        with self.assertRaises(Exception):
            rekordbox.parse_collection("not xml at all")

    def test_empty_collection(self):
        library = rekordbox.parse_collection(
            '<?xml version="1.0"?><DJ_PLAYLISTS Version="1.0.0"><COLLECTION Entries="0"/></DJ_PLAYLISTS>'
        )
        self.assertEqual(library.tracks, {})
        self.assertEqual(library.playlists, [])

    def test_bom_is_tolerated(self):
        text = "\ufeff" + FIXTURE.read_text(encoding="utf-8")
        self.assertEqual(len(rekordbox.parse_collection(text).tracks), 12)


if __name__ == "__main__":
    unittest.main()
