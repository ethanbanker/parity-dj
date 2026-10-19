import json
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
import wave
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from parity import audio, rekordbox, server, tags, waveform
from parity.server import Api, start_in_thread
from parity.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "collection.xml"
XML = FIXTURE.read_text(encoding="utf-8")


def find_playlist(playlists, name):
    for node in playlists:
        if node.get("name") == name:
            return node
        found = find_playlist(node.get("children") or [], name)
        if found:
            return found
    return None


class AudioEnergyTest(unittest.TestCase):
    """The audio energy endpoint, with the decoder stubbed out.

    The measurement itself is covered in ``tests/test_audio.py`` against real
    files; here the interest is the wiring -- caching, what the stored entry
    carries, and that a measured value takes over from the metadata guess.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.api = Api(self.store)
        self.state = self.api.import_xml(XML, "fixture")
        self.audio_file = Path(self.tmp.name) / "track.wav"
        self.audio_file.write_bytes(b"RIFF....WAVEfmt " + b"\x00" * 32)
        patch = mock.patch.object(Api, "audio_path", lambda api, key: self.audio_file)
        patch.start()
        self.addCleanup(patch.stop)
        self.features = audio.Features(
            seconds=180.0, loudness_dbfs=-5.4, centroid_hz=2800.0,
            dynamic_range_db=12.0, onset_rate=3.2, peak_dbfs=-1.4, engine="numpy",
        )
        self.features.energy = audio.score(-5.4, 2800.0, 12.0, 3.2)
        analyse = mock.patch.object(audio, "analyse", lambda path, seconds=None: self.features)
        analyse.start()
        self.addCleanup(analyse.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def keys(self, count: int = 3):
        return [track["key"] for track in self.api.tracks(None)["tracks"][:count]]

    def test_analysis_stores_a_value_that_takes_over_from_the_guess(self):
        report = self.api.analyse_audio({"keys": self.keys(), "local_only": False})
        self.assertEqual(report["analysed"], 3)
        self.assertEqual(report["skipped"], [])
        self.assertEqual(report["model"], audio.MODEL_VERSION)
        track = next(
            item for item in self.api.tracks(None)["tracks"] if item["key"] in self.keys()
        )
        self.assertEqual(track["energy_source"], "audio")
        self.assertEqual(track["energy"], self.features.energy)
        self.assertAlmostEqual(track["audio"]["centroid_hz"], 2800.0)
        self.assertIn("mtime", track["audio"])
        self.assertEqual(track["audio"]["engine"], "numpy")

    def test_a_second_run_uses_the_cache(self):
        keys = self.keys()
        self.api.analyse_audio({"keys": keys, "local_only": False})
        again = self.api.analyse_audio({"keys": keys, "local_only": False})
        self.assertEqual(again["analysed"], 0)
        self.assertEqual(again["cached"], 3)
        # Named, not only counted: the UI sends a big pool in chunks and has to
        # know which keys are finished so it can stop sending them.
        self.assertEqual(sorted(again["cached_keys"]), sorted(keys))

    def sweep(self, pool: list[str], limit: int = 2):
        """The UI's chunked loop: ask for a chunk, drop whatever it settled.

        A key comes back exactly one way -- measured, already cached, or
        unreadable -- and is dropped from the next request, so nothing is asked
        for or counted twice.
        """
        measured = cached = 0
        remaining = list(pool)
        for _ in range(20):
            answer = self.api.analyse_audio(
                {"keys": remaining, "local_only": False, "limit": limit}
            )
            settled = set(answer["cached_keys"])
            settled |= {row["key"] for row in answer["skipped"]}
            settled |= {row["key"] for row in answer["results"]}
            remaining = [key for key in remaining if key not in settled]
            measured += answer["analysed"]
            cached += answer["cached"]
            if not answer["analysed"] or not remaining:
                break
        return measured, cached, remaining

    def test_a_pool_asked_for_in_chunks_counts_each_track_once(self):
        """Re-sending the whole pool made the cached count grow with the chunks.

        Each chunk re-walked the keys the earlier ones had already cached, so a
        14-track pool came back "26 already cached" -- and the number grew again
        with every further chunk.  Dropping the settled keys is what fixes it,
        and this is that loop.
        """
        pool = self.keys()
        self.assertEqual(self.sweep(pool), (3, 0, []))
        # Pressed again with all three measured: three cached, counted once.
        self.assertEqual(self.sweep(pool), (0, 3, []))

    def test_force_re_measures(self):
        self.api.analyse_audio({"keys": self.keys(), "local_only": False})
        forced = self.api.analyse_audio({"keys": self.keys(), "local_only": False, "force": True})
        self.assertEqual(forced["analysed"], 3)

    def test_a_dry_run_reports_without_storing(self):
        report = self.api.analyse_audio({"keys": self.keys(), "local_only": False, "dry_run": True})
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["results"][0]["energy"], self.features.energy)
        self.assertEqual(self.store.audio_analysis(), {})
        track = self.api.tracks(None)["tracks"][0]
        self.assertNotEqual(track["energy_source"], "audio")

    def test_a_track_whose_file_is_gone_is_reported_not_raised(self):
        def missing(api, key):
            raise ValueError("That track is streaming only - there is no local file to preview.")

        with mock.patch.object(Api, "audio_path", missing):
            report = self.api.analyse_audio({"keys": self.keys(2), "local_only": False})
        self.assertEqual(report["analysed"], 0)
        self.assertEqual(len(report["skipped"]), 2)
        self.assertIn("streaming only", report["skipped"][0]["reason"])

    def test_an_older_model_is_recomputed_rather_than_trusted(self):
        self.store.set_audio_analysis(
            {self.keys(1)[0]: {"energy": 9.9, "model": audio.MODEL_VERSION - 1, "mtime": 1.0, "size": 1}}
        )
        track = next(
            item for item in self.api.tracks(None)["tracks"] if item["key"] == self.keys(1)[0]
        )
        self.assertNotEqual(track["energy_source"], "audio")
        report = self.api.analyse_audio({"keys": self.keys(1), "local_only": False})
        self.assertEqual(report["analysed"], 1)

    def test_status_and_clear(self):
        self.assertEqual(self.api.audio_status()["analysed"], 0)
        self.api.analyse_audio({"keys": self.keys(), "local_only": False})
        status = self.api.audio_status()
        self.assertEqual(status["analysed"], 3)
        # Three measured, but only two report "audio" as their source: the third
        # fixture track carries an explicit E-tag in its comments, and a value the
        # user typed outranks the measurement.
        self.assertEqual(status["using_audio"], 2)
        self.store.clear_audio_analysis()
        self.assertEqual(self.api.audio_status()["analysed"], 0)


class ApiFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.api = Api(self.store)
        self.state = self.api.import_xml(XML, "fixture")

    def tearDown(self):
        self.tmp.cleanup()

    def playlist_id(self, name="Friday Set"):
        return find_playlist(self.state["playlists"], name)["id"]

    def test_import_reports_the_library(self):
        self.assertTrue(self.state["has_library"])
        self.assertEqual(self.state["track_count"], 12)
        self.assertEqual(self.state["source"], "fixture")
        self.assertIn("journey", self.state["templates"])
        self.assertTrue((Path(self.tmp.name) / "library.xml").exists())

    def test_playlists_are_exposed_without_track_id_lists(self):
        node = find_playlist(self.state["playlists"], "Friday Set")
        self.assertEqual(node["count"], 8)
        self.assertNotIn("track_ids", node)
        self.assertEqual(find_playlist(self.state["playlists"], "Folders")["total"], 5)

    def test_tracks_are_enriched(self):
        data = self.api.tracks(self.playlist_id())
        self.assertEqual(data["count"], 8)
        first = data["tracks"][0]
        self.assertEqual(first["camelot"], "8A")
        self.assertEqual(first["musical_key"], "Am")
        self.assertEqual(first["energy"], 4.0)
        self.assertEqual(first["energy_source"], "tag")
        self.assertEqual(first["label"], "Marlow - Deep Intro")
        self.assertIn("key", first)

    def test_tracks_in_a_folder_include_children(self):
        data = self.api.tracks(self.playlist_id("Folders"))
        self.assertEqual(data["count"], 5)

    def test_build_uses_only_the_chosen_playlist(self):
        playlist = self.api.tracks(self.playlist_id())
        playlist_keys = {track["key"] for track in playlist["tracks"]}
        result = self.api.build(
            {
                "playlist_id": self.playlist_id(),
                "config": {"template": "peak_time", "length_tracks": 6, "seed": 1, "iterations": 600},
            }
        )
        self.assertEqual(len(result["order"]), 6)
        self.assertTrue(set(result["order_keys"]).issubset(playlist_keys))
        self.assertEqual(len(result["transitions"]), 5)
        self.assertEqual(len(result["targets"]), 6)
        self.assertGreater(result["stats"]["peak_energy"], 0)
        self.assertEqual(result["order"][0]["key"], result["order_keys"][0])

    def test_optimize_keeps_a_locked_track_in_place(self):
        result = self.api.build(
            {"playlist_id": self.playlist_id(), "config": {"length_tracks": 7, "seed": 3, "iterations": 400}}
        )
        keys = result["order_keys"]
        locked = keys[0]
        optimized = self.api.optimize(
            {"order": keys, "config": {"locks": {"0": locked}, "seed": 4, "iterations": 800}}
        )
        self.assertEqual(optimized["order_keys"][0], locked)
        self.assertEqual(sorted(optimized["order_keys"]), sorted(keys))

    def test_energy_override_wins_and_is_saved(self):
        track = self.api.tracks(self.playlist_id())["tracks"][0]
        self.api.set_energy({"track_key": track["key"], "value": 9.5})
        again = self.api.tracks(self.playlist_id())["tracks"][0]
        self.assertEqual(again["energy"], 9.5)
        self.assertTrue(again["energy_override"])
        self.assertEqual(again["energy_source"], "override")

        # A fresh store on the same directory keeps the edit.
        reopened = Api(Store(Path(self.tmp.name)))
        persisted = reopened.tracks(self.playlist_id())["tracks"][0]
        self.assertEqual(persisted["energy"], 9.5)

        # Clearing it returns to the heuristic value.
        self.api.set_energy({"track_key": track["key"], "value": None})
        self.assertFalse(self.api.tracks(self.playlist_id())["tracks"][0]["energy_override"])

    def test_export_rekordbox_xml_matches_the_set_order(self):
        result = self.api.build(
            {"playlist_id": self.playlist_id(), "config": {"length_tracks": 5, "seed": 2, "iterations": 200}}
        )
        content, filename, content_type = self.api.export(
            {"order": result["order_keys"], "name": "Friday Peak", "format": "rekordbox"}
        )
        self.assertEqual(filename, "Friday-Peak.xml")
        self.assertEqual(content_type, "application/xml")

        library = rekordbox.parse_collection(content)
        exported_titles = [library.tracks[tid].title for tid in library.playlists[0].track_ids]
        self.assertEqual(exported_titles, [track["title"] for track in result["order"]])
        for track in result["order"]:
            self.assertTrue(library.tracks[track["id"]].bpm > 0)

    def test_export_other_formats(self):
        result = self.api.build(
            {"playlist_id": self.playlist_id(), "config": {"length_tracks": 4, "seed": 2, "iterations": 100}}
        )
        payload = {"order": result["order_keys"], "name": "Peak Hour", "format": "m3u8"}
        content, filename, _ = self.api.export(payload)
        self.assertTrue(content.startswith(b"#EXTM3U"))
        self.assertEqual(filename, "Peak-Hour.m3u8")

        payload["format"] = "csv"
        content, filename, _ = self.api.export(payload)
        self.assertEqual(filename, "Peak-Hour.csv")
        self.assertIn(b"position,title,artist", content)

        payload["format"] = "txt"
        content, filename, _ = self.api.export(payload)
        self.assertEqual(filename, "Peak-Hour.txt")
        self.assertIn(b"tracks |", content)

        with self.assertRaises(ValueError):
            self.api.export({**payload, "format": "spotify"})

    def test_the_exported_xml_carries_the_set_tags(self):
        result = self.api.build(
            {"playlist_id": self.playlist_id(), "config": {"length_tracks": 4, "seed": 2, "iterations": 100}}
        )
        content, _, _ = self.api.export(
            {"order": result["order_keys"], "name": "Tagged", "format": "rekordbox"}
        )
        library = rekordbox.parse_collection(content)
        first = library.tracks[str(result["order"][0]["id"])]
        self.assertIn("[E", first.comments)
        self.assertIn("[opener]", first.comments)
        self.assertIn("1/4", first.comments)
        # The order is the set's order, so the last track closes it.
        last = library.tracks[str(result["order"][-1]["id"])]
        self.assertIn("[closer]", last.comments)

    def test_tags_can_be_left_out_of_an_export(self):
        result = self.api.build(
            {"playlist_id": self.playlist_id(), "config": {"length_tracks": 3, "seed": 2, "iterations": 100}}
        )
        content, _, _ = self.api.export(
            {"order": result["order_keys"], "name": "Untagged", "format": "rekordbox", "tags": False}
        )
        library = rekordbox.parse_collection(content)
        for track in library.tracks.values():
            self.assertNotIn("[E", track.comments)

    def test_the_set_package_carries_the_whole_set(self):
        import io
        import zipfile

        result = self.api.build(
            {"playlist_id": self.playlist_id(), "config": {"length_tracks": 4, "seed": 2, "iterations": 100}}
        )
        content, filename, content_type = self.api.export(
            {"order": result["order_keys"], "name": "Friday Peak", "format": "package"}
        )
        self.assertEqual(filename, "Friday-Peak.zip")
        self.assertEqual(content_type, "application/zip")

        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = set(archive.namelist())
            self.assertEqual(
                names,
                {
                    "Friday-Peak.xml",
                    "Friday-Peak.txt",
                    "Friday-Peak.csv",
                    "Friday-Peak.cues.txt",
                    "Friday-Peak.preflight.txt",
                    "Friday-Peak.autopilot.json",
                    "README.txt",
                },
            )
            xml = archive.read("Friday-Peak.xml")
            cue_log = archive.read("Friday-Peak.cues.txt").decode("utf-8")
            check = archive.read("Friday-Peak.preflight.txt").decode("utf-8")
            readme = archive.read("README.txt").decode("utf-8")
            set_list = archive.read("Friday-Peak.txt").decode("utf-8")

        exported = rekordbox.parse_collection(xml)
        self.assertEqual(
            [exported.tracks[tid].title for tid in exported.playlists[0].track_ids],
            [track["title"] for track in result["order"]],
        )
        self.assertIn("cue log", cue_log)
        self.assertIn("Friday Peak", check)
        self.assertIn("pre-flight", check.lower())
        self.assertIn("Friday Peak", set_list)
        # The note names every part, so nothing is in the zip undocumented.
        for name in ("Friday-Peak.xml", "Friday-Peak.cues.txt", "Friday-Peak.preflight.txt"):
            self.assertIn(name, readme)

    def test_preflight_reports_missing_files_and_duplicates(self):
        result = self.api.build(
            {"playlist_id": self.playlist_id(), "config": {"length_tracks": 3, "seed": 2, "iterations": 100}}
        )
        keys = result["order_keys"]
        report = self.api.preflight({"order": keys, "name": "Friday Peak"})
        self.assertEqual(report["checked"], len(keys))
        self.assertFalse(report["ok"])
        # The fixture library is a real rekordbox export with paths that only
        # exist on the machine it came from.
        self.assertEqual(report["summary"]["missing_file"], len(keys))
        self.assertIn("Friday Peak", report["text"])
        self.assertIn("ERRORS", report["text"])

        doubled = self.api.preflight({"order": [keys[0], keys[0], keys[1]]})
        kinds = [issue["kind"] for issue in doubled["issues"]]
        self.assertIn("duplicate_track", kinds)
        self.assertEqual(doubled["checked"], 3)

    def test_preflight_of_an_empty_set_is_empty(self):
        report = self.api.preflight({"order": []})
        self.assertEqual(report["checked"], 0)
        self.assertTrue(report["ok"])
        self.assertEqual(report["issues"], [])

    def test_export_can_be_saved_to_disk(self):
        """The native app exports this way: the server writes the file itself."""
        result = self.api.build(
            {"playlist_id": self.playlist_id(), "config": {"length_tracks": 4, "seed": 2, "iterations": 100}}
        )
        payload = {
            "order": result["order_keys"], "name": "Saved Set", "format": "csv",
            "directory": self.tmp.name,
        }
        saved = self.api.save_export(payload)
        self.assertEqual(saved["filename"], "Saved-Set.csv")
        self.assertEqual(saved["directory"], self.tmp.name)

        written = Path(saved["saved_to"])
        self.assertTrue(written.is_file())
        self.assertEqual(saved["bytes"], written.stat().st_size)
        self.assertEqual(len(written.read_text().strip().splitlines()), 5)  # header + 4 tracks

        with self.assertRaises(ValueError):
            self.api.save_export({**payload, "directory": "/dev/null/nowhere"})

    def test_export_without_a_set_is_rejected(self):
        with self.assertRaises(ValueError):
            self.api.export({"order": [], "format": "rekordbox"})

    def test_unknown_playlist_yields_no_tracks(self):
        self.assertEqual(self.api.tracks("pl:nope")["count"], 0)

    def test_sets_round_trip(self):
        result = self.api.build(
            {"playlist_id": self.playlist_id(), "config": {"length_tracks": 4, "seed": 2, "iterations": 100}}
        )
        saved = self.store.save_set(
            {"name": "Friday", "playlist_id": self.playlist_id(), "order": result["order_keys"], "locks": {"0": result["order_keys"][0]}}
        )
        self.assertEqual(len(self.store.list_sets()), 1)
        self.assertEqual(saved["order"], result["order_keys"])
        self.assertEqual(saved["locks"], {"0": result["order_keys"][0]})
        self.assertTrue(self.store.delete_set(saved["id"]))
        self.assertEqual(self.store.list_sets(), [])

    def test_bad_xml_is_rejected_with_a_helpful_message(self):
        with self.assertRaises(ValueError):
            self.api.import_xml("nonsense", "nonsense.xml")
        with self.assertRaises(ValueError):
            self.api.import_xml('<?xml version="1.0"?><DJ_PLAYLISTS><COLLECTION Entries="0"/></DJ_PLAYLISTS>', "empty")

    def test_track_views_are_cached_and_invalidated_on_change(self):
        first = self.api.all_views()
        self.assertIs(self.api.all_views(), first)  # memoised, not recomputed

        track = self.api.tracks(self.playlist_id())["tracks"][0]
        self.api.set_energy({"track_key": track["key"], "value": 9.5})
        refreshed = self.api.all_views()
        self.assertIsNot(refreshed, first)  # a write must invalidate
        self.assertEqual(refreshed[track["key"]]["energy"], 9.5)

    def test_a_large_library_round_trips(self):
        rows = "\n".join(
            f'    <TRACK TrackID="{i}" Name="Track {i}" Artist="Artist {i % 40}" Genre="Techno" '
            f'AverageBpm="128.00" Tonality="8A" Rating="153" TotalTime="300" '
            f'Location="file://localhost/Music/{i}.aiff"/>'
            for i in range(1, 1501)
        )
        links = "\n".join(f'        <TRACK Key="{i}"/>' for i in range(1, 1501))
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n<DJ_PLAYLISTS Version="1.0.0">\n'
            f'  <COLLECTION Entries="1500">\n{rows}\n  </COLLECTION>\n  <PLAYLISTS>\n'
            f'    <NODE Type="0" Name="ROOT" Count="1">\n'
            f'      <NODE Name="Big" Type="1" KeyType="0" Entries="1500">\n{links}\n      </NODE>\n'
            "    </NODE>\n  </PLAYLISTS>\n</DJ_PLAYLISTS>"
        )
        state = self.api.import_xml(xml, "generated")
        self.assertEqual(state["track_count"], 1500)

        big = find_playlist(state["playlists"], "Big")["id"]
        self.assertEqual(self.api.tracks(big)["count"], 1500)

        result = self.api.build(
            {"playlist_id": big, "config": {"length_tracks": 120, "seed": 5, "iterations": 1500}}
        )
        self.assertEqual(len(result["order"]), 120)
        self.assertEqual(len(set(result["order_keys"])), 120)
        self.assertEqual(result["stats"]["harmonic_ok"], 100.0)

    def test_reload_rereads_the_stored_xml(self):
        self.assertTrue(self.store.refresh_library_from_xml())
        self.assertEqual(self.store.library()["track_count"], 12)

    def test_reset_clears_everything(self):
        self.store.reset()
        self.assertFalse(self.store.library())
        self.assertEqual(self.store.list_sets(), [])


class HttpTest(unittest.TestCase):
    """Smoke test over real HTTP, the same interface the browser uses."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.httpd = start_in_thread("127.0.0.1", 0, Path(self.tmp.name))
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as response:
            return response.status, response.read(), dict(response.headers)

    def post(self, path, payload):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())

    def test_index_is_served_with_its_assets(self):
        status, body, headers = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Parity", body)
        self.assertIn("text/html", headers["Content-Type"])

        status, body, headers = self.get("/app.js")
        self.assertEqual(status, 200)
        self.assertIn(b"javascript", headers["Content-Type"].encode())
        self.assertIn(b"/api/build", body)

        status, body, _ = self.get("/style.css")
        self.assertEqual(status, 200)
        self.assertIn(b"--accent", body)

    def test_a_static_path_cannot_reach_outside_the_web_folder(self):
        """A sibling folder whose name *starts with* "web" must stay unreadable.

        This is the case a string-prefix containment check serves by accident:
        ``"/…/webX/secret.txt".startswith("/…/web")`` is true.  The dots are
        percent-encoded because that is what a browser sends for them, and the
        route is decoded before it is resolved -- which is the whole of the
        bug: the decoded path is what has to be contained.
        """
        # Resolved, like the real ``WEB_DIR``, which is built from
        # ``Path(__file__).resolve()``.  An unresolved temp path never compares
        # equal to a resolved target, so it would hide the bug, not test it:
        # ``/var`` and ``/private/var`` are the same folder on macOS.
        folder = Path(self.tmp.name).resolve()
        (folder / "web").mkdir()
        (folder / "web" / "index.html").write_text("<title>Parity</title>", encoding="utf-8")
        (folder / "webX").mkdir()
        (folder / "webX" / "secret.txt").write_text("secret", encoding="utf-8")

        with mock.patch.object(server, "WEB_DIR", folder / "web"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.get("/%2e%2e/webX/secret.txt")
        self.assertEqual(caught.exception.code, 404)

    def test_empty_state_then_import_build_export(self):
        status, body, _ = self.get("/api/state")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["has_library"])

        status, state = self.post("/api/import", {"xml": XML, "source": "over-the-wire"})
        self.assertEqual(status, 200)
        self.assertTrue(state["has_library"])

        playlist_id = find_playlist(state["playlists"], "Friday Set")["id"]
        status, tracks = self.post("/api/build", {"playlist_id": playlist_id, "config": {"length_tracks": 6, "seed": 1}})
        self.assertEqual(status, 200)
        self.assertEqual(len(tracks["order"]), 6)

        request = urllib.request.Request(
            self.base + "/api/export",
            data=json.dumps({"order": tracks["order_keys"], "name": "Over the wire", "format": "rekordbox"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("attachment", response.headers["Content-Disposition"])
            body = response.read()
        self.assertIn(b"DJ_PLAYLISTS", body)

    def test_the_preflight_endpoint_is_reachable_over_the_wire(self):
        _, state = self.post("/api/import", {"xml": XML, "source": "preflight-test"})
        playlist_id = find_playlist(state["playlists"], "Friday Set")["id"]
        _, built = self.post(
            "/api/build", {"playlist_id": playlist_id, "config": {"length_tracks": 3, "seed": 1}}
        )
        keys = built["order_keys"]

        status, report = self.post("/api/preflight", {"order": keys, "name": "Over the wire"})
        self.assertEqual(status, 200)
        self.assertEqual(report["checked"], 3)
        self.assertFalse(report["ok"])
        self.assertEqual(report["summary"]["missing_file"], 3)
        self.assertTrue(report["text"])

        status, body, _ = self.get(f"/api/preflight?key={keys[0]}&cues=0")
        self.assertEqual(status, 200)
        checked = json.loads(body)
        self.assertEqual(checked["checked"], 1)
        self.assertEqual(checked["summary"]["no_cues"], 0)  # cues were not asked for

    def test_the_tag_endpoint_returns_what_the_file_will_carry(self):
        """The tag on a set row is the tag the export writes, not a second guess."""
        _, state = self.post("/api/import", {"xml": XML, "source": "tags-test"})
        playlist_id = find_playlist(state["playlists"], "Friday Set")["id"]
        _, built = self.post(
            "/api/build", {"playlist_id": playlist_id, "config": {"length_tracks": 4, "seed": 2}}
        )
        keys = built["order_keys"]

        status, plan = self.post("/api/tags", {"order": keys})
        self.assertEqual(status, 200)
        self.assertEqual(plan["count"], 4)
        self.assertEqual([row["key"] for row in plan["rows"]], keys)
        self.assertEqual(plan["rows"][0]["role"], "opener")
        self.assertEqual(plan["rows"][-1]["role"], "closer")
        self.assertTrue(plan["rows"][0]["reason"])
        for row in plan["rows"]:
            parsed = tags.parse_tag(row["tag"])
            self.assertIsNotNone(parsed, row["tag"])
            self.assertEqual(parsed["role"], row["role"])
            self.assertEqual(parsed["position"], row["position"])
            self.assertEqual(row["after"], tags.merge_comment(row["before"], row["tag"]))

        # The tag it hands back is the comment the XML comes out with, compared
        # here track by track: the row says what will be written, so it has to be
        # the same arithmetic rather than a second guess.
        request = urllib.request.Request(
            self.base + "/api/export",
            data=json.dumps({"order": keys, "name": "Tagged", "format": "rekordbox"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            library = rekordbox.parse_collection(response.read())
        by_key = {track["key"]: track for track in built["order"]}
        for row in plan["rows"]:
            self.assertEqual(library.tracks[str(by_key[row["key"]]["id"])].comments, row["after"])

        # The tag is decided from the order, so asking about the same set the
        # other way round swaps the ends of it and nothing else.
        status, reversed_plan = self.post("/api/tags", {"order": list(reversed(keys))})
        self.assertEqual(status, 200)
        self.assertEqual(
            [row["key"] for row in reversed_plan["rows"]], list(reversed(keys))
        )
        self.assertEqual(reversed_plan["rows"][0]["role"], "opener")
        self.assertEqual(reversed_plan["rows"][-1]["role"], "closer")

    def test_the_tag_endpoint_is_quiet_without_a_set(self):
        self.post("/api/import", {"xml": XML, "source": "tags-empty"})
        status, plan = self.post("/api/tags", {"order": []})
        self.assertEqual(status, 200)
        self.assertEqual(plan["rows"], [])
        self.assertEqual(plan["count"], 0)
        # Keys the library does not know are dropped rather than guessed at.
        status, plan = self.post("/api/tags", {"order": ["nope-1", "nope-2"]})
        self.assertEqual(status, 200)
        self.assertEqual(plan["count"], 0)

    def test_the_folder_tidy_up_endpoints_are_reachable(self):
        folder = Path(self.tmp.name) / "staging"
        folder.mkdir()
        (folder / "late night.wav").write_bytes(b"RIFF" + b"\x00" * 40)

        status, body, _ = self.get("/api/ingest")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual([entry["key"] for entry in payload["formats"]], ["aiff", "wav"])

        status, data = self.post(
            "/api/ingest", {"source": str(folder), "format": "wav", "measure": False}
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["plan"]["summary"]["found"], 1)

        status, data = self.post(
            "/api/ingest/run", {"source": str(folder), "format": "wav", "measure": False}
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["report"]["summary"]["copied"], 1)

    def test_the_cue_editor_endpoints_are_reachable(self):
        _, state = self.post("/api/import", {"xml": XML, "source": "editor-test"})
        playlist_id = find_playlist(state["playlists"], "Friday Set")["id"]
        status, body, _ = self.get(f"/api/tracks?playlist={urllib.parse.quote(playlist_id)}")
        self.assertEqual(status, 200)
        key = json.loads(body)["tracks"][0]["key"]
        quoted = urllib.parse.quote(key, safe="")

        status, analysed = self.post("/api/cues", {"keys": [key]})
        self.assertEqual(status, 200)
        self.assertEqual(analysed["analysed"], 1)

        status, body, _ = self.get(f"/api/cues?key={quoted}")
        self.assertEqual(status, 200)
        plan = json.loads(body)
        self.assertEqual([cue["slot"] for cue in plan["hot_cues"]], list("ABCDEFGH"))
        self.assertTrue(plan["first_beat_ms"] == 0 or plan["first_beat_ms"] > 0)
        self.assertGreater(plan["duration_ms"], 0)

        target = plan["hot_cues"][3]["ms"] + 900
        status, answer = self.post("/api/cues/edit", {"key": key, "slot": "D", "ms": target})
        self.assertEqual(status, 200)
        self.assertEqual(answer["cue"]["ms"], target)
        self.assertTrue(answer["cue"]["locked"])

        audio_file = Path(self.tmp.name) / "over-the-wire.wav"
        audio_file.write_bytes(b"RIFF....WAVEfmt " + b"\x00" * 64)
        fake = waveform.Waveform(
            peaks={"low": [1, 255], "mid": [1, 100], "high": [1, 50]},
            seconds=120.0,
            duration_ms=120000,
            peaks_per_band=2,
        )
        with mock.patch.object(Api, "audio_path", lambda api, requested: audio_file), \
                mock.patch.object(waveform, "analyse", lambda path, seconds=None: fake):
            status, body, _ = self.get(f"/api/waveform?key={quoted}")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertFalse(payload["cached"])
        self.assertEqual(payload["waveform"]["peaks"]["low"], [1, 255])

        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/api/waveform")
        self.assertEqual(caught.exception.code, 400)

    def test_export_save_returns_the_written_path(self):
        _, state = self.post("/api/import", {"xml": XML, "source": "save-test"})
        playlist_id = find_playlist(state["playlists"], "Peak material")["id"]
        _, built = self.post(
            "/api/build", {"playlist_id": playlist_id, "config": {"length_tracks": 4, "seed": 1}}
        )
        status, saved = self.post(
            "/api/export",
            {
                "order": built["order_keys"], "name": "Dock App Set", "format": "rekordbox",
                "save": True, "directory": self.tmp.name,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(saved["filename"], "Dock-App-Set.xml")
        body = Path(saved["saved_to"]).read_text()
        self.assertIn("DJ_PLAYLISTS", body)
        self.assertEqual(body.count("<TRACK "), 8)  # 4 in the collection, 4 in the playlist

    def test_unknown_endpoint_and_bad_payload(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/api/nope")
        self.assertEqual(caught.exception.code, 404)

        request = urllib.request.Request(
            self.base + "/api/import",
            data=b"{not json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 400)
        self.assertIn("Malformed", caught.exception.read().decode())

    def test_preview_streams_a_track_and_honours_a_range(self):
        """The preview player needs real bytes: the whole file, then a range."""
        payload = b"""<?xml version="1.0" encoding="UTF-8"?>
<DJ_PLAYLISTS Version="1.0.0"><PRODUCT Name="rekordbox"/>
<COLLECTION Entries="1">
<TRACK TrackID="1" Name="Preview Me" Artist="A" Genre="House" AverageBpm="126.00"
       Tonality="8A" TotalTime="300" Location="LOCATION" Kind="MP3 File"/>
</COLLECTION></DJ_PLAYLISTS>""".decode("utf-8")
        audio = Path(self.tmp.name) / "preview me.mp3"
        audio.write_bytes(bytes(range(256)) * 40)
        location = "file://localhost" + str(audio).replace(" ", "%20")
        self.post("/api/import", {"xml": payload.replace("LOCATION", location), "source": "fixture"})
        key = json.loads(self.get("/api/tracks")[1])["tracks"][0]["key"]

        status, body, headers = self.get(f"/api/audio?key={key}")
        self.assertEqual(status, 200)
        self.assertEqual(body, audio.read_bytes())
        self.assertEqual(headers["Content-Type"], "audio/mpeg")
        self.assertEqual(headers["Accept-Ranges"], "bytes")

        request = urllib.request.Request(
            f"{self.base}/api/audio?key={key}", headers={"Range": "bytes=10-19"}
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            ranged = response.read()
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers["Content-Range"], f"bytes 10-19/{len(body)}")
        self.assertEqual(ranged, body[10:20])

    def test_preview_refuses_an_unknown_key(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/api/audio?key=nope")
        self.assertEqual(caught.exception.code, 404)

    def test_path_traversal_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/../parity/server.py")
        self.assertEqual(caught.exception.code, 404)


STREAMING_XML = """<?xml version="1.0" encoding="UTF-8"?>
<DJ_PLAYLISTS Version="1.0.0">
  <PRODUCT Name="rekordbox" Version="7.2.19" Company="AlphaTheta"/>
  <COLLECTION Entries="2">
    <TRACK TrackID="1" Name="Local One" Artist="A" Genre="Tech House" AverageBpm="126.00"
           Tonality="8A" Rating="204" TotalTime="300" Location="file://localhost/Volumes/nas/a.mp3"
           Kind="MP3 File" Size="123" Comments="E6"/>
    <TRACK TrackID="2" Name="Cloud Edit" Artist="B" AverageBpm="128.00" Tonality="9A"
           Rating="0" TotalTime="240" Location="file://localhostsoundcloud:tracks:862894612"
           Kind="Unknown Format" Size="0" Comments=""/>
  </COLLECTION>
  <PLAYLISTS>
    <NODE Type="0" Name="ROOT" Count="1">
      <NODE Name="Crate" Type="1" KeyType="0" Entries="2">
        <TRACK Key="1"/>
        <TRACK Key="2"/>
      </NODE>
    </NODE>
  </PLAYLISTS>
</DJ_PLAYLISTS>
"""


class StreamingFlagTest(unittest.TestCase):
    """Streaming entries are flagged through the API so the UI can filter them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.api = Api(self.store)
        self.state = self.api.import_xml(STREAMING_XML, "rbexport.xml")

    def tearDown(self):
        self.tmp.cleanup()

    def test_state_counts_streaming_tracks(self):
        self.assertEqual(self.state["track_count"], 2)
        self.assertEqual(self.state["streaming_count"], 1)

    def test_tracks_carry_the_flag(self):
        by_title = {t["title"]: t for t in self.api.tracks(None)["tracks"]}
        self.assertFalse(by_title["Local One"]["streaming"])
        self.assertTrue(by_title["Cloud Edit"]["streaming"])
        self.assertEqual(by_title["Local One"]["path"], "/Volumes/nas/a.mp3")
        self.assertEqual(by_title["Cloud Edit"]["path"], "")

    def test_reload_keeps_the_flag_and_the_source_file(self):
        reloaded = Api(Store(Path(self.tmp.name)))
        state = reloaded.state()
        self.assertEqual(state["streaming_count"], 1)
        self.assertEqual(state["source"], "rbexport.xml")
        self.assertTrue(reloaded.store.refresh_library_from_xml())
        self.assertEqual(reloaded.state()["source"], "rbexport.xml")


class RekordboxModuleTest(unittest.TestCase):
    """The direct-database endpoints, exercised without pyrekordbox installed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.api = Api(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_status_reports_the_database_path_and_the_extra(self):
        status = self.api.rekordbox_status()
        self.assertIn("path", status)
        self.assertIn("pyrekordbox", status)
        self.assertIn("available", status["pyrekordbox"])
        self.assertIn("hint", status["pyrekordbox"])
        self.assertIn("library", status)
        self.assertFalse(status["library"]["loaded"])

    def test_state_exposes_the_rekordbox_summary(self):
        self.api.import_xml(XML, "fixture")
        state = self.api.state()
        self.assertIn("rekordbox", state)
        self.assertEqual(state["source_kind"], "xml")
        self.assertTrue(state["has_source_xml"])
        self.assertEqual(state["analysed_count"], 0)

    def test_connect_without_a_database_or_fallback_is_a_tidy_error(self):
        with self.assertRaises(ValueError) as caught:
            self.api.connect_rekordbox({"db_path": "/definitely/not/here/master.db"})
        self.assertIn("No rekordbox database", str(caught.exception))

    def test_connect_falls_back_to_an_xml_export(self):
        result = self.api.connect_rekordbox(
            {"db_path": "/definitely/not/here/master.db", "fallback_xml": str(FIXTURE)}
        )
        self.assertTrue(result["connected"])
        self.assertEqual(result["origin"], "xml")
        self.assertIn("master.db unavailable", result["detail"])
        self.assertEqual(result["track_count"], 12)
        self.assertEqual(self.store.library_origin()["source_kind"], "xml")


class DatabaseImportTest(unittest.TestCase):
    """A master.db import must not throw away a stored XML export."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_stale_export_stays_on_disk_but_is_not_reused(self):
        self.store.set_library(rekordbox.parse_collection(XML), XML, source="rbexport.xml")
        xml_path = Path(self.tmp.name) / "library.xml"
        self.assertTrue(xml_path.exists())
        self.assertIsNotNone(self.store.library_xml())

        # Now a database-style import (no XML of its own).
        db_library = rekordbox.parse_collection(XML)
        db_library.source_kind = "db"
        self.store.set_library(db_library, None, source="/tmp/master.db", source_kind="db")

        self.assertTrue(xml_path.exists(), "the user's export was deleted")
        self.assertIsNone(self.store.library_xml())
        self.assertEqual(self.store.library_origin()["source_kind"], "db")
        self.assertTrue(self.store.library_origin()["has_xml"])

    def test_a_reload_after_a_db_import_rereads_the_database(self):
        # The user had an export stored, then imported master.db on top of it.
        self.store.set_library(rekordbox.parse_collection(XML), XML, source="rbexport.xml")
        db_library = rekordbox.parse_collection(XML)
        db_library.source_kind = "db"
        self.store.set_library(db_library, None, source="/tmp/master.db", source_kind="db")
        xml_path = Path(self.tmp.name) / "library.xml"
        self.assertTrue(xml_path.exists())
        # Point the database lookup at a path that cannot exist, so this test says
        # the same thing on a machine with a real rekordbox install as on CI.
        with mock.patch.dict(os.environ, {"PARITY_MASTER_DB": "/definitely/not/here/master.db"}):
            # A reload follows the origin: the database, never the leftover export.
            self.assertFalse(self.store.refresh_library())
            self.assertFalse(self.store.refresh_library_from_db())
            self.assertEqual(self.store.library_origin()["source_kind"], "db")
            # Re-reading the stored copy is still there as an explicit choice.
            self.assertTrue(self.store.refresh_library_from_xml())
            self.assertEqual(self.store.library_origin()["source_kind"], "xml")


class PoolFilterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.api = Api(self.store)
        self.api.import_xml(STREAMING_XML, "rbexport.xml")

    def tearDown(self):
        self.tmp.cleanup()

    def test_streaming_tracks_are_purged_and_reported(self):
        result = self.api.pool({"filter": {"require_file": False}})
        self.assertEqual(result["report"]["scanned"], 2)
        self.assertEqual(result["report"]["kept"], 1)
        self.assertEqual(result["report"]["reasons"]["streaming"], 1)
        self.assertEqual(len(result["keys"]), 1)

    def test_a_track_can_be_kept_when_streaming_is_allowed(self):
        result = self.api.pool({"filter": {"local_only": False, "require_file": False}})
        self.assertEqual(result["report"]["kept"], 2)

    def test_folder_selection_narrows_the_pool(self):
        result = self.api.pool({"filter": {"folder": "/definitely/elsewhere", "require_file": False}})
        self.assertEqual(result["report"]["kept"], 0)

    def test_pool_needs_a_library(self):
        empty = Api(Store(Path(self.tmp.name) / "other"))
        with self.assertRaises(ValueError):
            empty.pool({})

    def test_build_honours_a_filter(self):
        result = self.api.build(
            {"filter": {"require_file": False}, "config": {"length_tracks": 1, "seed": 1, "iterations": 50}}
        )
        self.assertEqual(len(result["order"]), 1)
        self.assertFalse(result["order"][0]["streaming"])


class CueGenerationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.api = Api(self.store)
        self.api.import_xml(XML, "fixture")

    def tearDown(self):
        self.tmp.cleanup()

    def test_analysis_without_rekordbox_data_falls_back_and_says_so(self):
        result = self.api.analyse({})
        self.assertEqual(result["analysed"], 12)
        self.assertEqual(result["by_source"], {"heuristic": 12})
        self.assertEqual(result["stored"], 12)
        for row in result["results"]:
            self.assertEqual(row["confidence"], "low")
            self.assertEqual([cue["slot"] for cue in row["hot_cues"]], list("ABCDEFGH"))
            self.assertTrue(row["note"])

    def test_views_carry_the_cue_summary(self):
        self.api.analyse({})
        track = self.api.tracks(self.api.state()["playlists"][0]["id"])["tracks"][0]
        self.assertTrue(track["cues"]["analysed"])
        self.assertEqual(track["cues"]["hot"], 8)
        self.assertEqual(track["cues"]["confidence"], "low")
        self.assertEqual(self.api.state()["cue_plan_count"], 12)

    def test_analysis_can_be_scoped_to_keys(self):
        keys = [self.api.tracks(None)["tracks"][0]["key"]]
        result = self.api.analyse({"keys": keys})
        self.assertEqual(result["analysed"], 1)
        self.assertEqual(result["stored"], 1)

    def test_cue_plans_survive_a_reload(self):
        self.api.analyse({})
        reopened = Api(Store(Path(self.tmp.name)))
        self.assertEqual(reopened.state()["cue_plan_count"], 12)
        reopened.store.clear_cue_plans()
        self.assertEqual(reopened.state()["cue_plan_count"], 0)

    def test_exports_carry_the_plans(self):
        self.api.analyse({})
        tracks = self.api.tracks(None)["tracks"]
        plans = self.api.export_cue_plans([track["key"] for track in tracks])
        self.assertEqual(len(plans), 12)
        plan = plans[tracks[0]["key"]]
        self.assertEqual(len(plan["hot_cues"]), 8)
        self.assertEqual(plan["beat_grid"]["bpm"], plan["structure"]["bpm"])

    # -- existing cues ----------------------------------------------------

    def recue(self, key, cues):
        """Give a stored track the cues rekordbox would report for it.

        The track views are memoised on ``library_version``, which only an
        import bumps, so the test bumps it the way a real import does.
        """
        self.api.store.tracks_by_key()[key]["cues"] = cues
        self.api.store.library_version += 1

    def cued_track(self):
        """One track of the fixture, marked as hand-cued in rekordbox."""
        track = self.api.tracks(None)["tracks"][0]
        self.recue(
            track["key"],
            [{"num": 4, "name": "", "start": 82.076, "kind": "hot", "source": "db"}],
        )
        return track

    def test_existing_cues_reach_the_track_view(self):
        track = self.cued_track()
        view = self.api.tracks(None)["tracks"][0]
        self.assertEqual(view["existing_cues"]["hot"], 1)
        self.assertEqual(view["existing_cues"]["user"], 1)
        self.assertEqual(view["existing_cues"]["user_cues"][0]["seconds"], 82.076)
        self.assertEqual(view["cue_policy"], "overwrite")
        self.assertEqual(self.api.state()["tracks_with_user_cues"], 1)
        self.assertEqual(track["key"], view["key"])

    def test_cues_parity_wrote_are_counted_apart(self):
        key = self.api.tracks(None)["tracks"][0]["key"]
        self.recue(key, [
            {"num": 0, "name": "First Downbeat", "start": 0.0, "kind": "hot", "source": "db"},
            {"num": 6, "name": "Mix-Out / Breakdown", "start": 300.0, "kind": "hot", "source": "db"},
            {"num": -1, "name": "1.1Bars", "start": 0.0, "kind": "memory", "source": "db"},
        ])
        view = self.api.tracks(None)["tracks"][0]
        self.assertEqual(view["existing_cues"]["parity"], 2)
        self.assertEqual(view["existing_cues"]["user"], 0)

    def test_keep_carries_the_users_marks_and_none_of_ours(self):
        self.api.analyse({})
        track = self.cued_track()
        self.api.set_cue_policy({"policy": "keep"})
        plans = self.api.export_cue_plans([track["key"]])
        plan = plans[track["key"]]
        self.assertEqual(plan["hot_cues"], [])
        self.assertTrue(plan["kept"])
        self.assertEqual(plan["existing"][0]["start"], 82.076)
        generated = self.api.store.cue_plans()[track["key"]]
        self.assertEqual(plan["beat_grid"]["bpm"], generated["structure"]["bpm"])

        xml = self.api.xml_bytes([self.api.tracks(None)["tracks"][0]], "Kept", True)
        element = ET.fromstring(xml).find("COLLECTION/TRACK")
        marks = element.findall("POSITION_MARK")
        self.assertEqual([m.attrib["Num"] for m in marks], ["4"])
        self.assertEqual(marks[0].attrib["Start"], "82.076")
        self.assertIsNotNone(element.find("TEMPO"))

    def test_overwrite_exports_our_eight_cues(self):
        self.api.analyse({})
        track = self.cued_track()
        xml = self.api.xml_bytes([self.api.tracks(None)["tracks"][0]], "Written", True)
        element = ET.fromstring(xml).find("COLLECTION/TRACK")
        hot = [m for m in element.findall("POSITION_MARK") if int(m.attrib["Num"]) >= 0]
        self.assertEqual(len(hot), 8)
        self.assertEqual(hot[0].attrib["Name"], "First Downbeat")
        self.assertEqual(track["key"], self.api.tracks(None)["tracks"][0]["key"])


class CueEditorTest(unittest.TestCase):
    """The waveforms behind the cue editor and the edits made on them.

    The peaks themselves are covered in ``tests/test_waveform.py`` against real
    files; the interest here is the wiring -- one read per track, a cache that
    notices the file changing, a moved cue that survives the next analysis pass,
    and a locked cue that is still written under the ``keep`` policy.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.api = Api(self.store)
        self.api.import_xml(XML, "fixture")
        self.audio_file = Path(self.tmp.name) / "track.wav"
        self.audio_file.write_bytes(b"RIFF....WAVEfmt " + b"\x00" * 64)
        patch = mock.patch.object(Api, "audio_path", lambda api, key: self.audio_file)
        patch.start()
        self.addCleanup(patch.stop)
        self.reads: list[str] = []
        analyse = mock.patch.object(waveform, "analyse", self.fake_analyse)
        analyse.start()
        self.addCleanup(analyse.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def fake_analyse(self, path, seconds=None, peaks=None):
        self.reads.append(str(path))
        return waveform.Waveform(
            peaks={"low": [10, 200, 255], "mid": [5, 100, 120], "high": [1, 50, 60]},
            seconds=180.0,
            duration_ms=180000,
            peaks_per_band=3,
            engine="numpy",
        )

    def cue_editor(self):
        """A track with cues, its key, and the D cue as it was generated."""
        key = self.api.tracks(None)["tracks"][0]["key"]
        self.api.analyse({"keys": [key]})
        plan = self.api.cue_plan(key)
        generated = next(cue for cue in plan["hot_cues"] if cue["slot"] == "D")
        return key, plan, generated

    # -- the peaks --------------------------------------------------------

    def test_the_first_look_reads_the_file_and_the_next_one_does_not(self):
        key = self.api.tracks(None)["tracks"][0]["key"]
        first = self.api.waveform(key)
        self.assertFalse(first["cached"])
        self.assertEqual(len(self.reads), 1)
        self.assertEqual(first["waveform"]["peaks"]["low"], [10, 200, 255])
        self.assertEqual(first["waveform"]["peaks_per_band"], 3)
        self.assertEqual(first["waveform"]["model"], waveform.MODEL_VERSION)
        # The file is named with the profile so a cache entry can be explained.
        self.assertEqual(first["waveform"]["path"], str(self.audio_file))

        again = self.api.waveform(key)
        self.assertTrue(again["cached"])
        self.assertEqual(len(self.reads), 1)
        self.assertEqual(again["waveform"]["peaks"], first["waveform"]["peaks"])

    def test_a_changed_file_is_read_again(self):
        key = self.api.tracks(None)["tracks"][0]["key"]
        self.api.waveform(key)
        self.audio_file.write_bytes(b"RIFF....WAVEfmt " + b"\x00" * 128)
        self.assertFalse(self.api.waveform(key)["cached"])
        self.assertEqual(len(self.reads), 2)

    def test_force_reads_it_again_anyway(self):
        key = self.api.tracks(None)["tracks"][0]["key"]
        self.api.waveform(key)
        self.assertFalse(self.api.waveform(key, force=True)["cached"])
        self.assertEqual(len(self.reads), 2)

    def test_a_stored_profile_from_an_older_model_is_rebuilt(self):
        key = self.api.tracks(None)["tracks"][0]["key"]
        self.api.waveform(key)
        entry = self.store.waveforms()[key]
        entry["model"] = waveform.MODEL_VERSION - 1
        self.store.set_waveform(key, entry)
        self.assertFalse(self.api.waveform(key)["cached"])
        self.assertEqual(len(self.reads), 2)

    def test_a_track_with_no_file_has_no_waveform(self):
        with mock.patch.object(Api, "audio_path", side_effect=ValueError("That track is streaming only - there is no local file to preview.")):
            with self.assertRaises(ValueError) as caught:
                self.api.waveform("anything")
        self.assertIn("streaming", str(caught.exception).lower())
        self.assertEqual(self.reads, [])

    def test_a_missing_ffmpeg_is_reported_rather_than_500(self):
        with mock.patch.object(waveform, "analyse", side_effect=audio.AudioUnavailable("ffmpeg is not available")):
            with self.assertRaises(ValueError) as caught:
                self.api.waveform(self.api.tracks(None)["tracks"][0]["key"])
        self.assertIn("ffmpeg", str(caught.exception))

    def test_no_key_asks_for_no_waveform(self):
        with self.assertRaises(ValueError):
            self.api.waveform("")

    # -- moving a cue -----------------------------------------------------

    def test_moving_a_cue_moves_the_plan_and_locks_it(self):
        key, plan, generated = self.cue_editor()
        target = generated["ms"] + 1234
        answer = self.api.edit_cue({"key": key, "slot": "D", "ms": target})
        self.assertTrue(answer["moved"])
        self.assertEqual(answer["cue"]["ms"], target)
        self.assertEqual(answer["cue"]["seconds"], round(target / 1000.0, 3))
        # Moving it by hand is what makes it the user's: the lock follows, and
        # the plan records why this cue is not on a phrase boundary any more.
        self.assertTrue(answer["cue"]["locked"])
        self.assertTrue(answer["cue"]["edited"])
        self.assertIn("moved by hand", answer["cue"]["reason"])

        stored = self.api.cue_plan(key)
        moved = next(cue for cue in stored["hot_cues"] if cue["slot"] == "D")
        self.assertEqual(moved["ms"], target)
        self.assertTrue(moved["locked"])
        untouched = [cue["ms"] for cue in stored["hot_cues"] if cue["slot"] != "D"]
        self.assertEqual(untouched, [cue["ms"] for cue in plan["hot_cues"] if cue["slot"] != "D"])

    def test_a_cue_can_be_moved_by_seconds_instead(self):
        key, _, generated = self.cue_editor()
        answer = self.api.edit_cue({"key": key, "slot": "D", "seconds": 123.456})
        self.assertEqual(answer["cue"]["ms"], 123456)
        self.assertNotEqual(answer["cue"]["ms"], generated["ms"])

    def test_a_moved_cue_stays_on_the_track(self):
        key, plan, _ = self.cue_editor()
        beyond = self.api.edit_cue({"key": key, "slot": "D", "ms": plan["duration_ms"] + 60000})
        self.assertEqual(beyond["cue"]["ms"], plan["duration_ms"])
        below = self.api.edit_cue({"key": key, "slot": "D", "ms": -5000})
        self.assertEqual(below["cue"]["ms"], 0)

    def test_an_edit_reports_it_did_not_move_the_cue(self):
        key, _, generated = self.cue_editor()
        answer = self.api.edit_cue({"key": key, "slot": "D", "ms": generated["ms"]})
        self.assertFalse(answer["moved"])
        self.assertTrue(answer["cue"]["locked"])

    def test_a_cue_that_is_not_on_the_track_is_refused(self):
        key, _, _ = self.cue_editor()
        with self.assertRaises(ValueError) as caught:
            self.api.edit_cue({"key": key, "slot": "Z", "ms": 1000})
        self.assertIn("no Z cue", str(caught.exception))
        with self.assertRaises(ValueError):
            self.api.edit_cue({"key": key, "slot": "", "ms": 1000})

    def test_an_unanalysed_track_cannot_be_edited(self):
        key = self.api.tracks(None)["tracks"][0]["key"]
        with self.assertRaises(ValueError) as caught:
            self.api.edit_cue({"key": key, "slot": "D", "ms": 1000})
        self.assertIn("Analyse", str(caught.exception))

    def test_a_cue_can_be_unlocked_without_moving_it(self):
        key, _, generated = self.cue_editor()
        target = generated["ms"] + 700
        self.api.edit_cue({"key": key, "slot": "D", "ms": target})
        answer = self.api.edit_cue({"key": key, "slot": "D", "locked": False})
        self.assertFalse(answer["cue"]["locked"])
        self.assertEqual(answer["cue"]["ms"], target)

    # -- what a lock is for -----------------------------------------------

    def test_reanalysing_keeps_a_locked_cue_and_restores_an_unlocked_one(self):
        key, _, generated = self.cue_editor()
        target = generated["ms"] + 4321
        self.api.edit_cue({"key": key, "slot": "D", "ms": target})

        result = self.api.analyse({"keys": [key]})
        self.assertEqual(result["kept"], 1)
        self.assertEqual(result["kept_locked"], {key: 1})
        plan = self.api.cue_plan(key)
        moved = next(cue for cue in plan["hot_cues"] if cue["slot"] == "D")
        self.assertEqual(moved["ms"], target)
        self.assertTrue(moved["locked"])
        # The slot keeps the role and colour the new analysis gave it; only the
        # position is the user's.
        self.assertEqual(moved["role"], generated["role"])
        self.assertIn("kept where you put them", plan["note"])

        # Unlocked, the same cue is the analysis's to place again.
        self.api.edit_cue({"key": key, "slot": "D", "locked": False})
        self.api.analyse({"keys": [key]})
        restored = next(cue for cue in self.api.cue_plan(key)["hot_cues"] if cue["slot"] == "D")
        self.assertEqual(restored["ms"], generated["ms"])
        self.assertFalse(restored["locked"])

    def test_an_analysis_that_kept_nothing_reports_nothing(self):
        key = self.api.tracks(None)["tracks"][0]["key"]
        self.api.analyse({"keys": [key]})
        result = self.api.analyse({"keys": [key]})
        self.assertEqual(result["kept"], 0)
        self.assertEqual(result["kept_locked"], {})

    def test_keep_still_writes_the_cues_that_were_locked(self):
        key, _, generated = self.cue_editor()
        target = generated["ms"] + 2500
        self.api.edit_cue({"key": key, "slot": "D", "ms": target})
        # The DJ's own cue on the same track: under "keep" that is normally
        # enough for our eight to be left out entirely.
        self.api.store.tracks_by_key()[key]["cues"] = [
            {"num": 4, "name": "", "start": 82.076, "kind": "hot", "source": "db"}
        ]
        self.api.store.library_version += 1
        self.api.set_cue_policy({"policy": "keep"})

        plan = self.api.export_cue_plans([key])[key]
        self.assertTrue(plan["kept"])
        self.assertEqual(plan["kept_locked"], 1)
        self.assertEqual([cue["slot"] for cue in plan["hot_cues"]], ["D"])
        self.assertEqual(plan["hot_cues"][0]["ms"], target)
        self.assertEqual(plan["existing"][0]["start"], 82.076)

        track = next(t for t in self.api.tracks(None)["tracks"] if t["key"] == key)
        element = ET.fromstring(self.api.xml_bytes([track], "Kept", True)).find("COLLECTION/TRACK")
        marks = {mark.attrib["Num"]: mark.attrib["Start"] for mark in element.findall("POSITION_MARK")}
        self.assertEqual(marks["4"], "82.076")
        self.assertEqual(marks["3"], f"{target / 1000.0:.3f}")

    def test_keep_without_a_lock_still_writes_none_of_ours(self):
        key, _, _ = self.cue_editor()
        self.api.store.tracks_by_key()[key]["cues"] = [
            {"num": 4, "name": "", "start": 82.076, "kind": "hot", "source": "db"}
        ]
        self.api.store.library_version += 1
        self.api.set_cue_policy({"policy": "keep"})
        plan = self.api.export_cue_plans([key])[key]
        self.assertEqual(plan["hot_cues"], [])
        self.assertNotIn("kept_locked", plan)


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.api = Api(self.store)
        self.api.import_xml(STREAMING_XML, "rbexport.xml")

    def tearDown(self):
        self.tmp.cleanup()

    def test_end_to_end_run(self):
        result = self.api.pipeline(
            {
                "name": "Syd Bday Ultimate",
                "directory": self.tmp.name,
                "filter": {"require_file": False},
                "build": {"template": "peak_time", "length_tracks": 1, "seed": 2, "iterations": 50},
            }
        )
        self.assertEqual(result["filter"]["kept"], 1)
        self.assertEqual(result["stats"]["count"], 1)
        self.assertEqual(result["cues"]["analysed"], 1)
        self.assertEqual(result["export"]["filename"], "Syd-Bday-Ultimate.zip")
        self.assertTrue(Path(result["export"]["saved_to"]).is_file())
        self.assertTrue(result["order"][0]["cues"]["analysed"])

    def test_a_filter_that_keeps_nothing_is_explained(self):
        with self.assertRaises(ValueError) as caught:
            self.api.pipeline({
                "filter": {"folder": "/nowhere", "require_file": False},
                "build": {"length_tracks": 1},
            })
        self.assertIn("Nothing left after filtering", str(caught.exception))
        self.assertIn("0", str(caught.exception))

    def test_pipeline_requires_a_library(self):
        empty = Api(Store(Path(self.tmp.name) / "other"))
        with self.assertRaises(ValueError):
            empty.pipeline({})

    def test_the_bundle_unzips_into_both_import_targets(self):
        import zipfile

        result = self.api.pipeline({
            "name": "Bundle Test",
            "directory": self.tmp.name,
            "filter": {"require_file": False},
            "build": {"length_tracks": 1, "seed": 3, "iterations": 40},
        })
        with zipfile.ZipFile(result["export"]["saved_to"]) as archive:
            names = set(archive.namelist())
            self.assertIn("Bundle-Test.xml", names)
            self.assertIn("Bundle-Test.autopilot.json", names)
            xml = archive.read("Bundle-Test.xml")
        self.assertIn(b"POSITION_MARK", xml)
        self.assertIn(b"DJ_PLAYLISTS", xml)


class PreviewAudioTest(unittest.TestCase):
    """The track preview: which files resolve, and the plan behind the markers."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.api = Api(self.store)
        self.api.import_xml(STREAMING_XML, "rbexport.xml")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_local_file_resolves(self):
        local = self.api.store.tracks_by_key()
        key = next(k for k, r in local.items() if not r.get("streaming"))
        with mock.patch("parity.server.Path.is_file", return_value=True):
            self.assertEqual(str(self.api.audio_path(key)), local[key]["path"])

    def test_a_streaming_track_has_nothing_to_preview(self):
        streaming = next(
            k for k, r in self.api.store.tracks_by_key().items() if r.get("streaming")
        )
        with self.assertRaises(ValueError) as caught:
            self.api.audio_path(streaming)
        self.assertIn("streaming", str(caught.exception).lower())

    def test_a_missing_file_is_reported(self):
        key = next(
            k for k, r in self.api.store.tracks_by_key().items() if not r.get("streaming")
        )
        with self.assertRaises(ValueError) as caught:
            self.api.audio_path(key)  # the fixture paths do not exist on this machine
        self.assertIn("No file at", str(caught.exception))

    def test_an_unknown_key_is_refused(self):
        with self.assertRaises(ValueError):
            self.api.audio_path("not-a-track")

    def test_the_cue_plan_gives_the_player_jump_targets(self):
        key = next(k for k, r in self.api.store.tracks_by_key().items() if not r.get("streaming"))
        empty = self.api.cue_plan(key)
        self.assertFalse(empty["analysed"])
        self.assertEqual(empty["hot_cues"], [])

        self.api.analyse({"keys": [key]})
        plan = self.api.cue_plan(key)
        self.assertTrue(plan["analysed"])
        self.assertEqual([cue["slot"] for cue in plan["hot_cues"]], list("ABCDEFGH"))
        self.assertEqual(plan["hot_cues"][0]["seconds"], round(plan["hot_cues"][0]["seconds"], 3))
        self.assertLess(plan["hot_cues"][0]["seconds"], plan["hot_cues"][7]["seconds"])


class StoreMigrationTest(unittest.TestCase):
    """A library written by an older Parity picks up new derived fields."""

    def test_old_records_are_repaired_from_the_stored_xml(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        store = Store(directory)
        store.set_library(
            rekordbox.parse_collection(STREAMING_XML), STREAMING_XML, source="rbexport.xml"
        )

        data = json.loads(store.path.read_text())
        data["library"].pop("schema", None)
        for record in data["library"]["tracks"].values():
            record.pop("streaming", None)
            record.pop("location", None)
        store.path.write_text(json.dumps(data))

        repaired = Store(directory)
        records = repaired.tracks_by_key()
        self.assertIn("streaming", next(iter(records.values())))
        cloud = next(record for record in records.values() if record["title"] == "Cloud Edit")
        self.assertTrue(cloud["streaming"])
        self.assertEqual(repaired.library()["source"], "rbexport.xml")

    def test_already_current_records_are_left_alone(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        store = Store(directory)
        store.set_library(rekordbox.parse_collection(STREAMING_XML), STREAMING_XML, source="rbexport.xml")
        before = store.path.read_text()
        self.assertEqual(Store(directory).path.read_text(), before)

    def test_encrypted_streaming_names_are_repaired_in_place(self):
        """A library imported before the check existed is fixed on the next
        launch, without re-importing over the user's own edits."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        store = Store(directory)
        store.set_library(
            rekordbox.parse_collection(STREAMING_XML), STREAMING_XML, source="rbexport.xml"
        )

        data = json.loads(store.path.read_text())
        records = data["library"]["tracks"]
        cloud = next(record for record in records.values() if record["title"] == "Cloud Edit")
        cloud["title"] = "$A7:v1:PDHdijBIBsu1CSnE7Edt5g==:L89UTZ4YMWvHjboAFGhA5A=="
        cloud["artist"] = "$A7:v1:tDdgm1yDnW+JT+gJNB5wnQ==:7M9kOmgGGZBq1ZDHhX7OejeXLs8JnOxIYDo+z/ym+mI="
        cloud.pop("metadata_locked", None)
        local = next(record for record in records.values() if record["title"] == "Local One")
        local["comments"] = "my own note"
        store.path.write_text(json.dumps(data))

        repaired = Store(directory)
        fixed = next(record for record in repaired.tracks_by_key().values() if record["id"] == cloud["id"])
        self.assertEqual(fixed["title"], "SoundCloud track 862894612")
        self.assertEqual(fixed["artist"], "")
        self.assertTrue(fixed["metadata_locked"])
        kept = next(record for record in repaired.tracks_by_key().values() if record["id"] == local["id"])
        self.assertEqual(kept["comments"], "my own note")

    def test_cue_plans_from_an_older_layout_are_dropped(self):
        """A four-cue plan written before the eight-cue layout went in must not
        be shown (or written) as if it were current."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        store = Store(directory)
        store.set_library(rekordbox.parse_collection(STREAMING_XML), STREAMING_XML, source="x")
        store.set_cue_plans({"old": {"hot_cues": [{"slot": "A"}]}, "new": {"planner": 2, "hot_cues": []}})

        reopened = Store(directory)
        self.assertEqual(list(reopened.cue_plans()), ["new"])
        self.assertEqual(reopened.stale_cue_plans, 1)
        # The startup reader and the server build separate Stores, so the count
        # has to survive a second load -- otherwise the UI is told nothing went
        # missing and just shows an empty cue list.
        later = Store(directory)
        self.assertEqual(later.stale_cue_plans, 1)
        self.assertEqual(list(later.cue_plans()), ["new"])
        self.assertEqual(Api(later).state()["stale_cue_plans"], 1)

    def test_fresh_plans_clear_the_analyse_again_note(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        store = Store(directory)
        store.set_cue_plans({"old": {"hot_cues": []}})
        # The next launch drops the old plan and records why.
        launched = Store(directory)
        self.assertEqual(launched.stale_cue_plans, 1)
        launched.set_cue_plans({"key": {"planner": 2, "hot_cues": []}})
        self.assertEqual(Store(directory).stale_cue_plans, 0)

    def test_a_fresh_store_has_nothing_stale_to_report(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.assertEqual(Store(Path(tmp.name)).stale_cue_plans, 0)


class IngestApiTest(unittest.TestCase):
    """The folder tidy-up endpoints: the plan, the commit, and the refusals.

    The conversion itself is covered in ``tests/test_ingest.py``; here the
    interest is the wiring -- what the plan reports, that the folder is
    remembered, and that a bad folder is an error the UI can show.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.api = Api(self.store)
        self.root = Path(self.tmp.name) / "staging"
        self.root.mkdir()
        (self.root / "01_Ada_Kaleh_-_Night_Ferry_🎵_[Official_Video]_320kbps.mp3").write_bytes(b"\x00" * 32)
        (self.root / "late night.wav").write_bytes(b"\x00" * 32)
        (self.root / "cover.jpg").write_bytes(b"\x00" * 32)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_scan_returns_a_plan_and_names_its_destination(self):
        data = self.api.ingest_scan({"source": str(self.root), "format": "wav", "measure": False})
        plan = data["plan"]
        self.assertEqual(plan["summary"]["found"], 2)
        self.assertEqual(plan["summary"]["renames"], 1)
        self.assertEqual(plan["destination"], str((self.root / "converted").resolve()))
        self.assertEqual(data["report"]["summary"]["planned"], 2)
        # A scan is a dry run: it reports the destination, it does not make it.
        self.assertFalse((self.root / "converted").exists())

    def test_the_folder_and_options_are_remembered(self):
        self.api.ingest_scan(
            {"source": str(self.root), "destination": str(self.root / "out"), "format": "wav"}
        )
        status = self.api.ingest_status()
        self.assertTrue(status["source_exists"])
        self.assertEqual(status["settings"]["format"], "wav")
        self.assertEqual(status["destination"], str(self.root / "out"))
        # A later request that names no folder reuses the stored one.
        again = self.api.ingest_scan({"measure": False})
        self.assertEqual(again["plan"]["root"], str(self.root.resolve()))

    def test_a_commit_copies_a_file_that_is_already_the_right_format(self):
        folder = Path(self.tmp.name) / "wavs"
        folder.mkdir()
        (folder / "Ada Kaleh - Night Ferry.wav").write_bytes(b"RIFF" + b"\x00" * 40)
        data = self.api.ingest_run({"source": str(folder), "format": "wav", "measure": False})
        report = data["report"]
        self.assertEqual(report["summary"]["copied"], 1)
        self.assertEqual(report["failed"], [])
        self.assertTrue((folder / "converted" / "Ada Kaleh - Night Ferry.wav").is_file())
        self.assertTrue((folder / "Ada Kaleh - Night Ferry.wav").is_file())

    def test_a_dry_run_commit_writes_nothing(self):
        folder = Path(self.tmp.name) / "wavs"
        folder.mkdir()
        (folder / "late night.wav").write_bytes(b"RIFF" + b"\x00" * 40)
        data = self.api.ingest_run(
            {"source": str(folder), "format": "wav", "measure": False, "dry_run": True}
        )
        self.assertTrue(data["report"]["dry_run"])
        self.assertFalse((folder / "converted").exists())

    def test_a_folder_that_is_not_there_is_a_message_not_a_crash(self):
        with self.assertRaises(ValueError) as caught:
            self.api.ingest_scan({"source": str(self.root / "nope")})
        self.assertIn("no folder", str(caught.exception).lower())

    def test_no_folder_at_all_is_refused(self):
        with self.assertRaises(ValueError):
            self.api.ingest_scan({})

    def test_an_unknown_format_is_refused(self):
        with self.assertRaises(ValueError):
            self.api.ingest_scan({"source": str(self.root), "format": "flac"})


class DuplicateApiTest(unittest.TestCase):
    """The duplicate scan and the removal plan, through the API."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.api = Api(self.store)
        self.api.import_xml(XML, "fixture")

    def tearDown(self):
        self.tmp.cleanup()

    def keys(self, count=3):
        return [track["key"] for track in self.api.tracks(None)["tracks"][:count]]

    def test_a_repeated_track_is_one_group(self):
        keys = self.keys()
        report = self.api.duplicates({"order": [keys[0], keys[0], keys[1]]})
        self.assertEqual(report["scope"], "set")
        self.assertEqual(report["group_count"], 1)
        self.assertEqual(report["duplicate_count"], 1)
        self.assertEqual(report["groups"][0]["kind"], "same_track")

    def test_removal_returns_the_order_to_apply(self):
        keys = self.keys()
        plan = self.api.remove_duplicates({"order": [keys[0], keys[1], keys[0]]})
        self.assertEqual(plan["order"], [keys[0], keys[1]])
        self.assertEqual(plan["removed_count"], 1)
        self.assertEqual(plan["removed"][0]["key"], keys[0])

    def test_the_pool_scope_reports_and_removes_nothing(self):
        report = self.api.duplicates({"keys": self.keys()})
        self.assertEqual(report["scope"], "pool")
        self.assertEqual(report["groups"], [])

    def test_an_empty_set_is_refused(self):
        with self.assertRaises(ValueError):
            self.api.remove_duplicates({"order": []})


def one_track_xml(path: str, *, streaming: bool = False) -> str:
    location = (
        "file://localhostsoundcloud:tracks:1"
        if streaming
        else "file://localhost" + urllib.parse.quote(path)
    )
    return (
        '<DJ_PLAYLISTS Version="1.0.0"><PRODUCT Name="rekordbox" Version="7.0.0"/>'
        '<COLLECTION Entries="1"><TRACK TrackID="1" Name="Old Name" Artist="Old Artist" '
        f'Genre="House" AverageBpm="122" Tonality="8A" Location="{location}"/>'
        '</COLLECTION><PLAYLISTS><NODE Type="0" Name="ROOT" Count="1">'
        '<NODE Name="P" Type="1" KeyType="0" Entries="1"><TRACK Key="1"/></NODE>'
        '</NODE></PLAYLISTS></DJ_PLAYLISTS>'
    )


class MetadataApiTest(unittest.TestCase):
    """The track edit endpoint: library record, and the file when asked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir / "data")
        self.api = Api(self.store)
        self.file = self.dir / "track.wav"
        with wave.open(str(self.file), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(44100)
            handle.writeframes(b"\x00\x00" * 22050)
        self.api.import_xml(one_track_xml(str(self.file)), "fixture")
        self.key = next(iter(self.store.tracks_by_key()))

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_edit_changes_the_view_and_survives_a_reload(self):
        result = self.api.edit_track(
            {"key": self.key, "fields": {"title": "New Name", "artist": "New Artist"}}
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["track"]["title"], "New Name")
        self.assertEqual(result["track"]["artist"], "New Artist")
        # The other records are untouched.
        self.assertEqual(result["track"]["genre"], "House")
        stored = Store(self.dir / "data").tracks_by_key()[self.key]
        self.assertEqual(stored["title"], "New Name")

    def test_bpm_and_key_edits_feed_the_harmonic_view(self):
        result = self.api.edit_track(
            {"key": self.key, "fields": {"bpm": "128", "key": "9A"}}
        )
        self.assertEqual(result["track"]["bpm"], 128.0)
        self.assertEqual(result["track"]["camelot"], "9A")
        self.assertEqual(result["track"]["tonality"], "Em")

    def test_an_unknown_field_is_refused(self):
        with self.assertRaises(ValueError):
            self.api.edit_track({"key": self.key, "fields": {"titel": "typo"}})

    def test_an_unknown_track_is_refused(self):
        with self.assertRaises(ValueError):
            self.api.edit_track({"key": "nope", "fields": {"title": "X"}})

    def test_streaming_tracks_cannot_write_tags(self):
        self.api.import_xml(one_track_xml("", streaming=True), "fixture")
        key = next(iter(self.store.tracks_by_key()))
        with self.assertRaises(ValueError):
            self.api.edit_track({"key": key, "fields": {"title": "X"}, "write_tags": True})

    @unittest.skipUnless(audio.status()["ffmpeg_ok"], "ffmpeg is not installed")
    def test_write_tags_reaches_the_file(self):
        result = self.api.edit_track(
            {
                "key": self.key,
                "fields": {"title": "Tagged Name", "artist": "Tagged Artist"},
                "write_tags": True,
            }
        )
        self.assertIsNotNone(result["tags"])
        from parity.ingest import probe

        tags = probe(self.file)["tags"]
        self.assertEqual(tags["title"], "Tagged Name")
        self.assertEqual(tags["artist"], "Tagged Artist")
        # The library record agrees with the file it just wrote.
        self.assertEqual(self.store.tracks_by_key()[self.key]["title"], "Tagged Name")


class PlaylistImportApiTest(unittest.TestCase):
    """The M3U import and watch-folder endpoints."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir / "data")
        self.api = Api(self.store)
        self.music = self.dir / "music"
        self.music.mkdir()
        for name in ("01_Ada_Kaleh_-_Night_Ferry.mp3", "Soha - Rivet.wav"):
            (self.music / name).write_bytes(b"not really audio")

    def tearDown(self):
        self.api._stop_watch()
        self.tmp.cleanup()

    def test_an_m3u_becomes_a_playlist_the_pool_can_use(self):
        result = self.api.import_m3u(
            {
                "text": "\n".join(
                    [
                        "#EXTM3U",
                        "#PLAYLIST:Friday",
                        f"#EXTINF:300,Ada Kaleh - Night Ferry",
                        str(self.music / "01_Ada_Kaleh_-_Night_Ferry.mp3"),
                        f"#EXTINF:300,Soha - Rivet",
                        str(self.music / "Soha - Rivet.wav"),
                    ]
                ),
                "name": "Friday",
            }
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["added"], 2)
        self.assertEqual(result["state"]["track_count"], 2)
        playlist = result["state"]["playlists"][0]
        self.assertEqual(playlist["name"], "Friday")
        self.assertEqual(playlist["count"], 2)
        self.assertEqual(len(result["state"]["folders"]), 1)

    def test_the_same_file_is_not_added_twice(self):
        path = str(self.music / "Soha - Rivet.wav")
        self.api.import_m3u({"text": path, "name": "One"})
        again = self.api.import_m3u({"text": path, "name": "Two"})
        self.assertEqual(again["added"], 0)
        self.assertEqual(again["matched"], 1)
        self.assertEqual(again["state"]["track_count"], 1)

    def test_the_watch_folder_imports_and_reports(self):
        saved = self.api.watch_configure(
            {"path": str(self.music), "enabled": False, "recursive": True}
        )
        self.assertFalse(saved["settings"]["enabled"])
        tick = self.api.watch_tick()
        self.assertEqual(tick["report"]["added"], 2)
        self.assertEqual(tick["status"]["seen"], 2)
        self.assertEqual(tick["state"]["track_count"], 2)
        again = self.api.watch_tick()
        self.assertEqual(again["report"]["added"], 0)

    def test_a_missing_watch_folder_is_refused(self):
        with self.assertRaises(ValueError):
            self.api.watch_configure({"path": str(self.dir / "nope"), "enabled": True})

    def test_enabling_starts_and_disabling_stops_the_background_thread(self):
        self.api.watch_configure({"path": str(self.music), "enabled": True})
        self.assertTrue(self.api.watch_status()["running"])
        self.api.watch_configure({"path": str(self.music), "enabled": False})
        self.assertFalse(self.api.watch_status()["running"])


if __name__ == "__main__":
    unittest.main()
