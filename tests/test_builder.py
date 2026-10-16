import random
import unittest

from parity import builder


def make_pool(count: int = 120, seed: int = 7) -> list[builder.Candidate]:
    rng = random.Random(seed)
    codes = [f"{n}{side}" for n in range(1, 13) for side in "AB"]
    genres = ["Deep House", "Tech House", "Techno", "Hard Techno", "Melodic House"]
    tracks = []
    for index in range(count):
        tracks.append(
            builder.Candidate(
                key=f"t{index:03d}",
                title=f"Track {index}",
                artist=f"Artist {index % 23}",
                genre=rng.choice(genres),
                camelot=rng.choice(codes),
                bpm=round(rng.uniform(118.0, 142.0), 1),
                energy=round(rng.uniform(2.0, 10.0), 1),
                duration=round(rng.uniform(200.0, 430.0), 1),
            )
        )
    return tracks


class CurveTest(unittest.TestCase):
    def test_linear_interpolation(self):
        points = [(0.0, 2.0), (1.0, 8.0)]
        self.assertEqual(builder.curve_from_points(points, 5), [2.0, 3.5, 5.0, 6.5, 8.0])

    def test_single_slot(self):
        self.assertEqual(builder.curve_from_points([(0.0, 3.0), (1.0, 9.0)], 1), [3.0])

    def test_multipoint_curve_hits_its_knots(self):
        points = builder.template_points("journey")
        values = builder.curve_from_points(points, 5)
        for (position, energy), value in zip(points, values):
            self.assertAlmostEqual(energy, value, places=6)

    def test_empty_points_fall_back(self):
        self.assertEqual(builder.curve_from_points([], 3), [5.0, 5.0, 5.0])

    def test_zero_length(self):
        self.assertEqual(builder.curve_from_points([(0.0, 3.0)], 0), [])


def candidates(energies: list[float], **common) -> list[builder.Candidate]:
    return [
        builder.Candidate(key=f"e{index}-{value}", energy=value, **common)
        for index, value in enumerate(energies)
    ]


class CurveFitTest(unittest.TestCase):
    """Choosing the group is its own problem, and the greedy one got it wrong."""

    def test_the_quietest_target_gets_the_quietest_track(self):
        pool = candidates([4.0, 2.0, 9.0, 6.0])
        targets = [2.5, 5.5, 8.5]
        placed = builder.fit_curve(pool, targets, [0, 1, 2])
        self.assertEqual([placed[i].energy for i in (0, 1, 2)], [2.0, 6.0, 9.0])

    def test_energy_follows_the_target_order(self):
        """The invariant: rank the slots by target and the tracks come out ranked."""
        pool = candidates([2.0, 2.1, 3.0, 3.1, 4.0, 4.1, 5.0, 5.1])
        targets = builder.curve_from_points([(0.0, 2.0), (0.6, 9.0), (1.0, 3.0)], 6)
        placed = builder.fit_curve(pool, targets, list(range(6)))
        ranked = [placed[i].energy for i in sorted(range(6), key=lambda index: targets[index])]
        self.assertEqual(ranked, sorted(ranked))

    def test_a_slot_by_slot_greedy_would_have_spent_the_wrong_track(self):
        # Target 0 sits between -1 and 0.5, but 0.5 is the only track near
        # target 1, so taking the closest for slot 0 costs twice as much.
        targets = [0.0, 1.0]
        pool = candidates([-1.0, 0.5, 1.0])
        placed = builder.fit_curve(pool, targets, [0, 1])
        self.assertEqual([placed[0].energy, placed[1].energy], [0.5, 1.0])
        best = sum((placed[i].energy - targets[i]) ** 2 for i in (0, 1))
        self.assertLess(best, (1.0 + 0.0))

    def test_only_the_positions_asked_for_are_filled(self):
        pool = candidates([1.0, 2.0, 3.0, 4.0])
        placed = builder.fit_curve(pool, [1.0, 2.0, 3.0, 4.0], [1, 3])
        self.assertEqual(sorted(placed), [1, 3])
        self.assertEqual(len({track.key for track in placed.values()}), 2)

    def test_a_pool_smaller_than_the_set_uses_everything_it_has(self):
        pool = candidates([1.0, 5.0])
        placed = builder.fit_curve(pool, [1.0, 3.0], [0, 1])
        self.assertEqual(len(placed), 2)

    def test_equal_energy_tracks_are_drawn_fresh_each_build(self):
        """A clump of identical estimates must not make every build the same."""
        pool = candidates([5.0] * 8)
        firsts = {
            builder.fit_curve(pool, [5.0, 5.0], [0, 1], random.Random(seed))[0].key
            for seed in range(20)
        }
        self.assertGreater(len(firsts), 1)
        placed = builder.fit_curve(pool, [5.0, 5.0], [0, 1], random.Random(3))
        self.assertEqual({track.energy for track in placed.values()}, {5.0})

    def test_the_walk_fallback_agrees_with_the_table(self):
        pool = candidates([2.0, 3.0, 4.5, 5.0, 8.0, 9.0, 9.5])
        targets = [3.0, 5.0, 9.0]
        table = builder._fit_dp(pool, targets)
        walk = builder._fit_walk(pool, targets)
        self.assertEqual([track.energy for track in walk], [track.energy for track in table])


