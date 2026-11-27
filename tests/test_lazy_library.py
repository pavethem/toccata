import threading
import unittest

from toccata.library import (CHUNK, UNKNOWN, LibraryFetcher, count_songs, fetch_artist, fetch_db_stamp, fetch_newest_since,
                                 fetch_artist_names, filter_quote)
from toccata.mpd_client import CommandError, MPDClient, ProtocolError
from tests import lazy_mock
from tests.lazy_mock import LazyHandler

ALL_NAMES = ["Band", "Big Band", "Guns N' Roses", "M\u00f6tley N' Cr\u00fce", "Solo", UNKNOWN, "Various Artists"]


class ServerCase(unittest.TestCase):
    """One fresh server per test, so settings never leak between tests."""

    def serve(self, db=None, **settings):
        srv, port = lazy_mock.start(db=db, **settings)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        c = MPDClient("127.0.0.1", port)
        c.connect()
        self.addCleanup(c.close)
        self.port = port
        return c

    def finds(self):
        return [a for a in LazyHandler.log if a[0] == "find"]


class TestArtistNames(ServerCase):
    def test_includes_album_artists_untagged_artists_and_unknown_sorted(self):
        self.assertEqual(fetch_artist_names(self.serve()), ALL_NAMES)

    def test_track_artists_of_a_compilation_are_not_listed(self):
        names = fetch_artist_names(self.serve())
        for track_artist in ("A", "B", "C"):
            self.assertNotIn(track_artist, names)

    def test_old_mpd_without_filters_still_lists_album_artists(self):
        c = self.serve(supports_filters=False)
        self.assertEqual(fetch_artist_names(c), ["Band", "Big Band", "Guns N' Roses", "Various Artists"])
        self.assertEqual(c.status()["state"], "stop")  # rejected queries left the connection in sync

    def test_no_empty_names(self):
        self.assertNotIn("", fetch_artist_names(self.serve()))

    def test_unknown_only_when_some_song_has_no_artist_at_all(self):
        db = [s for s in lazy_mock.make_db() if s["file"] != "loose/untagged.flac"]
        self.assertNotIn(UNKNOWN, fetch_artist_names(self.serve(db=db)))

    def test_count_songs(self):
        self.assertEqual(count_songs(self.serve(), "(AlbumArtist == '')"), 4)  # 2 solo + motley + loose
        self.assertEqual(count_songs(self.serve(db=[]), "(AlbumArtist == '')"), 0)

    def test_count_songs_none_when_filters_unsupported(self):
        self.assertIsNone(count_songs(self.serve(supports_filters=False), "(AlbumArtist == '')"))


