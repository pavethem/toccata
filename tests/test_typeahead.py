import unittest

from toccata.typeahead import Matcher, TypeAhead, normalize

NAMES = ["ABBA", "Air", "Alan Lamb", "Beatles, The", "Björk", "Can", "Guns N' Roses",
         "Mötley Crüe", "The Beatles", "The The", "Tom Waits"]


class TestNormalize(unittest.TestCase):
    def test_case_accents_punctuation(self):
        self.assertEqual(normalize("Mötley Crüe"), "motley crue")
        self.assertEqual(normalize("Guns N' Roses"), "guns n roses")
        self.assertEqual(normalize("  AC/DC  "), "acdc")


class TestMatcher(unittest.TestCase):
    def setUp(self):
        self.m = Matcher(NAMES)

    def name(self, typed, after=-1):
        i = self.m.find(typed, after)
        return None if i is None else NAMES[i]

    def test_prefix(self):
        self.assertEqual(self.name("a"), "ABBA")
        self.assertEqual(self.name("al"), "Alan Lamb")
        self.assertEqual(self.name("bjo"), "Björk")
        self.assertEqual(self.name("guns n"), "Guns N' Roses")
        self.assertEqual(self.name("motley"), "Mötley Crüe")

    def test_prefix_beats_later_word(self):
        self.assertEqual(self.name("beat"), "Beatles, The")
        self.assertEqual(self.name("the b"), "The Beatles")

    def test_later_word_when_no_prefix(self):
        self.assertEqual(self.name("lamb"), "Alan Lamb")
        self.assertEqual(self.name("waits"), "Tom Waits")

    def test_no_match(self):
        self.assertIsNone(self.name("zz"))
        self.assertIsNone(self.name(""))
        self.assertIsNone(Matcher([]).find("a"))

    def test_after_steps_on_and_wraps(self):
        self.assertEqual(self.name("a", after=0), "Air")
        self.assertEqual(self.name("a", after=2), "ABBA")  # wrapped around


class TestTypeAhead(unittest.TestCase):
    def test_letters_accumulate_within_timeout(self):
        t = TypeAhead(timeout=1.0)
        self.assertEqual(t.type("b", 0.0), ("b", False))
        self.assertEqual(t.type("j", 0.5), ("bj", False))
        self.assertEqual(t.type("o", 1.4), ("bjo", False))  # 0.9 s after the last key

    def test_pause_starts_over(self):
        t = TypeAhead(timeout=1.0)
        t.type("b", 0.0)
        self.assertEqual(t.type("c", 1.5), ("c", False))

    def test_same_letter_again_steps(self):
        t = TypeAhead(timeout=1.0)
        self.assertEqual(t.type("a", 0.0), ("a", False))
        self.assertEqual(t.type("a", 0.3), ("a", True))
        self.assertEqual(t.type("a", 0.6), ("a", True))
        self.assertEqual(t.text, "aaa")

    def test_double_letter_inside_a_word_does_not_step(self):
        t = TypeAhead(timeout=1.0)
        t.type("a", 0.0)
        t.type("b", 0.1)
        self.assertEqual(t.type("b", 0.2), ("abb", False))

    def test_backspace_and_cancel(self):
        t = TypeAhead(timeout=1.0)
        t.type("a", 0.0)
        t.type("l", 0.1)
        self.assertEqual(t.backspace(0.2), "a")
        self.assertTrue(t.active(0.3))
        t.cancel()
        self.assertFalse(t.active(0.3))
        self.assertEqual(t.backspace(5.0), "")


if __name__ == "__main__":
    unittest.main()