class CurveNoteTest(unittest.TestCase):
    def test_a_curve_the_pool_cannot_reach_is_named(self):
        pool = candidates([6.0, 6.5, 7.0])
        notes = builder.curve_notes(pool, pool, [2.5, 4.0, 3.0])
        self.assertTrue(any("quietest track in the pool is E6.0" in note for note in notes), notes)

    def test_a_flat_result_against_a_steep_curve_is_called_out(self):
        # The pool can span the curve (E2-E10 against a curve of E1-E9), so the
        # fault is the set's: it came out flat and the note says so.
        pool = candidates([2.0, 3.0, 4.0, 5.0, 8.0, 10.0])
        order = candidates([4.0, 4.1, 4.2])
        notes = builder.curve_notes(pool, order, [1.0, 9.0, 1.0])
        self.assertTrue(any("flattens out" in note for note in notes), notes)

    def test_low_confidence_energies_are_reported(self):
        pool = candidates([3.0, 3.5, 4.0], confidence="low")
        notes = builder.curve_notes(pool, pool, [3.0, 3.5, 4.0])
        self.assertTrue(any("BPM alone" in note for note in notes), notes)

    def test_a_reachable_curve_says_nothing(self):
        pool = candidates([3.0, 4.0, 5.0, 6.0, 7.0], confidence="high")
        order = candidates([3.0, 4.0, 6.0, 5.0], confidence="high")
        self.assertEqual(builder.curve_notes(pool, order, [3.0, 4.0, 6.0, 5.0]), [])

    def test_a_pool_without_the_range_to_hold_the_shape_is_named(self):
        # Measured energies on a library of mastered club tracks: everything sits
        # inside one energy point, so no ordering can make a chill curve out of
        # it, and the build has to say that rather than draw a flat line quietly.
        pool = candidates([7.6, 7.8, 7.9, 8.1, 8.3], confidence="high")
        order = candidates([7.6, 7.9, 8.3, 8.1], confidence="high")
        notes = builder.curve_notes(pool, order, [2.5, 4.0, 3.0, 2.7])
        self.assertTrue(any("not enough range in it to build" in note for note in notes), notes)
        self.assertFalse(any("flattens out" in note for note in notes), notes)

    def test_a_curve_the_pool_is_mostly_too_loud_for_is_counted(self):
        # A measured library: five quiet tracks, everything else mastered loud.
        # A chill curve can only reach the five, so the set rides far above it --
        # and the note has to say how many tracks were actually in the band.
        pool = candidates([2.1, 2.4, 2.7, 3.7, 5.0, 6.6, 7.2, 7.4, 7.9, 8.3, 9.0], confidence="high")
        order = candidates([2.4, 2.1, 2.7, 5.0, 6.6, 7.2, 7.9], confidence="high")
        notes = builder.curve_notes(pool, order, [2.5, 3.0, 3.9, 3.0])
        self.assertTrue(any("tracks measure inside that band" in note for note in notes), notes)

    def test_a_narrow_flat_curve_is_not_lectured_about_band_size(self):
        # A flat curve has a narrow band by construction; if the set sits on it,
        # there is nothing to explain.
        pool = candidates([5.0, 5.4, 5.8, 6.0, 6.2, 6.6, 7.0], confidence="high")
        order = candidates([5.8, 6.0, 6.0, 6.2, 6.0, 6.0], confidence="high")
        notes = builder.curve_notes(pool, order, [6.0, 6.0, 6.0, 6.0, 6.0, 6.0])
        self.assertFalse(any("measure inside that band" in note for note in notes), notes)

    def test_a_pool_that_can_span_the_curve_is_not_accused(self):
        pool = candidates([2.0, 3.0, 5.0, 7.0, 9.0, 10.0], confidence="high")
        order = candidates([2.0, 5.0, 9.0, 10.0], confidence="high")
        notes = builder.curve_notes(pool, order, [2.0, 5.0, 9.0, 10.0])
        self.assertFalse(any("not enough range" in note for note in notes), notes)