class TestFetchArtist(ServerCase):
    def test_albums_sorted_with_year_and_tracks(self):
        artist = fetch_artist(self.serve(), "Band")
        self.assertEqual([(a.title, a.year, a.n_tracks) for a in artist.albums],
                         [("Debut", 2019, 3), ("Second", 2021, 1)])
        self.assertEqual([t["title"] for t in artist.albums[0].tracks], ["Song 1", "Song 2", "Song 3"])

    def test_asks_in_windows_and_with_old_style_syntax(self):
        c = self.serve()
        fetch_artist(c, "Band")
        first = self.finds()[0]
        self.assertEqual(first[:3], ["find", "albumartist", "Band"])
        self.assertEqual(first[3:], ["window", f"0:{CHUNK}"])

    def test_songs_without_album_artist_are_found_under_their_artist(self):
        artist = fetch_artist(self.serve(), "Solo")
        self.assertEqual([a.title for a in artist.albums], ["Solo Album"])
        self.assertEqual(artist.albums[0].n_tracks, 2)

    def test_apostrophes_and_umlauts_survive_the_filter(self):
        name = "M\u00f6tley N' Cr\u00fce"
        artist = fetch_artist(self.serve(), name)
        self.assertEqual([t["title"] for t in artist.tracks()], ["Kickstart"])
        filters = [a[1] for a in self.finds() if a[1].startswith("(")]
        self.assertIn("N\\' Cr", filters[0])  # the apostrophe was escaped inside the expression

    def test_apostrophe_artist_with_album_artist_tag(self):
        self.assertEqual(len(fetch_artist(self.serve(), "Guns N' Roses").tracks()), 2)

    def test_unknown_collects_completely_untagged_songs(self):
        artist = fetch_artist(self.serve(), UNKNOWN)
        self.assertEqual([t["file"] for t in artist.tracks()], ["loose/untagged.flac"])
        self.assertEqual(artist.albums[0].title, UNKNOWN)

    def test_compilation_is_one_album_with_many_track_artists(self):
        artist = fetch_artist(self.serve(), "Various Artists")
        self.assertEqual(len(artist.albums), 1)
        self.assertEqual([t["artist"] for t in artist.albums[0].tracks], ["A", "B", "C"])

    def test_no_songs_gives_an_empty_artist_not_a_crash(self):
        artist = fetch_artist(self.serve(), "Nobody")
        self.assertEqual((artist.name, artist.albums), ("Nobody", []))

    def test_no_duplicates_if_the_server_already_falls_back_by_itself(self):
        artist = fetch_artist(self.serve(falls_back_itself=True), "Solo")
        self.assertEqual(len(artist.tracks()), 2)

    def test_old_mpd_without_filters_still_returns_tagged_songs(self):
        c = self.serve(supports_filters=False)
        self.assertEqual(len(fetch_artist(c, "Band").tracks()), 4)
        self.assertEqual(c.status()["state"], "stop")

    def test_old_mpd_without_window_gets_a_plain_find(self):
        c = self.serve(supports_window=False)
        self.assertEqual(len(fetch_artist(c, "Band").tracks()), 4)
        self.assertEqual(len(fetch_artist(c, "Solo").tracks()), 2)  # fallback query without window too

    def test_failure_of_the_main_query_propagates_and_connection_survives(self):
        c = self.serve(fail_find=True)
        with self.assertRaises(CommandError):
            fetch_artist(c, "Band")
        self.assertEqual(c.status()["state"], "stop")


