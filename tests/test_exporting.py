import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from parity import exporting, rekordbox

FIXTURE = Path(__file__).parent / "fixtures" / "collection.xml"
LIBRARY_XML = FIXTURE.read_text(encoding="utf-8")

TRACKS = [
    {
        "id": "101", "title": "Deep Intro", "artist": "Marlow", "bpm": 122.0, "camelot": "8A",
        "energy": 4.0, "genre": "Deep House", "duration": 372.0, "path": "/Users/dj/Music/forge/deep intro.mp3",
    },
    {
        "id": "105", "title": "Rivet", "artist": "Soha", "bpm": 132.0, "camelot": "9A",
        "energy": 8.0, "genre": "Techno", "duration": 402.0, "path": "/Users/dj/Music/forge/rivet.mp3",
    },
    {
        "id": "110", "title": "Peak Hour", "artist": "Bosko", "bpm": 138.0, "camelot": "4A",
        "energy": 9.0, "genre": "Peak Time Techno", "duration": 344.0, "path": "/Users/dj/Music/forge/peak hour.mp3",
    },
]


class RekordboxXmlTest(unittest.TestCase):
    def test_export_is_valid_and_ordered(self):
        data = exporting.set_to_rekordbox_xml(LIBRARY_XML, ["110", "101", "105"], set_name="Friday Peak")
        root = ET.fromstring(data)
        self.assertEqual(root.tag, "DJ_PLAYLISTS")

        collection = root.find("COLLECTION")
        self.assertEqual(collection.attrib["Entries"], "3")
        exported_ids = [element.attrib["TrackID"] for element in collection.findall("TRACK")]
        self.assertEqual(exported_ids, ["110", "101", "105"])

        node = root.find("PLAYLISTS").find("NODE").find("NODE")
        self.assertEqual(node.attrib["Name"], "Friday Peak")
        self.assertEqual([child.attrib["Key"] for child in node.findall("TRACK")], ["110", "101", "105"])

    def test_original_track_elements_survive_intact(self):
        data = exporting.set_to_rekordbox_xml(LIBRARY_XML, ["101"])
        track = ET.fromstring(data).find("COLLECTION").find("TRACK")
        self.assertEqual(track.attrib["Comments"], "E4 warm")
        self.assertEqual(track.attrib["Tonality"], "Am")
        self.assertEqual(track.attrib["AverageBpm"], "122.00")
        # Cue points and tempo markers are preserved, they matter for CDJs.
        self.assertIsNotNone(track.find("POSITION_MARK"))
        self.assertIsNotNone(track.find("TEMPO"))

    def test_export_round_trips_through_the_parser(self):
        data = exporting.set_to_rekordbox_xml(LIBRARY_XML, ["105", "101"])
        library = rekordbox.parse_collection(data)
        self.assertEqual(set(library.tracks), {"105", "101"})
        playlist = library.playlists[0]
        self.assertEqual(playlist.name, "Parity Set")
        self.assertEqual([library.tracks[tid].title for tid in playlist.track_ids], ["Rivet", "Deep Intro"])

    def test_fallback_when_library_xml_is_missing(self):
        fallback = {t["id"]: t for t in TRACKS}
        data = exporting.set_to_rekordbox_xml(None, [t["id"] for t in TRACKS], fallback_tracks=fallback)
        library = rekordbox.parse_collection(data)
        self.assertEqual(len(library.tracks), 3)
        self.assertEqual(library.tracks["105"].tonality, "")
        self.assertEqual(library.tracks["105"].bpm, 132.0)
        self.assertEqual(library.tracks["105"].path, "/Users/dj/Music/forge/rivet.mp3")

    def test_unknown_ids_are_dropped(self):
        data = exporting.set_to_rekordbox_xml(LIBRARY_XML, ["101", "does-not-exist"])
        self.assertEqual(ET.fromstring(data).find("COLLECTION").attrib["Entries"], "1")