class CurveTrackingTest(unittest.TestCase):
    def test_the_curve_is_reported_with_the_stats(self):
        result = builder.build_set(make_pool(), builder.BuildConfig(length_tracks=12, seed=8))
        stats = result.stats
        self.assertLess(stats["curve_fit"], 1.0)
        self.assertGreaterEqual(stats["curve_worst"], stats["curve_fit"])
        self.assertEqual(len(stats["curve_target_range"]), 2)
        self.assertGreaterEqual(stats["curve_target_range"][1], stats["curve_target_range"][0])
        self.assertEqual(len(stats["curve_actual_range"]), 2)

    def test_a_rising_target_gets_a_rising_set(self):
        pool = candidates([2.0, 2.1, 3.0, 3.1, 4.0, 4.1, 5.0, 5.1], bpm=128.0)
        config = builder.BuildConfig(
            length_tracks=6,
            key_weight=0.0,
            bpm_weight=0.0,
            iterations=0,
            seed=1,
            curve_points=[(0.0, 2.0), (1.0, 5.2)],
        )
        result = builder.build_set(pool, config)
        self.assertGreater(result.energy_curve[-1], result.energy_curve[0])
        self.assertLess(result.stats["curve_fit"], 0.4)
        self.assertEqual(result.stats["curve_actual_range"], [2.0, 5.1])

    def test_the_set_stays_inside_the_pool(self):
        """No swap-in may smuggle in a track the pool filter removed."""
        pool = make_pool()
        allowed = {track.key for track in pool[:40]}
        config = builder.BuildConfig(length_tracks=10, seed=3, iterations=1500)
        config.excluded = {track.key for track in pool[40:]}
        result = builder.build_set(pool, config)
        self.assertTrue(set(result.order).issubset(allowed))

    def test_missing_energies_do_not_break_the_choice(self):
        pool = candidates([5.0, 5.0, 5.0])
        result = builder.build_set(pool, builder.BuildConfig(length_tracks=3, seed=1))
        self.assertEqual(len(result.order), 3)


