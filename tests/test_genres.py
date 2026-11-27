import threading
import unittest

from toccata.library import (UNKNOWN, LibraryFetcher, fetch_artist, fetch_artist_names, fetch_genres,
                                 genre_filter)
from toccata.mpd_client import MPDClient
from tests import lazy_mock
from tests.lazy_mock import LazyHandler, parse_filter


class GenreCase(unittest.TestCase):
    def serve(self, **settings):
        srv, port = lazy_mock.start(db=lazy_mock.genre_db(), **settings)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        c = MPDClient("127.0.0.1", port)
        c.connect()
        self.addCleanup(c.close)
        self.port = port
        return c


class TestMockFilter(unittest.TestCase):
    """The mock's filter parser must itself be right, or the tests below prove nothing."""

    def test_and_nesting_and_multi_values(self):
        song = {"Genre": ["Jazz", "Funk"], "Artist": "A"}
        self.assertTrue(parse_filter("(Genre == 'Funk')")(song))
        self.assertTrue(parse_filter("((Genre == 'Jazz') AND (Artist == 'A'))")(song))
        self.assertFalse(parse_filter("((Genre == 'Jazz') AND (Artist == 'B'))")(song))
        self.assertTrue(parse_filter("((AlbumArtist == '') AND (Artist == 'A') AND (Genre == 'Jazz'))")(song))
        with self.assertRaises(ValueError):
            parse_filter("Genre == 'x'")


class TestGenreQueries(GenreCase):
    def test_genre_list_sorted_without_empty(self):
        c = self.serve()
        self.assertEqual(fetch_genres(c), ["Funk", "Jazz", "Rock"])

    def test_filter_quotes_apostrophes(self):
        self.assertEqual(genre_filter("Rock 'n' Roll"), "(Genre == 'Rock \\'n\\' Roll')")

    def test_artists_of_a_genre_include_untagged_and_unknown(self):
        c = self.serve()
        self.assertEqual(fetch_artist_names(c, "Jazz"), ["Busker", "Hornist", UNKNOWN])
        self.assertEqual(fetch_artist_names(c, "Funk"), ["Hornist"])
        self.assertEqual(fetch_artist_names(c, "Rock"), ["Band", "Hornist"])

    def test_no_genre_lists_everyone(self):
        c = self.serve()
        self.assertIn("Big Band", fetch_artist_names(c))

    def test_artist_shows_only_the_genres_tracks(self):
        c = self.serve()
        jazz = fetch_artist(c, "Hornist", genre="Jazz")
        self.assertEqual([a.title for a in jazz.albums], ["Blue"])  # "Loud" is Rock only
        self.assertEqual([t["file"] for t in jazz.albums[0].tracks], ["jazz/1.flac", "jazz/2.flac"])
        band = fetch_artist(c, "Band", genre="Rock")
        self.assertEqual([t["file"] for t in band.tracks()], ["rock/1.flac"])
        self.assertEqual(len(fetch_artist(c, "Band").tracks()), 5)  # unfiltered: all of them

    def test_untagged_artist_and_unknown_with_genre(self):
        c = self.serve()
        self.assertEqual([t["file"] for t in fetch_artist(c, "Busker", genre="Jazz").tracks()], ["jazz/4.flac"])
        self.assertEqual([t["file"] for t in fetch_artist(c, UNKNOWN, genre="Jazz").tracks()], ["jazz/5.flac"])

    def test_old_server_without_filter_expressions(self):
        c = self.serve(supports_filters=False)
        self.assertEqual(fetch_artist_names(c, "Jazz"), ["Hornist"])  # old `genre X` syntax
        self.assertEqual([a.title for a in fetch_artist(c, "Hornist", genre="Jazz").albums], ["Blue"])
        sent = [a for a in LazyHandler.log if a[0] == "list" and a[1] == "albumartist"]
        self.assertIn(["list", "albumartist", "genre", "Jazz"], sent)


class TestFetcherCache(GenreCase):
    def wait(self, start):
        done, out = threading.Event(), []
        start(lambda r, e: (out.append((r, e)), done.set()))
        self.assertTrue(done.wait(5))
        return out[0]

    def test_cache_is_per_genre_and_survives_a_genre_switch(self):
        self.serve()
        f = LibraryFetcher(MPDClient("127.0.0.1", self.port), lambda fn, *a: fn(*a))
        self.addCleanup(f.shutdown)
        jazz, _ = self.wait(lambda cb: f.load_artist("Hornist", cb, genre="Jazz"))
        full, _ = self.wait(lambda cb: f.load_artist("Hornist", cb))
        self.assertIs(f.cached("Hornist", "Jazz"), jazz)
        self.assertIs(f.cached("Hornist"), full)
        self.assertEqual(len(jazz.tracks()), 2)
        self.assertEqual(len(full.tracks()), 3)
        names, _ = self.wait(lambda cb: f.load_names(cb, genre="Jazz", fresh=False))
        self.assertEqual(names, ["Busker", "Hornist", UNKNOWN])
        self.assertIs(f.cached("Hornist", "Jazz"), jazz)  # a genre switch keeps the cache
        self.wait(lambda cb: f.load_names(cb))
        self.assertIsNone(f.cached("Hornist", "Jazz"))  # a database reload does not


if __name__ == "__main__":
    unittest.main()
