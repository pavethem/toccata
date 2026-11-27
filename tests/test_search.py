import time
import unittest

from toccata import search
from toccata.mpd_client import MPDClient
from toccata.search import (MODIFIED, Result, SearchError, available_categories, parse_modified,
                                parse_version, run_search, song_sort_key, stats_text)
from tests import search_mock


class MockCase(unittest.TestCase):
    settings = {}

    def setUp(self):
        srv, port = search_mock.start(**self.settings)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.client = MPDClient("127.0.0.1", port)
        self.client.connect()
        self.addCleanup(self.client.close)

    def files(self, key, text, **kw):
        return [s["file"] for s in run_search(self.client, key, text, **kw).songs]


class TestCategories(unittest.TestCase):
    def keys(self, tags, version=(0, 23, 0)):
        return [c.key for c in available_categories(tags, version)]

    def test_full_server_offers_every_category_in_order(self):
        self.assertEqual(self.keys(["Composer", "Performer", "Comment", "OriginalDate"]),
                         ["artist", "composer", "performer", "album", "title", "genre", "comment",
                          "date", "originaldate", MODIFIED, "file", "any"])

    def test_optional_tags_need_tagtypes(self):
        keys = self.keys(["Artist", "Album"])
        for k in ("composer", "performer", "comment", "originaldate"):
            self.assertNotIn(k, keys)
        self.assertIn("genre", keys)

    def test_tagtypes_case_does_not_matter(self):
        self.assertIn("composer", self.keys(["composer"]))

    def test_modified_needs_0_19(self):
        self.assertNotIn(MODIFIED, self.keys([], (0, 18, 9)))
        self.assertIn(MODIFIED, self.keys([], (0, 19, 0)))

    def test_parse_version(self):
        self.assertEqual(parse_version("0.23.5"), (0, 23, 5))
        self.assertEqual(parse_version("0.24"), (0, 24, 0))
        self.assertEqual(parse_version(None), (0, 0, 0))


class TestParseModified(unittest.TestCase):
    NOW = time.mktime((2025, 6, 15, 13, 30, 0, 0, 0, -1))

    def test_days_count_from_local_midnight(self):
        midnight = time.mktime((2025, 6, 15, 0, 0, 0, 0, 0, -1))
        self.assertEqual(parse_modified("0", self.NOW), midnight)
        self.assertEqual(parse_modified("7", self.NOW), midnight - 7 * 86400)

    def test_dates_with_slash_or_dash(self):
        expected = time.mktime((2015, 1, 31, 0, 0, 0, 0, 0, -1))
        self.assertEqual(parse_modified("2015/01/31", self.NOW), expected)
        self.assertEqual(parse_modified("2015-01-31", self.NOW), expected)

    def test_garbage_is_rejected(self):
        for text in ("", "abc", "2015/13/01", "2015/1/1", "7 days", "-3"):
            self.assertIsNone(parse_modified(text, self.NOW), text)