class StrictRepairTest(unittest.TestCase):
    """Strict mode has to repair what the annealer leaves, or say it cannot."""

    @staticmethod
    def track(key: str, energy: float, code: str) -> builder.Candidate:
        return builder.Candidate(key=key, title=key, camelot=code, energy=energy, bpm=128.0)

    def set_up(self):
        # 2A -> 4A -> 6A -> 7A: two two-steps in a row.  No swap fixes that --
        # the only keys a swap could put between them still leave one of the
        # four transitions weak -- so the repair has to change the tracks.
        order = [
            self.track("a", 8.0, "2A"),
            self.track("b", 9.0, "4A"),
            self.track("c", 9.0, "6A"),
            self.track("d", 8.5, "7A"),
        ]
        targets = [8.0, 9.0, 9.0, 8.5]
        spare = [
            self.track("s1", 8.1, "4A"),
            self.track("s2", 9.1, "5A"),
            self.track("s3", 9.2, "6A"),
            self.track("s4", 3.0, "12B"),
        ]
        return order, targets, spare

    def test_a_gap_one_swap_cannot_bridge_is_rebuilt_from_the_pool(self):
        order, targets, spare = self.set_up()
        self.assertEqual(builder._weak_positions(order), [1, 2])
        config = builder.BuildConfig(strictness="strict")
        repaired = builder.resolve_clashes(order, targets, config, spare, locked=set())
        self.assertEqual(builder._weak_positions(repaired), [])
        self.assertEqual(len(repaired), len(order))
        # 4A, 5A, 6A, 7A -- the pool's 5A has to come in, and the 6A already in
        # the set is a better fit for its slot than the spare one.
        self.assertEqual(
            [track.camelot for track in repaired], ["4A", "5A", "6A", "7A"]
        )

    def test_the_repair_leaves_a_locked_slot_alone(self):
        order, targets, spare = self.set_up()
        config = builder.BuildConfig(strictness="strict")
        repaired = builder.resolve_clashes(order, targets, config, spare, locked={1})
        self.assertEqual(repaired[1].key, "b")

    def test_normal_builds_are_not_touched_by_the_repair(self):
        order, targets, spare = self.set_up()
        config = builder.BuildConfig(strictness="normal")
        repaired = builder.resolve_clashes(order, targets, config, spare, locked=set())
        self.assertEqual([track.key for track in repaired], [track.key for track in order])

    def test_an_unfixable_pair_is_reported_in_words(self):
        order = [self.track("a", 8.0, "2A"), self.track("b", 9.0, "4A"),
                 self.track("c", 8.5, "7A")]
        targets = [8.0, 9.0, 8.5]
        config = builder.BuildConfig(strictness="strict")
        repaired = builder.resolve_clashes(order, targets, config, [], locked=set())
        # Nothing in the pool can sit between a 2A and a 4A and then reach a 7A,
        # so both of those pairs stay weak and the build has to say so.
        self.assertEqual(builder._weak_positions(repaired), [1, 2])
        notes = builder.strict_notes(repaired, config)
        self.assertEqual(len(notes), 1)
        self.assertIn("Strict could not make every mix harmonic", notes[0])
        self.assertIn("2A", notes[0])
        self.assertEqual(builder.strict_notes(repaired, builder.BuildConfig()), [])

    def test_a_strict_build_is_harmonic_all_the_way_through(self):
        config = builder.BuildConfig(
            template="journey", length_tracks=16, strictness="strict", seed=2, iterations=2000
        )
        result = builder.build_set(make_pool(), config)
        self.assertEqual(result.stats["harmonic_ok"], 100.0)
        self.assertEqual(result.stats["clashes"], 0)
        self.assertTrue(any("into a harmonic mix" in w for w in result.warnings))


class BpmTest(unittest.TestCase):
    def test_half_and_double_time_count_as_matching(self):
        self.assertEqual(builder.bpm_delta(87.0, 174.0), 0.0)
        self.assertEqual(builder.bpm_delta(174.0, 87.0), 0.0)
        self.assertEqual(builder.bpm_delta(120.0, 130.0), 10.0)

    def test_missing_bpm_is_neutral(self):
        self.assertEqual(builder.bpm_delta(0.0, 128.0), 0.0)


