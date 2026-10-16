import unittest
from pathlib import Path

from parity import structure
from parity.rekordbox import Track


def beat_grid(bpm=130.0, beats=200):
    """A plain 4/4 grid so beat N is at a predictable millisecond."""
    step = 60000.0 / bpm
    return [int(round(index * step)) for index in range(beats)]


def structure_with_phrases(bpm=130.0, beats=200):
    grid = beat_grid(bpm, beats)
    item = structure.Structure(
        bpm=bpm,
        duration_ms=int(round((beats - 1) * 60000.0 / bpm)),
        beat_ms=grid,
        source="rekordbox-anlz",
        confidence="high",
    )
    plan = [
        (1, "Intro", 1, 33),
        (2, "Up", 33, 65),
        (5, "Chorus", 65, 97),
        (5, "Chorus", 97, 129),
        (3, "Down", 129, 161),
        (2, "Up", 161, 193),
        (6, "Outro", 193, 200),
    ]
    for index, (kind, name, start, end) in enumerate(plan, start=1):
        item.phrases.append(
            structure.Phrase(
                index=index,
                kind=kind,
                name=name,
                start_beat=start,
                end_beat=end,
                start_ms=item.beat_time(start),
                end_ms=item.beat_time(end),
            )
        )
    return item


class PhraseNameTest(unittest.TestCase):
    def test_kinds_map_to_rekordbox_names(self):
        self.assertEqual(structure.PHRASE_KINDS[1], "Intro")
        self.assertEqual(structure.PHRASE_KINDS[2], "Up")
        self.assertEqual(structure.PHRASE_KINDS[3], "Down")
        self.assertEqual(structure.PHRASE_KINDS[5], "Chorus")
        self.assertEqual(structure.PHRASE_KINDS[6], "Outro")


class BeatTimeTest(unittest.TestCase):
    def test_beat_one_is_the_first_grid_entry(self):
        item = structure.Structure(bpm=130.0, beat_ms=[461, 922, 1384])
        self.assertEqual(item.beat_time(1), 461)
        self.assertEqual(item.beat_time(3), 1384)

    def test_beats_past_the_grid_are_extrapolated(self):
        item = structure.Structure(bpm=120.0, beat_ms=[100, 600])
        # 500 ms per beat at 120 BPM, so beat 4 is 100 + 3 * 500.
        self.assertEqual(item.beat_time(4), 1600)

    def test_without_a_grid_it_uses_the_tempo(self):
        item = structure.Structure(bpm=120.0)
        self.assertEqual(item.beat_time(1), 0)
        self.assertEqual(item.beat_time(3), 1000)

    def test_without_anything_it_does_not_crash(self):
        self.assertEqual(structure.Structure().beat_time(4), 0)


class HeuristicTest(unittest.TestCase):
    def test_shape_covers_the_track(self):
        item = structure.heuristic_structure(300_000, 130.0)
        self.assertEqual(item.source, "heuristic")
        self.assertEqual(item.confidence, "low")
        names = [phrase.name for phrase in item.phrases]
        self.assertEqual(names[:3], ["Intro", "Up", "Chorus"])
        self.assertEqual(names[-1], "Outro")
        self.assertEqual(len(item.phrases), 8)
        self.assertLess(item.phrases[0].start_ms, item.phrases[-1].start_ms)
        self.assertLessEqual(item.phrases[-1].end_ms, 300_000)

    def test_every_section_has_its_own_start(self):
        item = structure.heuristic_structure(210_000, 124.0)
        starts = [phrase.start_ms for phrase in item.phrases]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(len(set(starts)), len(starts))

    def test_a_short_track_still_spreads_eight_sections(self):
        item = structure.heuristic_structure(8_000, 128.0)
        starts = [phrase.start_ms for phrase in item.phrases]
        self.assertEqual(len(set(starts)), 8)
        self.assertLessEqual(starts[-1], 8_000)

    def test_cues_are_ordered_and_in_range(self):
        item = structure.heuristic_structure(300_000, 128.0)
        cues = structure.place_cues(item)
        self.assertEqual([cue["slot"] for cue in cues], list("ABCDEFGH"))
        seconds = [cue["seconds"] for cue in cues]
        self.assertEqual(seconds, sorted(seconds))
        self.assertLess(seconds[-1], 300)

    def test_no_duration_is_still_safe(self):
        cues = structure.place_cues(structure.heuristic_structure(0, 0))
        self.assertTrue(cues)
        self.assertTrue(all(cue["ms"] == 0 for cue in cues))


