import unittest

from toccata.mpd_client import CommandError, MPDClient
from toccata.playlists import (duplicate_positions, invalid_positions, is_invalid, is_overwrite,
                                   name_problem, sorted_playlists, total_duration)
from tests import search_mock


class TestRules(unittest.TestCase):
    def test_duplicates_keep_the_first(self):
        a = dict(file="a", artist="X", album="A", title="T", track="1")
        b = dict(file="b", artist="X", album="A", title="U", track="2")
        songs = [a, b, dict(a, file="a-copy"), a, b]
        self.assertEqual(duplicate_positions(songs), [2, 3, 4])

    def test_same_title_on_another_album_is_no_duplicate(self):
        songs = [dict(file="a", artist="X", album="A", title="T"),
                 dict(file="b", artist="X", album="Live", title="T")]
        self.assertEqual(duplicate_positions(songs), [])

    def test_album_artist_counts_in_the_key(self):
        songs = [dict(file="a", albumartist="VA", artist="X", album="H", title="T"),
                 dict(file="b", albumartist="Other", artist="X", album="H", title="T")]
        self.assertEqual(duplicate_positions(songs), [])

    def test_invalid_means_file_only(self):
        self.assertTrue(is_invalid({"file": "gone.flac"}))
        self.assertFalse(is_invalid({"file": "x.flac", "duration": "1.0"}))
        self.assertFalse(is_invalid({"file": "x.flac", "title": "t"}))
        self.assertFalse(is_invalid({"file": "http://radio/stream"}))
        self.assertEqual(invalid_positions([{"file": "a", "time": "3"}, {"file": "b"}]), [1])

    def test_names(self):
        self.assertIsNotNone(name_problem("  "))
        self.assertIsNotNone(name_problem("a/b"))
        self.assertIsNotNone(name_problem("a\nb"))
        self.assertIsNotNone(name_problem("same", current="same"))
        self.assertIsNone(name_problem("Party Mix"))
        self.assertTrue(is_overwrite("x", ["x", "y"]))
        self.assertFalse(is_overwrite("z", ["x", "y"]))
        self.assertFalse(is_overwrite("x", ["x"], current="x"))

    def test_sorted_and_duration(self):
        self.assertEqual([n for n, _ in sorted_playlists([("b", ""), ("A 10", ""), ("a 9", "")])],
                         ["a 9", "A 10", "b"])
        self.assertEqual(total_duration([{"duration": "1.5"}, {"time": "2"}, {"file": "x"}]), 3.5)


class TestClientCommands(unittest.TestCase):
    def setUp(self):
        srv, port = search_mock.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.c = MPDClient("127.0.0.1", port)
        self.c.connect()
        self.addCleanup(self.c.close)
        self.pl = search_mock.Handler.playlists

    def test_add_list_and_info(self):
        self.c.playlist_add("Mix", ["rock/a1.flac", "gone.flac"])
        self.assertEqual([n for n, _ in self.c.listplaylists()], ["Mix"])
        self.assertEqual(self.c.listplaylists()[0][1], "2025-01-01T00:00:00Z")
        info = self.c.listplaylistinfo("Mix")
        self.assertEqual(info[0]["title"], "Alpha")
        self.assertEqual(invalid_positions(info), [1])

    def test_add_in_chunks(self):
        self.c.playlist_add("Big", [f"f{i}" for i in range(7)], chunk=3)
        self.assertEqual(len(self.pl["Big"]), 7)

    def test_delete_positions_highest_first(self):
        self.pl["P"] = ["a", "b", "c", "d"]
        self.c.playlist_delete("P", [0, 2])
        self.assertEqual(self.pl["P"], ["b", "d"])

    def test_move(self):
        self.pl["P"] = ["a", "b", "c"]
        self.c.playlist_move("P", 0, 2)
        self.assertEqual(self.pl["P"], ["b", "c", "a"])

    def test_save_and_overwrite(self):
        search_mock.Handler.queue[:] = ["q1", "q2"]
        self.c.save_playlist("Q")
        self.assertEqual(self.pl["Q"], ["q1", "q2"])
        search_mock.Handler.queue[:] = ["q3"]
        with self.assertRaises(CommandError):
            self.c.save_playlist("Q")
        self.c.save_playlist("Q", overwrite=True)
        self.assertEqual(self.pl["Q"], ["q3"])

    def test_load_appends_or_inserts(self):
        self.pl["P"] = ["a", "b"]
        search_mock.Handler.queue[:] = ["x", "y"]
        self.c.load_playlist("P")
        self.assertEqual(search_mock.Handler.queue, ["x", "y", "a", "b"])
        self.c.load_playlist("P", position=1)
        self.assertEqual(search_mock.Handler.queue[:3], ["x", "a", "b"])

    def test_rename_and_delete(self):
        self.pl["Old"] = ["a"]
        self.c.rename_playlist("Old", "New")
        self.assertEqual(list(self.pl), ["New"])
        self.c.delete_playlist("New")
        self.assertEqual(self.pl, {})
        with self.assertRaises(CommandError):
            self.c.delete_playlist("New")


if __name__ == "__main__":
    unittest.main()


class TestSuggest(unittest.TestCase):
    NAMES = ["Party", "evening jazz", "Morning Jazz", "Jazz Classics", "Mix"]

    def test_everything_sorted_when_empty(self):
        from toccata.playlists import suggest
        self.assertEqual(suggest(self.NAMES, ""), ["evening jazz", "Jazz Classics", "Mix", "Morning Jazz", "Party"])

    def test_prefix_first_then_contains(self):
        from toccata.playlists import suggest
        self.assertEqual(suggest(self.NAMES, "jazz"), ["Jazz Classics", "evening jazz", "Morning Jazz"])
        self.assertEqual(suggest(self.NAMES, "  M "), ["Mix", "Morning Jazz"])
        self.assertEqual(suggest(self.NAMES, "ing"), ["evening jazz", "Morning Jazz"])  # only "contains"
        self.assertEqual(suggest(self.NAMES, "zzz"), [])

    def test_limit(self):
        from toccata.playlists import suggest
        self.assertEqual(len(suggest([f"p{i}" for i in range(100)], "", limit=10)), 10)
