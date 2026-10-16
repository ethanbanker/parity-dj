"""The direct-write endpoints, and the API methods behind them.

The writers themselves are covered in ``test_writedb``/``test_autopilot``.  What
matters here is the wiring: that a dry run is the default, that the set order and
cue plans reach the writer, and that a running app comes back as a 409 rather
than a broken request.
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from parity import autopilot, writedb
from parity.server import Api, serve
from parity.store import Store
from tests.test_server import XML, find_playlist


def report(dry_run=True, **extra):
    """A report with the shape the writers return."""
    data = {
        "database": "/tmp/master.db",
        "dry_run": dry_run,
        "backup": None,
        "cue_policy": "overwrite",
        "cues": {
            "created": 0,
            "replaced": 0,
            "tracks": 0,
            "memory": 0,
            "rows": [],
            "skipped": [],
            "kept": [],
            "policy": "overwrite",
        },
        "tags": {
            "changed": 0,
            "unchanged": 0,
            "rows": [],
            "skipped": [],
            "over_limit": [],
        },
        "playlist": None,
        "warnings": [],
    }
    data.update(extra)
    return data


def a_set(api):
    """A built set from the fixture library, with cues analysed."""
    playlist_id = find_playlist(api.state()["playlists"], "Friday Set")["id"]
    return api.build(
        {
            "playlist_id": playlist_id,
            "config": {"length_tracks": 5, "seed": 2, "iterations": 400},
        }
    )


class WriteApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name))
        self.api = Api(self.store)
        self.api.import_xml(XML, "fixture")
        self.result = a_set(self.api)

    # -- defaults ---------------------------------------------------------

    def test_a_write_is_a_dry_run_unless_it_says_otherwise(self):
        seen = {}

        def fake(order, cue_plans=None, **kwargs):
            seen["order"] = order
            seen["kwargs"] = kwargs
            return report(dry_run=kwargs.get("dry_run"))

        with mock.patch.object(writedb, "write_set", fake):
            self.api.write_rekordbox({"order": self.result["order_keys"], "name": "Friday Set"})
        self.assertTrue(seen["kwargs"]["dry_run"])
        self.assertEqual([track["key"] for track in seen["order"]], self.result["order_keys"])
        self.assertEqual(seen["kwargs"]["playlist"], "Friday Set")

    def test_the_cue_plans_travel_with_the_order(self):
        self.api.analyse({"keys": self.result["order_keys"]})
        seen = {}
        def fake(order, plans=None, **kw):
            seen["plans"] = plans
            return report()

        with mock.patch.object(writedb, "write_set", fake):
            self.api.write_rekordbox(
                {"order": self.result["order_keys"], "name": "Friday Set", "dry_run": True}
            )
        planned = [key for key, plan in seen["plans"].items() if plan.get("hot_cues")]
        self.assertTrue(planned)
        self.assertTrue(set(planned).issubset(set(self.result["order_keys"])))

    def test_cues_can_be_left_out(self):
        seen = {}
        def fake(order, plans=None, **kw):
            seen["plans"] = plans
            return report()

        with mock.patch.object(writedb, "write_set", fake):
            self.api.write_rekordbox(
                {"order": self.result["order_keys"], "cues": False, "dry_run": True}
            )
        self.assertEqual(seen["plans"], {})

    def test_no_playlist_name_means_order_only(self):
        seen = {}
        def fake(order, plans=None, **kw):
            seen.update(kw)
            return report()

        with mock.patch.object(writedb, "write_set", fake):
            self.api.write_rekordbox({"order": self.result["order_keys"], "dry_run": True})
        self.assertEqual(seen["playlist"], "")

    # -- existing cues ----------------------------------------------------

    def test_the_cue_policy_reaches_the_writer(self):
        seen = {}

        def fake(order, plans=None, **kw):
            seen.update(kw)
            return report()

        with mock.patch.object(writedb, "write_set", fake):
            self.api.set_cue_policy({"policy": "keep"})
            self.api.write_rekordbox({"order": self.result["order_keys"], "dry_run": True})
        self.assertEqual(seen["cue_policy"], "keep")
        self.assertTrue(all(value == "keep" for value in seen["cue_policies"].values()))

    def test_a_per_track_choice_beats_the_global_one(self):
        seen = {}

        def fake(order, plans=None, **kw):
            seen.update(kw)
            return report()

        key = self.result["order_keys"][0]
        with mock.patch.object(writedb, "write_set", fake):
            self.api.set_cue_policy({"policy": "keep", "values": {key: "overwrite"}})
            self.api.write_rekordbox({"order": self.result["order_keys"], "dry_run": True})
        self.assertEqual(seen["cue_policies"][key], "overwrite")
        self.assertEqual(seen["cue_policies"][self.result["order_keys"][1]], "keep")

    def test_keep_sends_only_the_users_marks_for_a_track_they_cued(self):
        """The XML carries their cues and none of ours, so a re-import leaves
        those marks exactly where they are."""
        keys = self.result["order_keys"][:2]
        key = keys[0]
        self.api.analyse({"keys": keys})
        records = self.api.store.tracks_by_key()
        records[key]["cues"] = [
            {"num": 0, "name": "My drop", "start": 12.5, "kind": "hot", "source": "rekordbox"}
        ]
        self.api.set_cue_policy({"policy": "keep"})
        plans = self.api.export_cue_plans(keys)
        kept = plans[key]
        self.assertEqual(kept["hot_cues"], [])
        self.assertTrue(kept["kept"])
        self.assertEqual([cue["name"] for cue in kept["existing"]], ["My drop"])
        # The other track is cued as usual, with the eight pads and a beat grid.
        self.assertEqual(len(plans[keys[1]]["hot_cues"]), 8)
        self.assertIn("beat_grid", plans[keys[1]])

    def test_keep_does_not_drop_a_plan_for_paritys_own_cues(self):
        """Otherwise choosing keep would freeze the library on the first write."""
        key = self.result["order_keys"][0]
        self.api.analyse({"keys": [key]})
        records = self.api.store.tracks_by_key()
        records[key]["cues"] = [
            {"num": 0, "name": "First Downbeat", "start": 1.5, "kind": "hot", "source": "db"},
            {"num": 3, "name": "Main Drop", "start": 30.0, "kind": "hot", "source": "db"},
        ]
        self.api.set_cue_policy({"policy": "keep"})
        plans = self.api.export_cue_plans([key])
        self.assertIn(key, plans)

    # -- tags and pre-flight ----------------------------------------------

    def test_tags_reach_the_writer_only_when_they_are_asked_for(self):
        seen = {}

        def fake(order, plans=None, **kw):
            seen.clear()
            seen.update(kw)
            return report()

        with mock.patch.object(writedb, "write_set", fake):
            self.api.write_rekordbox({"order": self.result["order_keys"], "dry_run": True})
        self.assertEqual(seen["tags"], {})

        with mock.patch.object(writedb, "write_set", fake):
            self.api.write_rekordbox(
                {"order": self.result["order_keys"], "dry_run": True, "tags": True}
            )
        keys = [track["key"] for track in self.result["order"]]
        self.assertEqual(sorted(seen["tags"]), sorted(keys))
        self.assertTrue(seen["tags"][keys[0]].startswith("[E"))
        self.assertIn("[opener]", seen["tags"][keys[0]])
        self.assertIn(f"1/{len(keys)}", seen["tags"][keys[0]])

    def test_the_tag_plan_comes_back_in_the_write_report(self):
        with mock.patch.object(writedb, "write_set", lambda *a, **kw: report()):
            result = self.api.write_rekordbox(
                {"order": self.result["order_keys"], "dry_run": True, "tags": True}
            )
        plan = result["tag_plan"]
        self.assertEqual(plan["count"], len(self.result["order_keys"]))
        self.assertEqual([row["position"] for row in plan["rows"]], list(range(1, len(plan["rows"]) + 1)))
        self.assertEqual(plan["rows"][0]["role"], "opener")
        self.assertEqual(plan["rows"][-1]["role"], "closer")
        self.assertTrue(plan["dry_run"])

    def test_no_tag_plan_when_the_tags_are_off(self):
        with mock.patch.object(writedb, "write_set", lambda *a, **kw: report()):
            result = self.api.write_rekordbox({"order": self.result["order_keys"], "dry_run": True})
        self.assertIsNone(result["tag_plan"])

    def test_the_write_report_carries_the_preflight_check(self):
        with mock.patch.object(writedb, "write_set", lambda *a, **kw: report()):
            result = self.api.write_rekordbox({"order": self.result["order_keys"], "dry_run": True})
        check = result["preflight"]
        self.assertEqual(check["checked"], len(self.result["order_keys"]))
        # The fixture's audio paths are the export machine's, so they are gone.
        self.assertEqual(check["summary"]["missing_file"], len(self.result["order_keys"]))
        self.assertFalse(check["ok"])

    def test_the_preflight_endpoint_checks_the_set_it_is_given(self):
        keys = self.result["order_keys"]
        report_data = self.api.preflight({"order": keys[:2], "name": "Friday Set"})
        self.assertEqual(report_data["checked"], 2)
        self.assertIn("Friday Set", report_data["text"])

        # An empty set is not an error, it is simply nothing to check.
        empty = self.api.preflight({"order": []})
        self.assertEqual(empty["checked"], 0)
        self.assertEqual(empty["issues"], [])

    # -- library management -----------------------------------------------

    def test_deleting_a_playlist_reaches_the_rekordbox_writer_as_a_dry_run(self):
        seen = {}

        def fake(value, **kw):
            seen["value"] = value
            seen.update(kw)
            return report(action="delete_playlist", removed=3)

        playlist_id = find_playlist(self.api.state()["playlists"], "Friday Set")["id"]
        self.assertTrue(playlist_id.startswith("pl:"), playlist_id)
        with mock.patch.object(writedb, "delete_playlist", fake):
            self.api.library_playlist("delete", {"target": "rekordbox", "id": playlist_id})
        # The loaded library names its playlists "pl:<ID>" (see parity.rxdb),
        # which is what the UI hands back; master.db wants the bare id.
        self.assertEqual(seen["value"], playlist_id.split(":", 1)[1])
        self.assertTrue(seen["dry_run"])

    def test_a_bare_playlist_id_or_a_name_reaches_the_writer_untouched(self):
        seen = []

        def fake(value, **kw):
            seen.append(value)
            return report(action="delete_playlist")

        with mock.patch.object(writedb, "delete_playlist", fake):
            self.api.library_playlist("delete", {"target": "rekordbox", "id": "42"})
            self.api.library_playlist("delete", {"target": "rekordbox", "id": "Syd Bday"})
        self.assertEqual(seen, ["42", "Syd Bday"])

    def test_renaming_a_playlist_passes_the_new_name_on(self):
        seen = {}

        def fake(value, name, **kw):
            seen["value"], seen["name"], seen["kwargs"] = value, name, kw
            return report(action="rename_playlist")

        with mock.patch.object(writedb, "rename_playlist", fake):
            self.api.library_playlist(
                "rename", {"target": "rekordbox", "id": "42", "name": "Saturday Set", "dry_run": False}
            )
        self.assertEqual((seen["value"], seen["name"]), ("42", "Saturday Set"))
        self.assertFalse(seen["kwargs"]["dry_run"])

    def test_the_autopilot_target_goes_to_the_autopilot_writer(self):
        seen = {}

        def fake(playlist_id, **kw):
            seen["id"] = playlist_id
            seen.update(kw)
            return report(action="delete_playlist")

        with mock.patch.object(autopilot, "delete_playlist", fake):
            self.api.library_playlist("delete", {"target": "autopilot", "id": 7})
        self.assertEqual(seen["id"], 7)
        self.assertTrue(seen["dry_run"])

    def test_a_committed_change_re_reads_the_library(self):
        """The manage panel lists playlists from the *stored* library, so a
        committed delete has to refresh it -- otherwise the playlist the user
        just removed keeps showing up until the next launch."""
        reads = []

        def fake(value, **kw):
            return report(dry_run=kw.get("dry_run"))

        with mock.patch.object(writedb, "delete_playlist", fake):
            with mock.patch.object(self.store, "refresh_library", lambda: reads.append(1)):
                self.api.library_playlist("delete", {"target": "rekordbox", "id": "42"})
                self.assertEqual(reads, [])
                self.api.library_playlist(
                    "delete", {"target": "rekordbox", "id": "42", "dry_run": False}
                )
        self.assertEqual(len(reads), 1)

    def test_deleting_tracks_resolves_parity_keys_to_library_ids(self):
        seen = {}

        def fake(content_ids, **kw):
            seen["ids"] = list(content_ids)
            seen.update(kw)
            return report(action="delete_tracks", removed=len(content_ids))

        records = self.api.store.tracks_by_key()
        keys = self.result["order_keys"][:2]
        with mock.patch.object(writedb, "delete_tracks", fake):
            report_out = self.api.library_delete_tracks(
                {"target": "rekordbox", "keys": keys, "dry_run": True}
            )
        self.assertEqual(seen["ids"], [str(records[key]["id"]) for key in keys])
        self.assertEqual(report_out["removed"], 2)

    def test_removing_tracks_from_a_playlist_uses_the_content_ids(self):
        seen = {}

        def fake(value, content_ids, **kw):
            seen["value"] = value
            seen["ids"] = list(content_ids)
            return report(action="remove_playlist_tracks", removed=1)

        records = self.api.store.tracks_by_key()
        key = self.result["order_keys"][0]
        with mock.patch.object(writedb, "remove_playlist_tracks", fake):
            self.api.library_remove_tracks(
                {"target": "rekordbox", "id": "42", "keys": [key], "dry_run": True}
            )
        self.assertEqual(seen["value"], "42")
        self.assertEqual(seen["ids"], [str(records[key]["id"])])

    def test_an_unknown_library_is_refused(self):
        with self.assertRaises(ValueError):
            self.api.library_playlist("delete", {"target": "itunes", "id": 1})

    def test_the_library_endpoint_lists_both_sides(self):
        listing = self.api.library()
        self.assertTrue(listing["loaded"])
        self.assertTrue(listing["rekordbox"]["playlists"])
        self.assertIn("path", listing["autopilot"])

    def test_an_empty_order_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.api.write_rekordbox({"order": []})
        self.assertIn("build a set first", str(caught.exception).lower())

    def test_status_reports_both_libraries(self):
        status = self.api.write_status()
        self.assertIn("rekordbox", status)
        self.assertIn("autopilot", status)
        self.assertIn("path", status["rekordbox"])
        self.assertIn("path", status["autopilot"])

    def test_a_blocked_write_is_reported_without_touching_anything(self):
        with mock.patch.object(
            writedb,
            "assert_closed",
            side_effect=writedb.WriteBlocked(
                "rekordbox is running (pid 5). Quit rekordbox completely, then run the write again."
            ),
        ):
            with self.assertRaises(writedb.WriteBlocked):
                self.api.write_rekordbox(
                    {"order": self.result["order_keys"], "name": "Friday Set", "dry_run": False}
                )


class WriteHttpTest(unittest.TestCase):
    """The HTTP surface: a running app must come back as 409, not 500."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name))
        self.api = Api(self.store)
        self.api.import_xml(XML, "fixture")
        self.result = a_set(self.api)
        self.httpd = serve("127.0.0.1", 0, Path(self.tmp.name))
        self.httpd.Api = self.api  # type: ignore[attr-defined]
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def post(self, route, payload):
        request = urllib.request.Request(
            self.base + route,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_a_blocked_write_is_409_with_the_reason(self):
        # The "an app is open" guard is only reached once a database is there to
        # write to -- with none, the answer is "No rekordbox database" -- so this
        # brings its own rather than needing rekordbox installed on the machine
        # running the tests.
        database = Path(self.tmp.name) / "master.db"
        database.write_bytes(b"db")
        with mock.patch.dict(os.environ, {"PARITY_MASTER_DB": str(database)}), mock.patch.object(
            writedb,
            "assert_closed",
            side_effect=writedb.WriteBlocked(
                "rekordbox is running (pid 5). Quit rekordbox completely, then run the write again."
            ),
        ):
            status, body = self.post(
                "/api/write/rekordbox",
                {"order": self.result["order_keys"], "name": "Friday Set", "dry_run": False},
            )
        self.assertEqual(status, 409)
        self.assertTrue(body["blocked"])
        self.assertIn("Quit rekordbox", body["error"])

    def test_a_missing_database_is_409_too(self):
        status, body = self.post(
            "/api/write/rekordbox",
            {
                "order": self.result["order_keys"],
                "name": "Friday Set",
                "dry_run": False,
                "db_path": str(Path(self.tmp.name) / "nowhere" / "master.db"),
            },
        )
        self.assertEqual(status, 409)
        self.assertIn("No rekordbox database", body["error"])

    def test_the_status_endpoint_answers(self):
        with urllib.request.urlopen(self.base + "/api/write/status", timeout=10) as response:
            body = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertIn("rekordbox", body)


if __name__ == "__main__":
    unittest.main()
