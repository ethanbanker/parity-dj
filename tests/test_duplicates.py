import unittest

from parity import duplicates


def track(key, title=None, artist="Soha", path=None, **extra):
    row = {
        "key": key,
        "title": title if title is not None else f"Track {key}",
        "artist": artist,
        "path": path if path is not None else f"/music/{key}.mp3",
        "rating": 0,
        "cues": {"analysed": False},
        "existing_cues": {"hot": 0},
        "streaming": False,
    }
    row.update(extra)
    return row


class FindDuplicatesTest(unittest.TestCase):
    def test_one_file_two_rows_is_one_group(self):
        rows = [
            track("a", path="/music/rivet.mp3"),
            track("b", path="/music/rivet.mp3"),
            track("c", path="/music/other.mp3", title="Other"),
        ]
        groups = duplicates.find_duplicates(rows)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["kind"], "same_file")
        self.assertEqual(groups[0]["positions"], [1, 2])
        self.assertEqual(groups[0]["keep_position"], 1)
        self.assertEqual(groups[0]["remove_positions"], [2])

    def test_same_key_twice_in_a_set(self):
        rows = [track("a"), track("a"), track("b")]
        groups = duplicates.find_duplicates(rows)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["kind"], "same_track")
        self.assertEqual(groups[0]["remove_positions"], [2])

    def test_same_artist_and_title_on_different_files(self):
        rows = [
            track("a", path="/music/one.mp3", title="Rivet"),
            track("b", path="/music/two.wav", title="Rivet"),
            track("c", title="Another", path="/music/three.mp3"),
        ]
        groups = duplicates.find_duplicates(rows)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["kind"], "same_name")

    def test_one_problem_is_not_reported_twice(self):
        # Same path *and* same name: one group, with the stronger reason.
        rows = [
            track("a", path="/music/one.mp3"),
            track("b", path="/music/one.mp3"),
        ]
        groups = duplicates.find_duplicates(rows)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["kind"], "same_file")

    def test_transitive_duplicates_make_one_group(self):
        rows = [
            track("a", path="/music/one.mp3", title="Rivet"),
            track("b", path="/music/one.mp3", title="Different name"),
            track("c", path="/music/two.mp3", title="Rivet"),
        ]
        groups = duplicates.find_duplicates(rows)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["tracks"]), 3)

    def test_unnamed_tracks_are_not_a_duplicate_of_each_other(self):
        rows = [
            track("a", title="", artist="", path="/music/a.mp3"),
            track("b", title="", artist="", path="/music/b.mp3"),
        ]
        self.assertEqual(duplicates.find_duplicates(rows), [])

    def test_streaming_entries_do_not_share_a_file(self):
        rows = [
            track("a", path="", streaming=True),
            track("b", path="", streaming=True),
        ]
        self.assertEqual(duplicates.find_duplicates(rows), [])

    def test_a_streaming_entry_and_its_download_are_the_same_name(self):
        rows = [
            track("a", path="", streaming=True, title="Rivet"),
            track("b", path="/music/rivet.mp3", title="Rivet"),
        ]
        groups = duplicates.find_duplicates(rows)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["kind"], "same_name")
        self.assertEqual(groups[0]["keep_position"], 2)

    def test_keep_prefers_a_local_named_cued_track(self):
        rows = [
            track("a", path="", streaming=True, title="Rivet"),
            track("b", path="/music/rivet.mp3", title="Rivet"),
        ]
        rows[1]["cues"] = {"analysed": True}
        group = duplicates.find_duplicates(rows)[0]
        self.assertEqual(group["keep_position"], 2)
        self.assertTrue(group["tracks"][1]["kept"])


class RemovePlanTest(unittest.TestCase):
    def rows(self):
        return [
            track("a", path="/music/one.mp3"),
            track("b", path="/music/two.mp3"),
            track("c", path="/music/one.mp3"),
            track("d", path="/music/three.mp3", title="Other"),
        ]

    def test_removal_keeps_the_recommended_row(self):
        plan = duplicates.plan_removal(self.rows())
        self.assertEqual(plan["order"], ["a", "b", "d"])
        self.assertEqual(plan["removed_count"], 1)
        self.assertEqual(plan["removed"][0]["key"], "c")
        self.assertEqual(plan["kept"][0]["position"], 1)

    def test_a_chosen_row_can_replace_the_recommendation(self):
        rows = self.rows()
        group = duplicates.find_duplicates(rows)[0]
        plan = duplicates.plan_removal(rows, {group["id"]: 3})
        self.assertEqual(plan["order"], ["b", "c", "d"])
        self.assertEqual(plan["removed"][0]["key"], "a")

    def test_a_stale_choice_cannot_remove_the_wrong_row(self):
        rows = self.rows()
        group = duplicates.find_duplicates(rows)[0]
        plan = duplicates.plan_removal(rows, {group["id"]: 99})
        self.assertEqual(plan["order"], ["a", "b", "d"])

    def test_nothing_to_do_returns_the_same_order(self):
        rows = [track("a"), track("b", title="Other")]
        plan = duplicates.plan_removal(rows)
        self.assertEqual(plan["order"], ["a", "b"])
        self.assertEqual(plan["removed"], [])

    def test_report_counts_every_removable_row(self):
        rows = self.rows() + [track("e", path="/music/one.mp3")]
        report = duplicates.report(rows)
        self.assertEqual(report["checked"], 5)
        self.assertEqual(report["group_count"], 1)
        self.assertEqual(report["duplicate_count"], 2)