class CuePlacementTest(unittest.TestCase):
    def setUp(self):
        self.item = structure_with_phrases()
        self.cues = {cue["slot"]: cue for cue in structure.place_cues(self.item)}

    def test_a_is_the_first_downbeat(self):
        self.assertEqual(self.cues["A"]["role"], "first_downbeat")
        self.assertEqual(self.cues["A"]["ms"], self.item.phrases[0].start_ms)
        self.assertTrue(self.cues["A"]["reason"].startswith("first phrase"))

    def test_b_is_where_the_intro_ends(self):
        self.assertEqual(self.cues["B"]["role"], "intro_end")
        self.assertEqual(self.cues["B"]["ms"], self.item.phrases[0].end_ms)

    def test_c_is_the_build_into_the_drop(self):
        self.assertEqual(self.cues["C"]["role"], "build_up")
        self.assertGreater(self.cues["C"]["ms"], self.cues["B"]["ms"])
        self.assertLess(self.cues["C"]["ms"], self.cues["D"]["ms"])

    def test_d_is_the_first_drop(self):
        self.assertEqual(self.cues["D"]["role"], "drop")
        self.assertEqual(self.cues["D"]["ms"], self.item.phrases[2].start_ms)

    def test_e_is_the_breakdown_after_the_drop(self):
        self.assertEqual(self.cues["E"]["role"], "breakdown")
        self.assertEqual(self.cues["E"]["ms"], self.item.phrases[4].start_ms)
        # A breakdown *before* the drop must not be chosen.
        self.assertGreater(self.cues["E"]["ms"], self.cues["D"]["ms"])

    def test_g_is_the_outro(self):
        self.assertEqual(self.cues["G"]["role"], "outro")
        self.assertEqual(self.cues["G"]["ms"], self.item.phrases[-1].start_ms)

    def test_slots_colours_and_numbers_follow_the_spec(self):
        self.assertEqual(
            [self.cues[s]["colour"] for s in "ABCDEG"],
            ["red", "orange", "yellow", "green", "aqua", "pink"],
        )
        self.assertEqual([self.cues[s]["num"] for s in "ABCDEG"], [0, 1, 2, 3, 4, 6])
        self.assertEqual(self.cues["A"]["rgb"], [230, 0, 0])
        self.assertEqual(self.cues["E"]["rgb"], [0, 200, 200])

    def test_confidence_follows_the_source(self):
        for cue in self.cues.values():
            self.assertEqual(cue["confidence"], "high")
            self.assertEqual(cue["source"], "rekordbox-anlz")

    def test_every_slot_gets_a_cue_even_with_nothing_to_land_on(self):
        # This phrase map has no second drop and a six-bar outro, so F and H
        # fall back to the nearest line that is still in order: an empty pad in
        # the middle of a set is worse than a cue that explains itself.
        self.assertEqual(sorted(self.cues), list("ABCDEFGH"))
        self.assertIn("no second drop", self.cues["F"]["reason"])
        self.assertIn("outro", self.cues["H"]["reason"])

    def test_a_cue_with_nowhere_to_go_keeps_its_slot_anyway(self):
        # A six-second sting has no room for eight separate cues.  Every slot
        # still gets one, in order, rather than the tail being dropped.
        item = structure_with_phrases()
        item.duration_ms = 6_000
        cues = structure.place_cues(item)
        self.assertEqual([cue["slot"] for cue in cues], list("ABCDEFGH"))
        times = [cue["ms"] for cue in cues]
        self.assertEqual(times, sorted(times))

    def test_falls_back_to_the_next_build_when_there_is_no_breakdown(self):
        item = structure_with_phrases()
        item.phrases = [p for p in item.phrases if p.kind != 3]
        cues = {cue["slot"]: cue for cue in structure.place_cues(item)}
        self.assertEqual(cues["E"]["ms"], item.phrases[4].start_ms)  # the next Up
        self.assertIn("no breakdown", cues["E"]["reason"])

    def test_falls_back_to_the_longest_build_when_there_is_no_chorus(self):
        item = structure_with_phrases()
        item.phrases = [p for p in item.phrases if p.kind != 5]
        cues = {cue["slot"]: cue for cue in structure.place_cues(item)}
        self.assertIn("build", cues["D"]["reason"])

    def test_a_beat_grid_without_phrases_still_gets_the_full_layout(self):
        item = structure.Structure(bpm=128.0, beat_ms=beat_grid(128.0, 128), duration_ms=59_531)
        cues = structure.place_cues(item)
        self.assertEqual([cue["slot"] for cue in cues], list("ABCDEFGH"))
        self.assertEqual(cues[0]["role"], "first_downbeat")

    def test_cues_are_clamped_to_the_track(self):
        item = structure_with_phrases()
        item.duration_ms = 20_000
        for cue in structure.place_cues(item):
            self.assertLessEqual(cue["ms"], 20_000)


