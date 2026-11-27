import unittest

from toccata.queuefilter import count_text, filter_names, fold, parse

SONGS = [
    dict(file="a/1.flac", title="Blue in Green", artist="Miles Davis", albumartist="Miles Davis",
         album="Kind of Blue", date="1959-08-17"),
    dict(file="b/1.flac", title="Kickstart My Heart", artist="Mötley Crüe", album="Dr. Feelgood", date="1989"),
    dict(file="c/1.flac", title="Track", artist="Guest", albumartist="Various Artists", album="Hits",
         composer="Bach", date="2001"),
    dict(file="d/untagged.flac"),
]


def matching(text):
    f = parse(text)
    return [s["file"] for s in SONGS if f.matches(s)]


class TestQueueFilter(unittest.TestCase):
    def test_short_text_does_not_filter(self):
        self.assertFalse(parse("").active)
        self.assertFalse(parse("b").active)
        self.assertEqual(len(matching("b")), 4)

    def test_word_in_any_field_case_insensitive(self):
        self.assertEqual(matching("BLUE"), ["a/1.flac"])           # title and album
        self.assertEqual(matching("feelgood"), ["b/1.flac"])       # album
        self.assertEqual(matching("various"), ["c/1.flac"])        # album artist
        self.assertEqual(matching("guest"), ["c/1.flac"])          # artist differing from album artist
        self.assertEqual(matching("bach"), ["c/1.flac"])           # composer
        self.assertEqual(matching("untagged"), ["d/untagged.flac"])  # file name when there is no title

    def test_every_word_must_match_somewhere(self):
        self.assertEqual(matching("miles green"), ["a/1.flac"])
        self.assertEqual(matching("miles heart"), [])

    def test_accents_can_be_left_out(self):
        self.assertEqual(matching("motley"), ["b/1.flac"])
        self.assertEqual(matching("Mötley"), ["b/1.flac"])
        self.assertEqual(fold("Crüe"), "crue")

    def test_year_and_range(self):
        self.assertEqual(matching("#1959"), ["a/1.flac"])
        self.assertEqual(matching("#1980-1990"), ["b/1.flac"])
        self.assertEqual(matching("#1990-1950"), ["a/1.flac", "b/1.flac"])  # either order
        self.assertEqual(matching("#2001 bach"), ["c/1.flac"])
        self.assertEqual(matching("#2001 miles"), [])
        self.assertEqual(matching("#99"), [])  # not a year: an ordinary word, found nowhere

    def test_words_do_not_match_across_fields(self):
        # album artist "Miles Davis" is followed by the title "Blue in Green":
        # "davis blue" is two words, both found; "davisblue" would span two fields
        self.assertEqual(matching("davis blue"), ["a/1.flac"])
        self.assertEqual(matching("davisblue"), [])

    def test_count_text(self):
        self.assertEqual(count_text(12, 340), "12 of 340")


if __name__ == "__main__":
    unittest.main()


class TestFilterNames(unittest.TestCase):
    NAMES = ["The Beatles", "Mötley Crüe", "Miles Davis", "Davis Miles Band", "ABBA"]

    def test_short_text_keeps_all(self):
        self.assertEqual(filter_names(self.NAMES, ""), self.NAMES)
        self.assertEqual(filter_names(self.NAMES, " a "), self.NAMES)

    def test_every_word_any_order_case_and_accents(self):
        self.assertEqual(filter_names(self.NAMES, "miles DAVIS"), ["Miles Davis", "Davis Miles Band"])
        self.assertEqual(filter_names(self.NAMES, "motley"), ["Mötley Crüe"])
        self.assertEqual(filter_names(self.NAMES, "crüe"), ["Mötley Crüe"])
        self.assertEqual(filter_names(self.NAMES, "beat"), ["The Beatles"])  # substring, also mid-name
        self.assertEqual(filter_names(self.NAMES, "miles band"), ["Davis Miles Band"])
        self.assertEqual(filter_names(self.NAMES, "zz"), [])