def cue_plan(hot=4, memory=True):
    """A cue plan in the shape :meth:`parity.server.Api.export_cue_plans` emits."""
    slots = [
        ("A", 0, "First Downbeat", "first_downbeat", "red", (230, 0, 0), 461),
        ("B", 1, "Build Up", "build_up", "yellow", (230, 210, 0), 16615),
        ("C", 2, "Main Drop", "drop", "green", (48, 210, 110), 46153),
        ("D", 3, "Mix-Out", "mix_out", "blue", (40, 120, 230), 75692),
    ][:hot]
    plan = {
        "hot_cues": [
            {
                "slot": slot, "num": num, "name": name, "role": role, "colour": colour,
                "rgb": list(rgb), "ms": ms, "confidence": "high", "reason": "test fixture",
            }
            for slot, num, name, role, colour, rgb, ms in slots
        ],
        "structure": {"bpm": 130.0, "first_beat_ms": 461, "source": "rekordbox-anlz"},
        "beat_grid": {"inizio": 0.461, "bpm": 130.0, "metro": "4/4", "battito": 1},
    }
    if memory:
        plan["memory_cues"] = [{"name": "Chorus 4", "ms": 60923}]
    return plan


class CueInjectionTest(unittest.TestCase):
    def export(self, plans, ids=None, library_xml=LIBRARY_XML):
        fallback = {t["id"]: t for t in TRACKS}
        return exporting.set_to_rekordbox_xml(
            library_xml, ids or ["101"], fallback_tracks=fallback, cue_plans=plans
        )

    def test_hot_cues_become_position_marks(self):
        data = self.export({"101": cue_plan()})
        track = ET.fromstring(data).find("COLLECTION").find("TRACK")
        marks = track.findall("POSITION_MARK")
        hot = [m for m in marks if int(m.attrib["Num"]) >= 0]
        self.assertEqual([int(m.attrib["Num"]) for m in hot], [0, 1, 2, 3])
        self.assertEqual(hot[0].attrib["Start"], "0.461")
        self.assertEqual(hot[2].attrib["Start"], "46.153")
        self.assertEqual(hot[0].attrib["Type"], "0")

    def test_colours_are_written_per_the_spec(self):
        data = self.export({"101": cue_plan()})
        track = ET.fromstring(data).find("COLLECTION").find("TRACK")
        hot = {int(m.attrib["Num"]): m for m in track.findall("POSITION_MARK") if int(m.attrib["Num"]) >= 0}
        self.assertEqual((hot[0].attrib["Red"], hot[0].attrib["Green"], hot[0].attrib["Blue"]), ("230", "0", "0"))
        self.assertEqual((hot[1].attrib["Red"], hot[1].attrib["Green"], hot[1].attrib["Blue"]), ("230", "210", "0"))
        self.assertEqual((hot[2].attrib["Red"], hot[2].attrib["Green"], hot[2].attrib["Blue"]), ("48", "210", "110"))
        self.assertEqual((hot[3].attrib["Red"], hot[3].attrib["Green"], hot[3].attrib["Blue"]), ("40", "120", "230"))

    def test_names_match_the_cue_roles(self):
        data = self.export({"101": cue_plan()})
        track = ET.fromstring(data).find("COLLECTION").find("TRACK")
        names = [m.attrib["Name"] for m in track.findall("POSITION_MARK") if int(m.attrib["Num"]) >= 0]
        self.assertEqual(names, ["First Downbeat", "Build Up", "Main Drop", "Mix-Out"])

    def test_memory_cues_use_num_minus_one(self):
        data = self.export({"101": cue_plan()})
        track = ET.fromstring(data).find("COLLECTION").find("TRACK")
        memory = [m for m in track.findall("POSITION_MARK") if m.attrib["Num"] == "-1"]
        self.assertEqual(len(memory), 1)
        self.assertEqual(memory[0].attrib["Start"], "60.923")
        self.assertEqual(memory[0].attrib["Name"], "Chorus 4")
        self.assertNotIn("Red", memory[0].attrib)

    def test_a_beat_grid_is_written(self):
        data = self.export({"101": cue_plan()})
        track = ET.fromstring(data).find("COLLECTION").find("TRACK")
        tempo = track.findall("TEMPO")
        self.assertEqual(len(tempo), 1)  # replaced, never duplicated
        self.assertEqual(tempo[0].attrib["Bpm"], "130.00")
        self.assertEqual(tempo[0].attrib["Inizio"], "0.461")
        self.assertEqual(tempo[0].attrib["Metro"], "4/4")

    def test_cues_rekordbox_already_had_survive(self):
        plan = cue_plan()
        plan["existing"] = [{"num": 6, "name": "mine", "start": 12.5}]
        data = self.export({"101": plan})
        track = ET.fromstring(data).find("COLLECTION").find("TRACK")
        nums = sorted(int(m.attrib["Num"]) for m in track.findall("POSITION_MARK"))
        self.assertIn(6, nums)
        kept = [m for m in track.findall("POSITION_MARK") if m.attrib["Num"] == "6"][0]
        self.assertEqual(kept.attrib["Start"], "12.500")

    def test_an_injected_cue_replaces_the_same_slot(self):
        plan = cue_plan()
        plan["existing"] = [{"num": 0, "name": "old A", "start": 99.0}]
        data = self.export({"101": plan})
        track = ET.fromstring(data).find("COLLECTION").find("TRACK")
        a_slot = [m for m in track.findall("POSITION_MARK") if m.attrib["Num"] == "0"]
        self.assertEqual(len(a_slot), 1)
        self.assertEqual(a_slot[0].attrib["Name"], "First Downbeat")

    def test_cues_land_on_synthesised_tracks_too(self):
        data = self.export({"105": cue_plan() if False else cue_plan(2, memory=False)}, ids=["105"], library_xml=None)
        library = rekordbox.parse_collection(data)
        self.assertEqual(library.tracks["105"].bpm, 132.0)
        self.assertEqual(library.tracks["105"].path, "/Users/dj/Music/forge/rivet.mp3")
        track = ET.fromstring(data).find("COLLECTION").find("TRACK")
        self.assertEqual(len(track.findall("POSITION_MARK")), 2)

    def test_no_plans_means_no_cues(self):
        data = self.export({})
        track = ET.fromstring(data).find("COLLECTION").find("TRACK")
        self.assertIsNotNone(track.find("POSITION_MARK"))  # from the fixture, untouched

    def test_export_still_round_trips(self):
        data = self.export({"101": cue_plan()}, ids=["101", "105"])
        library = rekordbox.parse_collection(data)
        self.assertEqual([library.tracks[tid].title for tid in library.playlists[0].track_ids], ["Deep Intro", "Rivet"])


