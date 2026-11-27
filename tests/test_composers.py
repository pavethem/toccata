import threading
import unittest

from toccata.library import (LibraryFetcher, fetch_artist, fetch_artist_names, not_composer_songs,
                             resolve_genres)
from toccata.mpd_client import MPDClient
from tests import lazy_mock


class Case(unittest.TestCase):
    def setUp(self):
        srv, port = lazy_mock.start(db=lazy_mock.composer_db())
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.port = port
        self.c = MPDClient("127.0.0.1", port)
        self.c.connect()
        self.addCleanup(self.c.close)

    def files(self, name, **kw):
        return sorted(t["file"] for t in fetch_artist(self.c, name, **kw).tracks())


class TestRules(unittest.TestCase):
    def test_resolve_ignores_case_and_keeps_server_spelling(self):
        self.assertEqual(resolve_genres(["Classical", "Opera", "Rock"], ["classical", "OPERA", "Jazz"]),
                         ("Classical", "Opera"))
        self.assertEqual(resolve_genres(["Rock"], ["Classical"]), ())

    def test_not_composer_songs_has_no_or(self):
        f = not_composer_songs(("Classical", "Opera"))
        self.assertNotIn(" OR ", f)
        self.assertEqual(f.count("(!"), 2)


class TestArtistList(Case):
    def test_off_album_artists_as_before(self):
        names = fetch_artist_names(self.c)
        self.assertIn("Berliner Philharmoniker", names)
        self.assertIn("Wiener Philharmoniker", names)
        self.assertNotIn("Johann Sebastian Bach", names)

    def test_composers_replace_album_artists_for_those_genres(self):
        names = fetch_artist_names(self.c, composer_genres=("Classical",))
        self.assertIn("Johann Sebastian Bach", names)
        self.assertIn("Mozart", names)
        self.assertIn("Berliner Philharmoniker", names)  # still has the film score
        self.assertNotIn("Wiener Philharmoniker", names)  # only composer songs: gone
        self.assertIn("Anon Ensemble", names)  # classical, but no composer tag: stays
        self.assertNotIn("Hans Zimmer", names)  # composer of a non-classical song
        self.assertIn("Band", names)
        self.assertIn("Chopin", names)
        self.assertNotIn("Solo Pianist", names)  # no album artist; its only song is a composer song

    def test_a_song_in_any_of_several_genres_counts(self):
        names = fetch_artist_names(self.c, composer_genres=("Opera",))
        self.assertIn("Mozart", names)
        self.assertNotIn("Johann Sebastian Bach", names)
        self.assertNotIn("Wiener Philharmoniker", names)  # its only song is an opera with a composer

    def test_with_the_genre_filter(self):
        self.assertEqual(fetch_artist_names(self.c, genre="Opera", composer_genres=("Classical",)), ["Mozart"])


class TestArtistSongs(Case):
    def test_composer_gets_the_songs(self):
        bach = fetch_artist(self.c, "Johann Sebastian Bach", composer_genres=("Classical",))
        self.assertEqual([a.title for a in bach.albums], ["Brandenburg"])
        self.assertEqual(len(bach.albums[0].tracks), 2)
        self.assertEqual(bach.albums[0].artist, "Johann Sebastian Bach")

    def test_album_artist_keeps_only_the_rest(self):
        self.assertEqual(self.files("Berliner Philharmoniker", composer_genres=("Classical",)), ["film/1.flac"])
        self.assertEqual(self.files("Berliner Philharmoniker"),
                         ["bach/1.flac", "bach/2.flac", "film/1.flac"])  # off: everything, as before
        self.assertEqual(self.files("Anon Ensemble", composer_genres=("Classical",)), ["chant/1.flac"])

    def test_song_in_two_composer_genres_is_listed_once(self):
        self.assertEqual(self.files("Mozart", composer_genres=("Classical", "Opera")), ["figaro/1.flac"])


class TestFetcher(Case):
    def wait(self, start):
        done, out = threading.Event(), []
        start(lambda r, e: (out.append((r, e)), done.set()))
        self.assertTrue(done.wait(5))
        return out[0]

    def test_typed_genres_are_resolved_on_the_server(self):
        f = LibraryFetcher(MPDClient("127.0.0.1", self.port), lambda fn, *a: fn(*a))
        self.addCleanup(f.shutdown)
        f.composer_genres = ("classical",)  # as typed: lower case
        names, err = self.wait(lambda cb: f.load_names(cb))
        self.assertIsNone(err)
        self.assertIn("Johann Sebastian Bach", names)
        bach, _ = self.wait(lambda cb: f.load_artist("Johann Sebastian Bach", cb))
        self.assertEqual(len(bach.tracks()), 2)
        f.composer_genres = ()
        names, _ = self.wait(lambda cb: f.load_names(cb))
        self.assertNotIn("Johann Sebastian Bach", names)


if __name__ == "__main__":
    unittest.main()


class TestGenreField(unittest.TestCase):
    def test_long_genre_names_are_kept(self):
        from toccata.library import parse_genres
        g = "Contemporary Avant-Garde Classical"
        self.assertEqual(parse_genres(f"Classical, {g}, classical,, "), ("Classical", g))

    def test_settings_keep_long_genres(self):
        from toccata import settings as st
        g = "Contemporary Avant-Garde Classical"
        self.assertEqual(st._clean({"composer_genres": ["Classical", g]}).composer_genres, ["Classical", g])