class TestOutputBufferLimit(ServerCase):
    """The reason this design exists: MPD drops the connection when one answer
    exceeds max_output_buffer_size."""
    LIMIT = 20_000  # the 600 'Big Band' songs are ~57 KB; 100 of them ~9.5 KB

    def test_asking_for_everything_at_once_is_what_breaks(self):
        c = self.serve(max_output=self.LIMIT)
        with self.assertRaises(ProtocolError) as cm:
            list(c.iter_songs("find", "albumartist", "Big Band"))  # no window: one big answer
        self.assertIn("closed", str(cm.exception).lower())

    def test_chunked_fetch_stays_under_the_limit(self):
        c = self.serve(max_output=self.LIMIT)
        artist = fetch_artist(c, "Big Band", chunk=100)
        self.assertEqual(len(artist.tracks()), 600)
        self.assertEqual(len(artist.albums), 6)
        self.assertEqual(c.status()["state"], "stop")  # connection survived

    def test_default_chunk_is_safely_below_mpds_default_buffer(self):
        # Real songs with full tags are a few hundred bytes; assume a pessimistic 1 KB
        # and keep a 4x margin below MPD's default 8 MiB output buffer.
        self.assertLess(CHUNK * 1024, 8 * 1024 * 1024 // 4)

    def test_artist_list_is_small_regardless_of_library_size(self):
        c = self.serve(max_output=self.LIMIT)
        self.assertEqual(fetch_artist_names(c), ALL_NAMES)  # names only: well under the limit

    def test_window_requests_cover_the_whole_artist(self):
        c = self.serve(max_output=self.LIMIT)
        fetch_artist(c, "Big Band", chunk=100)
        windows = [a[-1] for a in self.finds() if a[1] == "albumartist"]
        self.assertEqual(windows, ["0:100", "100:200", "200:300", "300:400", "400:500", "500:600", "600:700"])


class TestFilterQuote(unittest.TestCase):
    def test_quote(self):
        self.assertEqual(filter_quote("plain"), "'plain'")
        self.assertEqual(filter_quote("it's"), "'it\\'s'")
        self.assertEqual(filter_quote("a\\b"), "'a\\\\b'")
        self.assertEqual(filter_quote(""), "''")


class TestLibraryFetcher(ServerCase):
    def fetcher(self, client=None, **kw):
        if client is None:
            self.serve()
            client = MPDClient("127.0.0.1", self.port)
        f = LibraryFetcher(client, lambda fn, *a: fn(*a), **kw)
        self.addCleanup(f.shutdown)
        return f

    @staticmethod
    def wait_for(register):
        done, out = threading.Event(), []
        register(lambda result, err: (out.append((result, err)), done.set()))
        return out[0] if done.wait(3) else "TIMEOUT"

    def test_load_names(self):
        f = self.fetcher()
        names, err = self.wait_for(f.load_names)
        self.assertEqual((names, err), (ALL_NAMES, None))

    def test_load_artist_and_cache(self):
        f = self.fetcher()
        artist, err = self.wait_for(lambda cb: f.load_artist("Band", cb))
        self.assertIsNone(err)
        self.assertEqual(len(artist.tracks()), 4)
        self.assertIs(f.cached("Band"), artist)
        self.assertIsNone(f.cached("Solo"))

    def test_load_names_clears_the_cache(self):
        f = self.fetcher()
        self.wait_for(lambda cb: f.load_artist("Band", cb))
        self.assertIsNotNone(f.cached("Band"))
        self.wait_for(f.load_names)
        self.assertIsNone(f.cached("Band"))  # the database may have changed

    def test_result_from_before_a_reload_is_not_cached(self):
        f = self.fetcher()
        artist = fetch_artist(self._connected(), "Band")
        f._epoch = 5
        f._finish(artist, None, ("Band", None), lambda r, e: None, epoch=4)  # started before the reload
        self.assertIsNone(f.cached("Band"))
        f._finish(artist, None, ("Band", None), lambda r, e: None, epoch=5)
        self.assertIs(f.cached("Band"), artist)

    def _connected(self):
        c = MPDClient("127.0.0.1", self.port)
        c.connect()
        self.addCleanup(c.close)
        return c

    def test_lru_eviction(self):
        f = self.fetcher(cache_size=2)
        for name in ("Band", "Solo", "Various Artists"):
            self.wait_for(lambda cb, n=name: f.load_artist(n, cb))
        self.assertIsNone(f.cached("Band"))
        self.assertIsNotNone(f.cached("Solo"))
        self.assertIsNotNone(f.cached("Various Artists"))

    def test_unwanted_request_is_skipped_without_querying(self):
        f = self.fetcher()
        self.wait_for(f.load_names)
        before = len(LazyHandler.log)
        called = []
        f.load_artist("Band", lambda r, e: called.append(1), wanted=lambda: False)
        f._pool.shutdown(wait=True)
        self.assertEqual(len(LazyHandler.log), before)
        self.assertEqual(called, [])

    def test_unreachable_server_reports_an_error(self):
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        f = self.fetcher(client=MPDClient("127.0.0.1", port))
        result, err = self.wait_for(f.load_names)
        self.assertIsNone(result)
        self.assertIsInstance(err, OSError)

    def test_reconnects_once_when_the_connection_was_dropped(self):
        self.serve()
        attempts = []

        class Flaky(MPDClient):
            def connect(self):
                attempts.append(1)
                if len(attempts) == 1:
                    raise ConnectionResetError()
                super().connect()
        f = self.fetcher(client=Flaky("127.0.0.1", self.port))
        names, err = self.wait_for(f.load_names)
        self.assertEqual((names, err), (ALL_NAMES, None))
        self.assertEqual(len(attempts), 2)

    def test_command_error_is_reported_without_reconnecting_and_connection_survives(self):
        self.serve(fail_find=True)
        client = MPDClient("127.0.0.1", self.port)
        connects = []
        real_connect = client.connect
        client.connect = lambda: (connects.append(1), real_connect())[1]
        f = self.fetcher(client=client)
        artist, err = self.wait_for(lambda cb: f.load_artist("Band", cb))
        self.assertIsNone(artist)
        self.assertIsInstance(err, CommandError)
        self.assertEqual(connects, [1])  # an ACK is the server's answer: no reconnect, no retry
        names, err2 = self.wait_for(f.load_names)  # same connection still works
        self.assertEqual((names, err2), (ALL_NAMES, None))


if __name__ == "__main__":
    unittest.main()


class TestNewestSince(ServerCase):
    """After a database update the artist list shows what it added (user request 2026-11-20)."""

    def db(self):
        db = lazy_mock.make_db()
        for s in db:
            s["Added"] = "2025-01-01T00:00:00Z"
        db.append({"file": "new/1.flac", "Title": "N1", "AlbumArtist": "Newcomer", "Artist": "Newcomer",
                   "Album": "Fresh", "Added": "2026-11-20T10:00:01Z", "Last-Modified": "2020-01-01T00:00:00Z"})
        db.append({"file": "new/2.flac", "Title": "N2", "Artist": "Loner", "Album": "Older",
                   "Added": "2026-11-20T09:59:59Z", "Last-Modified": "2026-11-20T11:00:00Z"})
        return db


    def test_db_stamp(self):
        self.assertEqual(fetch_db_stamp(self.serve(db_update=123)), 123)
        c = self.serve()  # `stats` refused
        self.assertIsNone(fetch_db_stamp(c))
        self.assertEqual(c.status()["state"], "stop")

    def test_newest_added_song_gives_artist_and_album(self):
        since = 1795168800  # 2026-11-20T10:00:00Z
        self.assertEqual(fetch_newest_since(self.serve(db=self.db()), since), ("Newcomer", "Fresh"))

    def test_the_newest_of_several_new_songs_wins(self):
        db = self.db()
        db.insert(0, {"file": "new/0.flac", "Title": "N0", "AlbumArtist": "Early", "Album": "Before",
                      "Added": "2026-11-20T10:00:00Z", "Last-Modified": "2020-01-01T00:00:00Z"})
        self.assertEqual(fetch_newest_since(self.serve(db=db), 1795168800), ("Newcomer", "Fresh"))

    def test_older_server_uses_modified_time(self):
        db = self.db()
        db.insert(0, {"file": "new/0.flac", "Title": "N0", "AlbumArtist": "Early", "Album": "Before",
                      "Added": "2020-01-01T00:00:00Z", "Last-Modified": "2026-11-20T10:30:00Z"})
        self.assertEqual(fetch_newest_since(self.serve(db=db), 1795168800, "modified"), ("Loner", "Older"))

    def test_nothing_new_or_no_stamp(self):
        c = self.serve(db=self.db())
        self.assertIsNone(fetch_newest_since(c, 1795176000))  # 12:00: nothing added since
        self.assertIsNone(fetch_newest_since(c, None))

    def test_refused_filter_is_none(self):
        c = self.serve(db=self.db(), supports_filters=False)
        self.assertIsNone(fetch_newest_since(c, 1795168800))
        self.assertEqual(c.status()["state"], "stop")

    def test_composer_genre_song_is_filed_under_its_composer(self):
        db = lazy_mock.composer_db()
        for s in db:
            s["Added"] = "2025-01-01T00:00:00Z"
        next(s for s in db if s["file"] == "figaro/1.flac")["Added"] = "2026-11-20T10:00:05Z"  # Classical + Opera
        c = self.serve(db=db)
        self.assertEqual(fetch_newest_since(c, 1795168800, composer_genres=("Classical",)), ("Mozart", "Figaro"))
        self.assertEqual(fetch_newest_since(c, 1795168800), ("Wiener Philharmoniker", "Figaro"))
        self.assertEqual(fetch_newest_since(c, 1795168800, composer_genres=("Opera",)), ("Mozart", "Figaro"))

    def test_composer_outside_composer_genres_stays_with_album_artist(self):
        db = lazy_mock.composer_db()
        for s in db:
            s["Added"] = "2026-11-20T10:00:05Z" if s["file"] == "film/1.flac" else "2025-01-01T00:00:00Z"
        c = self.serve(db=db)
        self.assertEqual(fetch_newest_since(c, 1795168800, composer_genres=("Classical",)),
                         ("Berliner Philharmoniker", "Film"))

    def test_untitled_album(self):
        db = [{"file": "x.flac", "Title": "t", "Artist": "Solo", "Added": "2026-11-20T10:00:05Z"}]
        self.assertEqual(fetch_newest_since(self.serve(db=db), 1795168800), ("Solo", UNKNOWN))