class AutopilotManifestTest(unittest.TestCase):
    def manifest(self):
        return exporting.autopilot_manifest(
            TRACKS,
            "Syd Bday Ultimate",
            transitions=[{"index": 1, "label": "adjacent"}],
            stats={"count": 3},
            source="/Users/me/master.db",
            xml_filename="Syd-Bday-Ultimate.xml",
            cue_plans={"101": cue_plan()},
        )

    def test_manifest_shape(self):
        import json

        data = json.loads(self.manifest())
        self.assertEqual(data["manifest"], "parity.autopilot/1")
        self.assertEqual(data["import_file"], "Syd-Bday-Ultimate.xml")
        self.assertEqual(data["track_count"], 3)
        self.assertEqual(data["stats"]["count"], 3)
        self.assertEqual(len(data["transitions"]), 1)
        self.assertIn("AUTO/PILOT", data["note"])

    def test_cue_timestamps_are_in_seconds(self):
        import json

        data = json.loads(self.manifest())
        first = data["tracks"][0]
        self.assertEqual(first["position"], 1)
        self.assertEqual(first["camelot"], "8A")
        self.assertEqual([c["seconds"] for c in first["hot_cues"]], [0.461, 16.615, 46.153, 75.692])
        self.assertEqual(first["hot_cues"][2]["colour"], "green")
        self.assertEqual([c["seconds"] for c in first["memory_cues"]], [60.923])
        self.assertEqual(data["tracks"][1]["hot_cues"], [])

    def test_bundle_contains_both_import_targets(self):
        import io
        import json
        import zipfile

        xml = exporting.set_to_rekordbox_xml(LIBRARY_XML, ["101"], fallback_tracks={})
        bundle = exporting.set_to_bundle(
            "Syd-Bday-Ultimate",
            stems={
                "Syd-Bday-Ultimate.xml": xml,
                "Syd-Bday-Ultimate.txt": exporting.set_to_txt(TRACKS),
            },
            manifest_bytes=self.manifest(),
        )
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            names = set(archive.namelist())
            self.assertIn("Syd-Bday-Ultimate.xml", names)
            self.assertIn("Syd-Bday-Ultimate.autopilot.json", names)
            self.assertIn("Syd-Bday-Ultimate.txt", names)
            reopened = rekordbox.parse_collection(archive.read("Syd-Bday-Ultimate.xml"))
            self.assertIn("101", reopened.tracks)
            payload = json.loads(archive.read("Syd-Bday-Ultimate.autopilot.json"))
            self.assertEqual(payload["set_name"], "Syd Bday Ultimate")


