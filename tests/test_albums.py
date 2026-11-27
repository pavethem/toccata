import datetime
import unittest

from toccata import albums
from toccata.albums import AlbumEntry, RecentAlbums, fetch_album_tracks, fetch_album_uri, fetch_all_albums
from toccata.library import UNKNOWN
from toccata.mpd_client import MPDClient
from tests import albums_mock

NOW = datetime.datetime(2026, 11, 10, 12, 0, tzinfo=datetime.timezone.utc)


class MockCase(unittest.TestCase):
    def setUp(self):
        srv, port = albums_mock.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.c = MPDClient("127.0.0.1", port)
        self.c.connect()
        self.addCleanup(self.c.close)


class TestParse(unittest.TestCase):
    def test_group_values_are_carried_along(self):
        pairs = [("Date", ""), ("AlbumArtist", ""), ("Album", ""), ("AlbumArtist", "A"), ("Album", "X"),
                 ("Album", "Y"), ("Date", "1999"), ("AlbumArtist", "B"), ("Album", "Z")]
        got = [(e.artist, e.album, e.year) for e in albums.parse_album_list(pairs)]
        self.assertEqual(got, [("A", "X", 0), ("A", "Y", 0), ("B", "Z", 1999)])

    def test_merge_keeps_earliest_year(self):
        es = [AlbumEntry("A", "X", 2005), AlbumEntry("A", "X", 1999), AlbumEntry("A", "X", 0)]
        merged = albums.merge_albums(es)
        self.assertEqual([(e.album, e.year) for e in merged], [("X", 1999)])

    def test_sorts(self):
        es = [AlbumEntry("B", "a", 2001, "2020"), AlbumEntry("A", "c", 0, "2026"), AlbumEntry("A", "b", 1990, "2010")]
        self.assertEqual([e.album for e in albums.sort_albums(es, "artist")], ["b", "c", "a"])
        self.assertEqual([e.album for e in albums.sort_albums(es, "title")], ["a", "b", "c"])
        self.assertEqual([e.album for e in albums.sort_albums(es, "year")], ["a", "b", "c"])  # newest; unknown last
        self.assertEqual([e.album for e in albums.sort_albums(es, "added")], ["c", "a", "b"])

    def test_slice_filters(self):
        fs = albums.slice_filters("added", NOW)
        self.assertEqual(len(fs), len(albums.SLICES) + 1)
        self.assertEqual(fs[0], "(added-since '2026-10-11T12:00:00Z')")
        self.assertIn("(!(added-since '2026-10-11T12:00:00Z'))", fs[1])
        self.assertTrue(fs[-1].startswith("(!(added-since"))
        self.assertIn("modified-since", albums.slice_filters("modified", NOW)[0])

    def test_recent_field(self):
        self.assertEqual(albums.recent_field("0.24.0"), "added")
        self.assertEqual(albums.recent_field("0.23.5"), "modified")


class TestAllAlbums(MockCase):
    def test_every_album_once_with_untagged_ones(self):
        got = {(e.artist, e.album): (e.year, e.untagged) for e in fetch_all_albums(self.c)}
        self.assertEqual(got[("Oldie", "Ancient")], (1971, False))
        self.assertEqual(got[("Various Artists", "Mix")], (2000, False))
        self.assertEqual(got[("Solo", "Alone")], (1999, True))  # no album artist: under its artist
        self.assertNotIn((UNKNOWN, ""), got)
        self.assertEqual(len([k for k in got if k[0] == "Prolific"]), 30)

    def test_one_request_for_all_not_one_per_album(self):
        fetch_all_albums(self.c)
        from tests.lazy_mock import LazyHandler
        self.assertEqual([x for x in LazyHandler.log if x[0] == "find"], [])
        self.assertEqual(len([x for x in LazyHandler.log if x[0] == "list"]), 2)

    def test_tracks_and_uri(self):
        mix = AlbumEntry("Various Artists", "Mix")
        self.assertEqual([t["file"] for t in fetch_album_tracks(self.c, mix)], ["va/1.flac", "va/2.flac"])
        self.assertEqual(fetch_album_uri(self.c, AlbumEntry("Solo", "Alone", untagged=True)), "solo/1.flac")
        solo = fetch_album_tracks(self.c, AlbumEntry("Solo", "Alone", untagged=True))
        self.assertEqual([t["file"] for t in solo], ["solo/1.flac"])  # not the compilation's track by Solo
        self.assertEqual(fetch_album_uri(self.c, AlbumEntry("Nope", "Nothing")), "")