class BuildTest(unittest.TestCase):
    def build(self, **overrides):
        config = builder.BuildConfig(seed=11, iterations=800, **overrides)
        return builder.build_set(make_pool(), config), config

    def test_length_and_pool_membership(self):
        pool = make_pool()
        result = builder.build_set(pool, builder.BuildConfig(length_tracks=15, seed=3))
        self.assertEqual(len(result.order), 15)
        self.assertEqual(len(set(result.order)), 15)
        self.assertTrue(set(result.order).issubset({t.key for t in pool}))

    def test_length_is_capped_by_pool(self):
        small = make_pool(6)
        result = builder.build_set(small, builder.BuildConfig(length_tracks=20, seed=3))
        self.assertEqual(len(result.order), 6)
        self.assertTrue(any("Only 6 tracks" in w for w in result.warnings))

    def test_energy_follows_the_curve(self):
        config = builder.BuildConfig(
            template="journey", length_tracks=12, energy_weight=1.0,
            key_weight=0.0, bpm_weight=0.0, iterations=2000, seed=5,
        )
        result = builder.build_set(make_pool(), config)
        deviations = [abs(a - b) for a, b in zip(result.energy_curve, result.targets)]
        self.assertLess(sum(deviations) / len(deviations), 1.0)

    def test_cool_down_template_descends(self):
        config = builder.BuildConfig(
            template="cool_down", length_tracks=10, key_weight=0.0, bpm_weight=0.0, seed=2,
        )
        result = builder.build_set(make_pool(), config)
        self.assertGreater(result.energy_curve[0], result.energy_curve[-1])

    def test_strict_is_at_least_as_harmonic_as_free(self):
        strict = builder.BuildConfig(strictness="strict", length_tracks=14, seed=9, iterations=1500)
        free = builder.BuildConfig(strictness="free", length_tracks=14, seed=9, iterations=1500)
        pool = make_pool()
        strict_stats = builder.build_set(pool, strict).stats
        free_stats = builder.build_set(pool, free).stats
        self.assertGreaterEqual(strict_stats["harmonic_ok"], free_stats["harmonic_ok"])
        self.assertGreaterEqual(strict_stats["harmonic_ok"], 50.0)

    def test_strict_mode_avoids_key_clashes(self):
        config = builder.BuildConfig(
            template="peak_time", length_tracks=14, strictness="strict", seed=4, iterations=2000
        )
        result = builder.build_set(make_pool(), config)
        self.assertEqual(result.stats["clashes"], 0)
        self.assertEqual(result.stats["harmonic_ok"], 100.0)

    def test_bpm_tolerance_reduces_jumps(self):
        pool = make_pool()
        loose = builder.build_set(pool, builder.BuildConfig(length_tracks=12, bpm_tolerance=20, seed=4))
        tight = builder.build_set(pool, builder.BuildConfig(length_tracks=12, bpm_tolerance=3, seed=4))

        def mean_delta(result):
            deltas = [t.bpm_delta for t in result.transitions]
            return sum(deltas) / len(deltas)

        tight_max = max(t.bpm_delta for t in tight.transitions)
        loose_max = max(t.bpm_delta for t in loose.transitions)
        self.assertLess(mean_delta(tight), mean_delta(loose))
        self.assertLess(tight_max, loose_max)
        # The tolerance is a soft target: worst case stays in the same ballpark.
        self.assertLessEqual(tight_max, 2 * 3.0)

    def test_artist_repeat_window_is_respected(self):
        config = builder.BuildConfig(
            length_tracks=16, artist_repeat_window=3, energy_weight=0.2, key_weight=0.2,
            bpm_weight=0.0, iterations=3000, seed=13,
        )
        result = builder.build_set(make_pool(), config)
        pool = {t.key: t for t in make_pool()}
        artists = [pool[key].artist for key in result.order]
        repeats_within_window = sum(
            1
            for i in range(len(artists))
            for j in range(i + 1, min(len(artists), i + 4))
            if artists[i] == artists[j]
        )
        self.assertLessEqual(repeats_within_window, 2)

    def test_locks_and_pins_are_honoured(self):
        pool = make_pool()
        config = builder.BuildConfig(length_tracks=10, seed=6)
        config.locks = {0: pool[40].key, 5: pool[3].key}
        config.start_key = pool[40].key
        config.end_key = pool[7].key
        result = builder.build_set(pool, config)
        self.assertEqual(result.order[0], pool[40].key)
        self.assertEqual(result.order[5], pool[3].key)
        self.assertEqual(result.order[-1], pool[7].key)

    def test_excluded_tracks_never_appear(self):
        pool = make_pool()
        excluded = {t.key for t in pool[:20]}
        config = builder.BuildConfig(length_tracks=12, seed=1, iterations=500)
        config.excluded = excluded
        result = builder.build_set(pool, config)
        self.assertFalse(excluded & set(result.order))

    def test_filters_shrink_the_pool(self):
        pool = make_pool()
        config = builder.BuildConfig(length_tracks=10, min_bpm=125, max_bpm=132, min_energy=5, max_energy=9, seed=1)
        result = builder.build_set(pool, config)
        by_key = {t.key: t for t in pool}
        for key in result.order:
            track = by_key[key]
            self.assertTrue(125 <= track.bpm <= 132)
            self.assertTrue(5 <= track.energy <= 9)

    def test_same_seed_gives_same_set(self):
        pool = make_pool()
        config = builder.BuildConfig(length_tracks=12, seed=42)
        self.assertEqual(
            builder.build_set(pool, config).order,
            builder.build_set(pool, config).order,
        )

    def test_different_seeds_give_variety(self):
        pool = make_pool()
        first = builder.build_set(pool, builder.BuildConfig(length_tracks=12, seed=1)).order
        second = builder.build_set(pool, builder.BuildConfig(length_tracks=12, seed=2)).order
        self.assertNotEqual(first, second)

    def test_empty_pool_is_reported(self):
        result = builder.build_set([], builder.BuildConfig(length_tracks=5))
        self.assertEqual(result.order, [])
        self.assertTrue(result.warnings)

    def test_stats_are_reported(self):
        result = builder.build_set(make_pool(), builder.BuildConfig(length_tracks=12, seed=8))
        stats = result.stats
        self.assertEqual(stats["count"], 12)
        self.assertGreater(stats["duration"], 0)
        self.assertGreater(stats["bpm_max"], stats["bpm_min"])
        self.assertGreaterEqual(stats["harmonic_ok"], 0)
        self.assertLessEqual(stats["harmonic_ok"], 100)

    def test_transitions_align_with_order(self):
        result = builder.build_set(make_pool(), builder.BuildConfig(length_tracks=8, seed=8))
        self.assertEqual(len(result.transitions), 7)
        self.assertEqual([t.index for t in result.transitions], list(range(1, 8)))