class TagCommentTest(unittest.TestCase):
    """The tag goes into the XML's ``Comments`` -- the field rekordbox reads."""

    def export(self, tags, ids=("101",), library_xml=LIBRARY_XML):
        fallback = {t["id"]: t for t in TRACKS}
        return exporting.set_to_rekordbox_xml(
            library_xml, list(ids), fallback_tracks=fallback, tags=tags
        )

    def comment(self, data, track_id="101"):
        collection = ET.fromstring(data).find("COLLECTION")
        for element in collection.findall("TRACK"):
            if element.attrib["TrackID"] == track_id:
                return element.attrib.get("Comments", "")
        raise AssertionError(f"no track {track_id} in the export")

    def test_the_tag_is_appended_to_the_comment_the_track_had(self):
        data = self.export({"101": "[E4] [opener] 1/1"})
        self.assertEqual(self.comment(data), "E4 warm [E4] [opener] 1/1")

    def test_exporting_without_tags_leaves_the_comment_alone(self):
        self.assertEqual(self.comment(self.export({})), "E4 warm")
        self.assertEqual(self.comment(self.export({"999": "[E7] [peak] 1/1"})), "E4 warm")

    def test_re_exporting_replaces_our_tag_instead_of_stacking_it(self):
        first = self.export({"101": "[E4] [opener] 1/1"}).decode("utf-8")
        again = exporting.set_to_rekordbox_xml(first, ["101"], tags={"101": "[E9] [peak] 9/9"})
        self.assertEqual(self.comment(again), "E4 warm [E9] [peak] 9/9")

    def test_a_synthesised_track_gets_the_tag_as_its_whole_comment(self):
        data = self.export({"105": "[E9] [peak] 2/3"}, ids=["105"], library_xml=None)
        self.assertEqual(self.comment(data, "105"), "[E9] [peak] 2/3")
        self.assertEqual(rekordbox.parse_collection(data).tracks["105"].comments, "[E9] [peak] 2/3")


