"""Tests for :mod:`parity.tags`.

The tag is written into a field the DJ owns and into an exported file, so the
two things that must never break are covered first: what is appended, and that
a re-write replaces Parity's own tag rather than stacking a second one.
"""

import unittest

from parity import tags


def track(key, energy, comments="", **extra):
    return {
        "key": key,
        "title": f"Track {key}",
        "artist": "A",
        "energy": energy,
        "comments": comments,
        **extra,
    }


class TagFormatTest(unittest.TestCase):
    def test_the_canonical_shape(self):
        self.assertEqual(tags.format_tag(7, "peak", 14, 24), "[E7] [peak] 14/24")

    def test_the_band_is_rounded_half_up_and_clamped(self):
        self.assertEqual(tags.energy_band(6.5), 7)
        self.assertEqual(tags.energy_band(6.4), 6)
        self.assertEqual(tags.energy_band(0.2), 1)
        self.assertEqual(tags.energy_band(99), 10)
        self.assertEqual(tags.energy_band(None), 5)
        self.assertEqual(tags.energy_band(float("nan")), 5)

    def test_an_unknown_role_falls_back_to_build(self):
        self.assertEqual(tags.format_tag(5, "warmup", 2, 3), "[E5] [build] 2/3")

    def test_a_tag_is_read_back(self):
        found = tags.parse_tag("dark [E7] [peak] 14/24 edit")
        self.assertEqual(found["energy"], 7)
        self.assertEqual(found["role"], "peak")
        self.assertEqual(found["position"], 14)
        self.assertEqual(found["total"], 24)
        self.assertEqual(found["text"], "[E7] [peak] 14/24")

    def test_spacing_survives_a_round_trip_through_a_text_field(self):
        self.assertIsNotNone(tags.parse_tag("[ E 7 ] [ Peak ] 3 / 8"))

    def test_partial_tags_are_not_mistaken_for_ours(self):
        self.assertIsNone(tags.parse_tag("no tag here"))
        self.assertIsNone(tags.parse_tag("[E7] peak"))
        self.assertIsNone(tags.parse_tag("[E7] [peak]"))
        self.assertIsNone(tags.parse_tag("[E7] [warmup] 1/2"))

    def test_the_tag_is_the_tag_parity_reads_back_as_energy(self):
        """The [E7] Parity writes is the [E7] it parses out of a comment."""
        from parity import energy

        self.assertEqual(energy.tag_energy(tags.format_tag(7, "peak", 1, 2)), 7.0)


class CommentMergeTest(unittest.TestCase):
    def test_a_comment_the_user_typed_is_kept(self):
        self.assertEqual(
            tags.merge_comment("banger edit", "[E7] [peak] 1/2"),
            "banger edit [E7] [peak] 1/2",
        )

    def test_an_empty_comment_is_just_the_tag(self):
        self.assertEqual(tags.merge_comment("", "[E7] [build] 1/2"), "[E7] [build] 1/2")
        self.assertEqual(tags.merge_comment("   ", "[E7] [build] 1/2"), "[E7] [build] 1/2")

    def test_a_previous_tag_is_replaced_not_stacked(self):
        merged = tags.merge_comment("dark [E5] [build] 2/3", "[E9] [closer] 3/3")
        self.assertEqual(merged, "dark [E9] [closer] 3/3")
        self.assertEqual(merged.count("[E"), 1)

    def test_another_tools_brackets_are_left_alone(self):
        self.assertEqual(
            tags.merge_comment("[STAR] peak time", "[E5] [build] 1/1"),
            "[STAR] peak time [E5] [build] 1/1",
        )

    def test_stripping_removes_only_our_tag(self):
        self.assertEqual(tags.strip_tag("kept [E7] [peak] 1/2 more"), "kept more")
        self.assertEqual(tags.strip_tag("[E7] [peak] 1/2"), "")
        self.assertEqual(tags.strip_tag("untouched"), "untouched")


class RoleTest(unittest.TestCase):
    def role(self, position, total, energy, peak):
        return tags.role_for(position, total, energy, peak)[0]

    def test_the_first_track_opens_and_the_last_closes(self):
        self.assertEqual(self.role(1, 10, 3.0, 9.5), "opener")
        self.assertEqual(self.role(10, 10, 3.2, 9.5), "closer")

    def test_a_middle_track_near_the_peak_is_a_peak(self):
        self.assertEqual(self.role(5, 10, 9.2, 9.5), "peak")
        self.assertEqual(self.role(5, 10, 8.9, 9.5), "build")

    def test_a_single_track_set_opens(self):
        self.assertEqual(self.role(1, 1, 9.0, 9.0), "opener")

    def test_the_reason_says_which_rule_fired(self):
        role, reason = tags.role_for(1, 10, 4.0, 9.0)
        self.assertEqual(role, "opener")
        self.assertIn("opens", reason)
        role, reason = tags.role_for(5, 10, 9.0, 9.0)
        self.assertEqual(role, "peak")
        self.assertIn("9", reason)
        role, reason = tags.role_for(5, 10, 4.0, 9.0)
        self.assertEqual(role, "build")
        self.assertTrue(reason)


class PlanTest(unittest.TestCase):
    def test_every_track_gets_a_tag_in_set_order(self):
        plan = tags.plan_tags(
            [track("a", 4.0), track("b", 9.4, "dark edit"), track("c", 7.0)]
        )
        self.assertEqual([plan[key]["position"] for key in plan], [1, 2, 3])
        self.assertEqual(plan["a"]["tag"], "[E4] [opener] 1/3")
        self.assertEqual(plan["b"]["tag"], "[E9] [peak] 2/3")
        self.assertEqual(plan["c"]["tag"], "[E7] [closer] 3/3")
        self.assertEqual(plan["b"]["after"], "dark edit [E9] [peak] 2/3")
        self.assertEqual(plan["b"]["label"], "A - Track b")
        self.assertEqual(plan["a"]["total"], 3)

    def test_a_track_tagged_for_an_earlier_set_says_so(self):
        plan = tags.plan_tags([track("a", 4.0, "old [E6] [peak] 3/9")])
        self.assertEqual(plan["a"]["previous"]["energy"], 6)
        self.assertEqual(plan["a"]["before"], "old [E6] [peak] 3/9")
        self.assertEqual(plan["a"]["after"], "old [E4] [opener] 1/1")

    def test_a_track_with_no_key_is_left_out(self):
        self.assertEqual(tags.plan_tags([{"title": "x"}]), {})

    def test_an_empty_set_has_no_plan(self):
        self.assertEqual(tags.plan_tags([]), {})

    def test_the_report_counts_roles_and_replacements(self):
        plan = tags.plan_tags(
            [track("a", 4.0, "old [E6] [closer] 3/3"), track("b", 9.4), track("c", 7.0)]
        )
        report = tags.tag_report(plan, written=True)
        self.assertTrue(report["written"])
        self.assertEqual(report["count"], 3)
        self.assertEqual(report["roles"], {"opener": 1, "peak": 1, "closer": 1})
        self.assertEqual(report["replaced"], 1)
        self.assertEqual([row["position"] for row in report["rows"]], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
