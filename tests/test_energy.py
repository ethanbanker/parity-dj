import unittest

from parity import energy
from parity.rekordbox import Track


def make_track(**kwargs) -> Track:
    track = Track(track_id="1")
    for key, value in kwargs.items():
        setattr(track, key, value)
    return track


class TagTest(unittest.TestCase):
    def test_plain_tags(self):
        self.assertEqual(energy.tag_energy("E7"), 7.0)
        self.assertEqual(energy.tag_energy("energy 8"), 8.0)
        self.assertEqual(energy.tag_energy("Energy: 6"), 6.0)
        self.assertEqual(energy.tag_energy("Energy=9.5"), 9.5)

    def test_bracket_and_suffix_tags(self):
        self.assertEqual(energy.tag_energy("[7/10]"), 7.0)
        self.assertEqual(energy.tag_energy("banger E9"), 9.0)

    def test_words_ending_in_e_are_not_tags(self):
        self.assertIsNone(energy.tag_energy("the 4am rolling vibe 6"))
        self.assertIsNone(energy.tag_energy("vibe 6"))

    def test_out_of_range_and_scaled_tags(self):
        # 0 is not a valid energy, and 1-10 is the only accepted direct scale.
        self.assertIsNone(energy.tag_energy("E0"))
        # 11-100 is read as a percentage-style tag.
        self.assertEqual(energy.tag_energy("E44"), 4.4)
        self.assertEqual(energy.tag_energy("E75"), 7.5)

    def test_empty(self):
        self.assertIsNone(energy.tag_energy(""))
        self.assertIsNone(energy.tag_energy(None))


class GenreTest(unittest.TestCase):
    def test_longest_match_wins(self):
        self.assertEqual(energy.genre_energy("Tech House"), energy.GENRE_ENERGY["tech house"])
        self.assertGreater(energy.genre_energy("Peak Time Techno"), energy.genre_energy("Ambient"))

    def test_unknown_genre(self):
        self.assertIsNone(energy.genre_energy("Klezmer Bebop"))
        self.assertIsNone(energy.genre_energy(""))


class EstimateTest(unittest.TestCase):
    def test_override_wins(self):
        track = make_track(comments="E9", genre="Ambient", rating=255)
        estimate = energy.estimate(track, override=3.0)
        self.assertEqual(estimate.value, 3.0)
        self.assertEqual(estimate.source, "override")
        self.assertEqual(estimate.confidence, "high")

    def test_audio_measurement_beats_the_metadata_guess(self):
        track = make_track(comments="", genre="Ambient", rating=0, bpm=128.0)
        guess = energy.estimate(track)
        measured = energy.estimate(track, None, 8.4)
        self.assertEqual(measured.value, 8.4)
        self.assertEqual(measured.source, "audio")
        self.assertEqual(measured.confidence, "high")
        self.assertNotEqual(measured.value, guess.value)

    def test_a_hand_typed_override_and_tag_still_win(self):
        track = make_track(comments="E3", genre="Ambient")
        self.assertEqual(energy.estimate(track, None, 9.1).source, "tag")
        self.assertEqual(energy.estimate(track, 6.5, 9.1).source, "override")
        self.assertEqual(energy.estimate(track, 6.5, 9.1).value, 6.5)

    def test_audio_values_stay_in_range(self):
        track = make_track()
        self.assertEqual(energy.estimate(track, None, 12.5).value, 10.0)
        self.assertEqual(energy.estimate(track, None, 0.2).value, 1.0)

    def test_tag_beats_everything(self):
        estimate = energy.estimate(make_track(comments="E2", genre="Hard Techno", rating=255))
        self.assertEqual((estimate.value, estimate.source), (2.0, "tag"))

    def test_rating_nudges_genre(self):
        low = energy.estimate(make_track(genre="House", rating=51))
        high = energy.estimate(make_track(genre="House", rating=255))
        self.assertLess(low.value, high.value)
        self.assertEqual(high.source, "rating")
        self.assertEqual(high.confidence, "medium")

    def test_genre_only_is_medium_confidence(self):
        estimate = energy.estimate(make_track(genre="Techno", bpm=132))
        self.assertEqual(estimate.source, "genre")
        self.assertEqual(estimate.confidence, "medium")

    def test_bare_bpm_is_low_confidence_and_flagged(self):
        estimate = energy.estimate(make_track(bpm=140))
        self.assertEqual(estimate.source, "bpm")
        self.assertTrue(estimate.needs_review)

    def test_totally_empty_track_defaults(self):
        estimate = energy.estimate(make_track())
        self.assertEqual((estimate.value, estimate.source), (5.0, "default"))
        self.assertTrue(estimate.needs_review)

    def test_values_stay_in_range(self):
        for rating in (0, 51, 102, 153, 204, 255):
            for genre in ("Hardcore", "Ambient", "Unknown Genre"):
                estimate = energy.estimate(make_track(genre=genre, rating=rating, bpm=200))
                self.assertGreaterEqual(estimate.value, energy.MIN_ENERGY)
                self.assertLessEqual(estimate.value, energy.MAX_ENERGY)


if __name__ == "__main__":
    unittest.main()