class OptimizeTest(unittest.TestCase):
    def shuffled_pool(self):
        pool = make_pool()[30:60]
        random.Random(5).shuffle(pool)
        return pool

    def test_optimize_keeps_every_track_and_improves_or_holds_cost(self):
        pool = self.shuffled_pool()
        config = builder.BuildConfig(strictness="strict", iterations=4000, seed=3)
        targets = builder.curve_from_points(builder.template_points("journey"), len(pool))
        before = builder.total_cost(pool, targets, config)
        result = builder.optimize_set(pool, config, targets=targets)
        self.assertEqual(sorted(result.order), sorted(t.key for t in pool))
        self.assertLessEqual(result.cost, before)

    def test_optimize_keeps_locked_tracks_at_their_position(self):
        pool = self.shuffled_pool()
        config = builder.BuildConfig(strictness="normal", iterations=2000, seed=3)
        config.locks = {0: pool[2].key, 4: pool[7].key, len(pool) - 1: pool[1].key}
        result = builder.optimize_set(pool, config)
        self.assertEqual(result.order[0], pool[2].key)
        self.assertEqual(result.order[4], pool[7].key)
        self.assertEqual(result.order[-1], pool[1].key)

    def test_optimize_with_custom_targets(self):
        pool = self.shuffled_pool()
        config = builder.BuildConfig(iterations=1500, seed=1, energy_weight=2.0)
        targets = builder.curve_from_points([(0.0, 9.0), (1.0, 2.0)], len(pool))
        result = builder.optimize_set(pool, config, targets=targets)
        self.assertGreater(result.energy_curve[0], result.energy_curve[-1])

    def test_optimize_empty(self):
        result = builder.optimize_set([], builder.BuildConfig())
        self.assertEqual(result.order, [])
        self.assertTrue(result.warnings)


if __name__ == "__main__":
    unittest.main()