class BuildFallbackTest(unittest.TestCase):
    """rekordbox marks plenty of edits as a Chorus right after a short Intro.

    There is no Up phrase to use as the build, and the old fallback picked
    ``phrases[1]`` -- that same Chorus -- which put hot cues B and C on one
    millisecond.  Four tracks in the real library hit exactly that, so the
    fallback now moves the build into the run-up to the drop.
    """

    def item_without_a_build(self):
        item = structure_with_phrases()
        plan = [
            (1, "Intro", 1, 33),
            (5, "Chorus", 33, 97),
            (5, "Chorus", 97, 129),
            (3, "Down", 129, 161),
            (6, "Outro", 161, 200),
        ]
        item.phrases = []
        for index, (kind, name, start, end) in enumerate(plan, start=1):
            item.phrases.append(
                structure.Phrase(
                    index=index,
                    kind=kind,
                    name=name,
                    start_beat=start,
                    end_beat=end,
                    start_ms=item.beat_time(start),
                    end_ms=item.beat_time(end),
                )
            )
        return item

    def test_a_build_cue_never_lands_on_the_drop(self):
        item = self.item_without_a_build()
        cues = {cue["slot"]: cue for cue in structure.place_cues(item)}
        self.assertGreater(cues["C"]["ms"], cues["B"]["ms"])
        self.assertGreater(cues["D"]["ms"], cues["C"]["ms"])
        self.assertGreaterEqual(cues["C"]["ms"] - cues["B"]["ms"], structure.MIN_CUE_GAP_MS)
        self.assertGreaterEqual(cues["D"]["ms"] - cues["C"]["ms"], structure.MIN_CUE_GAP_MS)
        self.assertIn("run-up", cues["C"]["reason"])

    def test_every_hot_cue_has_its_own_timestamp(self):
        item = self.item_without_a_build()
        cues = structure.place_cues(item)
        times = [cue["ms"] for cue in cues]
        self.assertEqual(len(set(times)), len(cues))
        # F has no second drop, so it takes the middle of the run to the outro.
        self.assertEqual(len(cues), 8)
        self.assertIn("no second drop", cues[5]["reason"])

    def test_a_real_build_phrase_is_still_preferred(self):
        """When a build follows the intro's drop, the build cue uses it."""
        item = structure_with_phrases()
        plan = [
            (1, "Intro", 1, 17),
            (5, "Chorus", 17, 49),
            (2, "Up", 49, 65),
            (5, "Chorus", 65, 97),
            (6, "Outro", 97, 120),
        ]
        item.phrases = []
        for index, (kind, name, start, end) in enumerate(plan, start=1):
            item.phrases.append(
                structure.Phrase(
                    index=index,
                    kind=kind,
                    name=name,
                    start_beat=start,
                    end_beat=end,
                    start_ms=item.beat_time(start),
                    end_ms=item.beat_time(end),
                )
            )
        cues = {cue["slot"]: cue for cue in structure.place_cues(item)}
        self.assertEqual(cues["C"]["ms"], item.phrases[2].start_ms)
        self.assertIn("run-up", cues["C"]["reason"])