class CueLogTest(unittest.TestCase):
    """The cue log: where every cue sits, and why it landed there."""

    def log(self, plans=None, tags=None):
        return exporting.cue_log(
            TRACKS, plans if plans is not None else {"101": cue_plan()}, "Friday Peak", tags or {}
        ).decode("utf-8")

    def test_the_header_counts_the_set(self):
        text = self.log()
        self.assertIn("Friday Peak - cue log", text)
        self.assertIn("3 tracks, 4 hot cues, 1 memory cue", text)

    def test_every_hot_cue_is_named_with_its_time_colour_and_reason(self):
        text = self.log()
        self.assertIn("First Downbeat", text)
        self.assertIn("0:00.461", text)
        self.assertIn("green", text)
        self.assertIn("0:46.153", text)
        self.assertIn("test fixture", text)

    def test_memory_cues_are_marked_as_such(self):
        self.assertIn("mem", self.log())
        self.assertIn("Chorus 4", self.log())

    def test_the_set_tag_travels_with_the_track(self):
        self.assertIn("[E4] [opener] 1/3", self.log(tags={"101": "[E4] [opener] 1/3"}))

    def test_a_track_with_no_plan_says_so(self):
        # TRACKS[1] ("105") has no plan in this log.
        self.assertIn("cues: none generated yet", self.log())

    def test_a_locked_cue_says_it_was_moved_by_hand(self):
        plan = cue_plan()
        plan["hot_cues"][0]["locked"] = True
        self.assertIn("locked: moved by hand", self.log({"101": plan}))


class PackageReadmeTest(unittest.TestCase):
    FILES = [
        ("friday.xml", "the set as rekordbox XML"),
        ("friday.txt", "the set list"),
    ]

    def test_the_note_lists_every_file_it_is_given(self):
        text = exporting.package_readme(
            "Friday Peak", self.FILES, source="/Users/me/master.db", generated="2026-01-01T00:00:00"
        ).decode("utf-8")
        self.assertIn("Friday Peak", text)
        for name, description in self.FILES:
            self.assertIn(name, text)
            self.assertIn(description, text)
        self.assertIn("/Users/me/master.db", text)
        self.assertIn("2026-01-01T00:00:00", text)
        self.assertIn("Import Collection in xml format", text)

    def test_a_clean_check_is_reported_as_clean(self):
        text = exporting.package_readme(
            "Friday Peak", self.FILES, preflight={"issues": [], "checked": 12}
        ).decode("utf-8")
        self.assertIn("all clear", text)
        self.assertIn("12 tracks checked", text)

    def test_a_check_that_found_something_says_so(self):
        text = exporting.package_readme(
            "Friday Peak",
            self.FILES,
            preflight={"issues": [{"kind": "missing_file"}], "counts": {"error": 2, "warning": 1}},
        ).decode("utf-8")
        self.assertIn("2 error(s) and 1 warning(s)", text)
        self.assertIn("pre-flight report", text)

    def test_no_check_is_stated_plainly(self):
        text = exporting.package_readme("Friday Peak", self.FILES).decode("utf-8")
        self.assertIn("Pre-flight: not run", text)


class OtherFormatsTest(unittest.TestCase):
    def test_m3u8(self):
        text = exporting.set_to_m3u8(TRACKS, "Friday Peak").decode("utf-8")
        lines = text.strip().splitlines()
        self.assertEqual(lines[0], "#EXTM3U")
        self.assertEqual(lines[1], "#PLAYLIST:Friday Peak")
        self.assertEqual(lines[2], "#EXTINF:372,Marlow - Deep Intro")
        self.assertEqual(lines[3], "/Users/dj/Music/forge/deep intro.mp3")

    def test_csv(self):
        text = exporting.set_to_csv(TRACKS).decode("utf-8")
        rows = text.strip().splitlines()
        self.assertEqual(rows[0], "position,title,artist,bpm,key,camelot,energy,genre,duration,path")
        self.assertTrue(rows[1].startswith("1,Deep Intro,Marlow,122.0,"))
        self.assertIn("8A", rows[1])
        self.assertEqual(len(rows), 4)

    def test_txt(self):
        text = exporting.set_to_txt(TRACKS, "Friday Peak").decode("utf-8")
        self.assertIn("Friday Peak", text)
        self.assertIn("1. Marlow - Deep Intro  [122 BPM | 8A | E4]", text)
        self.assertIn("3 tracks | 18 min", text)


if __name__ == "__main__":
    unittest.main()
