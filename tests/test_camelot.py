import unittest

from parity import camelot


class ParseKeyTest(unittest.TestCase):
    def test_camelot_codes(self):
        self.assertEqual(camelot.parse_key("8A"), "8A")
        self.assertEqual(camelot.parse_key(" 12b "), "12B")
        self.assertEqual(camelot.parse_key("1 A"), "1A")

    def test_open_key_notation(self):
        self.assertEqual(camelot.parse_key("8m"), "8A")
        self.assertEqual(camelot.parse_key("8d"), "8B")

    def test_musical_keys(self):
        self.assertEqual(camelot.parse_key("Am"), "8A")
        self.assertEqual(camelot.parse_key("A minor"), "8A")
        self.assertEqual(camelot.parse_key("C"), "8B")
        self.assertEqual(camelot.parse_key("Cmaj"), "8B")
        self.assertEqual(camelot.parse_key("F#m"), "11A")
        self.assertEqual(camelot.parse_key("Gbm"), "11A")

    def test_enharmonic_spellings(self):
        self.assertEqual(camelot.parse_key("G#m"), "1A")
        self.assertEqual(camelot.parse_key("Abm"), "1A")
        self.assertEqual(camelot.parse_key("Db"), "3B")
        self.assertEqual(camelot.parse_key("C#"), "3B")

    def test_unknown_inputs(self):
        for value in ("", None, "8", "unknown", "H", 7):
            self.assertIsNone(camelot.parse_key(value), value)

    def test_out_of_range(self):
        self.assertIsNone(camelot.parse_key("13A"))
        self.assertIsNone(camelot.parse_key("0A"))


class CompatibilityTest(unittest.TestCase):
    def test_identical_key_is_perfect(self):
        self.assertEqual(camelot.compat_score("8A", "8A"), 1.0)
        self.assertEqual(camelot.compat_label("8A", "8A"), "perfect")

    def test_relative_major_minor(self):
        self.assertEqual(camelot.compat_label("8A", "8B"), "relative")
        self.assertGreaterEqual(camelot.compat_score("8A", "8B"), 0.9)

    def test_adjacent_wheel_step(self):
        self.assertEqual(camelot.compat_label("8A", "9A"), "adjacent")
        self.assertEqual(camelot.compat_label("1A", "12A"), "adjacent")

    def test_diagonal_and_two_step(self):
        self.assertEqual(camelot.compat_label("8A", "9B"), "diagonal")
        self.assertEqual(camelot.compat_label("8A", "10A"), "two-step")

    def test_clash_is_avoided(self):
        self.assertEqual(camelot.compat_label("8A", "2A"), "clash")
        self.assertEqual(camelot.compat_score("8A", "2A"), 0.0)

    def test_unknown_keys_are_neutral(self):
        self.assertEqual(camelot.compat_score(None, "8A"), 0.5)
        self.assertEqual(camelot.compat_label("8A", None), "unknown")

    def test_wheel_distance_wraps(self):
        self.assertEqual(camelot.wheel_distance("12A", "1A"), 1)
        self.assertEqual(camelot.wheel_distance("1A", "7A"), 6)

    def test_transpose_keeps_mode(self):
        self.assertEqual(camelot.transpose("12A", 1), "1A")
        self.assertEqual(camelot.transpose("1B", -1), "12B")

    def test_every_pair_scores_between_zero_and_one(self):
        codes = [f"{n}{side}" for n in range(1, 13) for side in "AB"]
        for a in codes:
            for b in codes:
                score = camelot.compat_score(a, b)
                self.assertGreaterEqual(score, 0.0)
                self.assertLessEqual(score, 1.0)


if __name__ == "__main__":
    unittest.main()