class TestRunSearch(MockCase):
    def test_tag_search_is_substring_and_case_insensitive(self):
        self.assertEqual(self.files("title", "ALP"), ["rock/a1.flac"])

    def test_each_category_uses_its_tag(self):
        self.assertEqual(self.files("composer", "bach"), ["rock/a1.flac"])
        self.assertEqual(self.files("performer", "trio"), ["jazz/b1.flac"])
        self.assertEqual(self.files("comment", "live"), ["jazz/b1.flac"])
        self.assertEqual(self.files("genre", "jazz"), ["jazz/b1.flac"])
        self.assertEqual(self.files("date", "2001"), ["rock/a1.flac", "rock/a2.flac"])
        self.assertEqual(self.files("originaldate", "1970"), ["special/alphabet soup.flac"])
        self.assertEqual(self.files("file", "soup"), ["special/alphabet soup.flac"])

    def test_any_also_finds_file_names(self):
        # "alphabet" is only in a path; "Alpha" is a title
        self.assertEqual(sorted(self.files("any", "alpha")), ["rock/a1.flac", "special/alphabet soup.flac"])

    def test_any_does_not_list_a_song_twice(self):
        files = self.files("any", "rock")  # genre/artist and path both match
        self.assertEqual(files, ["rock/a1.flac", "rock/a2.flac"])

    def test_modified_since_days(self):
        since = time.mktime((2025, 5, 1, 0, 0, 0, 0, 0, -1))
        files = self.files(MODIFIED, "2025/05/01")
        self.assertEqual(sorted(files), ["jazz/b1.flac", "special/alphabet soup.flac"])
        sent = [c for c in search_mock.Handler.log if c[0] == "find"][0]
        self.assertEqual(sent[:3], ["find", MODIFIED, str(int(since))])

    def test_modified_bad_input_raises(self):
        with self.assertRaises(SearchError):
            run_search(self.client, MODIFIED, "yesterday")

    def test_empty_text_sends_nothing(self):
        self.assertEqual(run_search(self.client, "any", "   ").songs, [])
        self.assertEqual([c for c in search_mock.Handler.log if c[0] == "search"], [])

    def windows(self):
        return [c[-1] for c in search_mock.Handler.log if c[0] == "search"]

    def test_one_window_when_the_answer_fits(self):
        files = self.files("artist", "mass", chunk=10)
        self.assertEqual(len(files), 25)
        self.assertEqual(self.windows(), [f"0:{search.LIMIT + 1}"])

    def test_too_big_answer_falls_back_to_small_windows(self):
        search_mock.Handler.max_output = 2000  # ~10 songs per answer
        files = self.files("artist", "mass", chunk=5)
        self.assertEqual(len(files), 25)
        self.assertEqual(self.windows(), [f"0:{search.LIMIT + 1}", "0:5", "5:10", "10:15",
                                          "15:20", "20:25", "25:30"])
        # and the connection is usable afterwards
        self.assertEqual(self.files("title", "alpha"), ["rock/a1.flac"])

    def test_any_skips_file_pass_when_already_over_limit(self):
        run_search(self.client, "any", "many", limit=10)
        self.assertEqual([c[1] for c in search_mock.Handler.log if c[0] == "search"], ["any"])
        run_search(self.client, "any", "many", limit=100)
        self.assertEqual([c[1] for c in search_mock.Handler.log if c[0] == "search"], ["any", "any", "file"])

    def test_limit_truncates_and_says_so(self):
        r = run_search(self.client, "artist", "mass", limit=12, chunk=5)
        self.assertEqual(len(r.songs), 12)
        self.assertTrue(r.truncated)
        self.assertFalse(run_search(self.client, "artist", "mass", limit=25).truncated)

    def test_results_sorted_by_album_artist_album_track(self):
        files = self.files("artist", "mass")
        self.assertEqual(files[:3], ["many/000.flac", "many/001.flac", "many/002.flac"])
        self.assertEqual(files[-1], "many/024.flac")  # track 25 after track 3, numerically


class TestNoWindow(MockCase):
    settings = {"supports_window": False}

    def test_old_server_gets_one_plain_call(self):
        self.assertEqual(len(self.files("artist", "mass", chunk=10)), 25)


class TestSortAndStats(unittest.TestCase):
    def test_sort_album_artist_then_album_then_disc_track(self):
        songs = [dict(file="3", albumartist="B", album="X", track="1"),
                 dict(file="2", artist="A", album="Y", disc="2", track="1"),
                 dict(file="1", artist="A", album="Y", disc="1", track="9")]
        self.assertEqual([s["file"] for s in sorted(songs, key=song_sort_key)], ["1", "2", "3"])

    def test_stats(self):
        self.assertEqual(stats_text(Result([], False)), "No tracks found.")
        r = Result([{"duration": "3600"}, {"time": "61"}], False)
        self.assertEqual(stats_text(r), "2 Tracks (1:01:01)")
        self.assertEqual(stats_text(Result([{"duration": "5"}], True)), "1+ Track (0:05)")


class TestTagtypes(MockCase):
    def test_client_reads_tagtypes(self):
        self.assertIn("Composer", self.client.tagtypes())


if __name__ == "__main__":
    unittest.main()