class TestTagFallback(MockCase):
    """MPD 0.24 matches a missing AlbumArtist against the Artist, so
    (AlbumArtist == '') finds nothing (user report 2026-11-16: "No tracks found"
    for a newly added album without album artist)."""
    def setUp(self):
        srv, port = albums_mock.start(tag_fallback=True)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.c = MPDClient("127.0.0.1", port)
        self.c.connect()
        self.addCleanup(self.c.close)

    def test_untagged_album_found(self):
        solo = AlbumEntry("Solo", "Alone", untagged=True)
        self.assertEqual([t["file"] for t in fetch_album_tracks(self.c, solo)], ["solo/1.flac"])
        self.assertEqual(fetch_album_uri(self.c, solo), "solo/1.flac")

    def test_recent_untagged_album_has_tracks(self):
        r = RecentAlbums("added", now=NOW)
        entries = []
        while not r.done:
            entries += r.load_more(self.c)
        solo = [e for e in entries if e.key == ("Solo", "Alone") and e.untagged]
        self.assertEqual(len(solo), 1)
        self.assertEqual([t["file"] for t in fetch_album_tracks(self.c, solo[0])], ["solo/1.flac"])


class TestRecent(MockCase):
    def walk(self, chunk=1000):
        r = RecentAlbums("added", now=NOW, chunk=chunk)
        out, calls = [], 0
        while not r.done:
            out += r.load_more(self.c)
            calls += 1
        return out, calls

    def test_newest_first_and_each_album_once(self):
        got, _ = self.walk()
        order = [e.album for e in got]
        self.assertEqual(order[:3], ["Fresh", "Mix", "Record 15"])  # Mix: its newest track counts
        keys = [e.key for e in got]
        self.assertEqual(len(keys), len(set(keys)))  # each (artist, album) once
        self.assertEqual(order.index("Ancient") > order.index("Alone"), True)
        self.assertNotIn("", order)
        mix = next(e for e in got if e.album == "Mix")
        self.assertEqual(mix.added, "2026-10-01T00:00:00Z")
        self.assertEqual(mix.uri, "va/2.flac")

    def test_small_windows_never_sort_the_whole_library(self):
        got, calls = self.walk(chunk=4)
        self.assertEqual(len(got), 35)  # Fresh, Mix, Alone (Solo), Alone (VA), Ancient, 30 Records
        for find in albums_mock.Handler.finds:
            self.assertIn("added-since", find[1])  # always a time slice, never the whole library unfiltered
            self.assertEqual(find[2:4], ["sort", "-Added"])
            lo, hi = map(int, find[5].split(":"))
            self.assertLessEqual(hi - lo, 4)

    def test_load_more_returns_something_until_done(self):
        r = RecentAlbums("added", now=NOW, chunk=4)
        first = r.load_more(self.c)
        self.assertTrue(first)
        self.assertFalse(r.done)


if __name__ == "__main__":
    unittest.main()


class TestPrefixSorting(unittest.TestCase):
    def test_artist_and_title_ignore_prefixes(self):
        es = [AlbumEntry("The Beatles", "The Abbey Road Sessions", 1969), AlbumEntry("Blur", "Parklife", 1994),
              AlbumEntry("Air", "Moon Safari", 1998)]
        self.assertEqual([e.artist for e in albums.sort_albums(es, "artist", ("The",))], ["Air", "The Beatles", "Blur"])
        self.assertEqual([e.artist for e in albums.sort_albums(es, "artist")], ["Air", "Blur", "The Beatles"])
        self.assertEqual([e.album for e in albums.sort_albums(es, "title", ("The",))],
                         ["The Abbey Road Sessions", "Moon Safari", "Parklife"])  # as "Abbey Road Sessions, The"
        self.assertEqual([e.album for e in albums.sort_albums(es, "title")][-1], "The Abbey Road Sessions")