class MemoryCueTest(unittest.TestCase):
    def test_a_memory_cue_on_every_free_phrase_boundary(self):
        item = structure_with_phrases()
        hot = structure.place_cues(item)
        memory = structure.memory_cues(item, hot)
        # The hot cues sit on phrases 1, 2, 3 and 5 and on the outro, so the
        # boundaries that are still free get a memory cue.
        # F now lands on the Up at 73.8s, so only the second Chorus is still
        # free -- the eight hot cues own the rest of the phrase map.
        self.assertEqual({cue["name"] for cue in memory}, {"Chorus 4"})
        self.assertTrue(all(cue["num"] == -1 for cue in memory))

    def test_a_memory_cue_never_lands_on_a_hot_cue(self):
        item = structure_with_phrases()
        hot = structure.place_cues(item)
        taken = {cue["ms"] for cue in hot}
        for cue in structure.memory_cues(item, hot):
            self.assertNotIn(cue["ms"], taken)


class AnalyseTrackTest(unittest.TestCase):
    def test_uses_the_analysis_reader_when_there_is_one(self):
        seen = {}

        def reader(path, bpm):
            seen["path"] = path
            seen["bpm"] = bpm
            return structure_with_phrases()

        track = Track(track_id="1", duration=200.0, bpm=130.0)
        track.analysis_path = "/tmp/does-not-matter.DAT"
        result = structure.analyse_track(track, anlz_reader=reader)
        self.assertEqual(seen["path"], "/tmp/does-not-matter.DAT")
        self.assertEqual(result["structure"]["source"], "rekordbox-anlz")
        self.assertEqual(result["note"], "")
        # All eight, whatever the track already carries: the write replaces the
        # hot slots and drops the memory cue that duplicates one of them.
        self.assertEqual([cue["slot"] for cue in result["hot_cues"]], list("ABCDEFGH"))

    def test_falls_back_when_there_is_no_analysis(self):
        track = Track(track_id="1", duration=240.0, bpm=124.0)
        result = structure.analyse_track(track)
        self.assertEqual(result["structure"]["source"], "heuristic")
        self.assertEqual(result["structure"]["confidence"], "low")
        self.assertIn("no rekordbox analysis", result["note"])

    def test_falls_back_when_the_reader_explodes(self):
        def reader(path, bpm):
            raise RuntimeError("corrupt ANLZ")

        track = Track(track_id="1", duration=240.0, bpm=124.0)
        track.analysis_path = "/tmp/x.DAT"
        result = structure.analyse_track(track, anlz_reader=reader)
        self.assertEqual(result["structure"]["source"], "heuristic")
        self.assertIn("corrupt ANLZ", result["note"])

    def test_analysis_path_from_a_real_anlz_file_is_optional(self):
        """No pyrekordbox must still produce cues, not an exception."""
        track = Track(track_id="1", duration=180.0, bpm=128.0)
        track.analysis_path = str(Path("/nonexistent/ANLZ0000.DAT"))
        result = structure.analyse_track(track)
        self.assertEqual([cue["slot"] for cue in result["hot_cues"]], list("ABCDEFGH"))
        self.assertTrue(result["note"])


if __name__ == "__main__":
    unittest.main()
