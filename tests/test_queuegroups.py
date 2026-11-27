import unittest

from toccata.queuegroups import (drop_index, group_runs, header_title, layout, prune_collapsed, shown_row,
                                     track_title)


def s(i, **kw):
    return dict(file=kw.pop("file", f"f{i}.flac"), id=str(i), **kw)


class TestRuns(unittest.TestCase):
    def test_consecutive_tracks_of_an_album_form_one_group(self):
        songs = [s(1, albumartist="A", album="X", duration="60"), s(2, albumartist="A", album="X", duration="30"),
                 s(3, albumartist="B", album="Y")]
        g = group_runs(songs)
        self.assertEqual([(x.start, x.end) for x in g], [(0, 2), (2, 3)])
        self.assertEqual(g[0].duration, 90.0)
        self.assertEqual(g[0].head_id, "1")
        self.assertEqual([x.index for x in g], [0, 1])

    def test_same_album_in_two_places_gives_two_groups(self):
        songs = [s(1, artist="A", album="X"), s(2, artist="B", album="Y"), s(3, artist="A", album="X")]
        self.assertEqual(len(group_runs(songs)), 3)

    def test_album_artist_beats_track_artist(self):
        songs = [s(1, albumartist="VA", artist="A", album="Hits"), s(2, albumartist="VA", artist="B", album="Hits")]
        self.assertEqual(len(group_runs(songs)), 1)

    def test_same_album_name_by_other_artist_is_another_group(self):
        songs = [s(1, artist="A", album="Greatest Hits"), s(2, artist="B", album="Greatest Hits")]
        self.assertEqual(len(group_runs(songs)), 2)

    def test_musicbrainz_id_separates_same_named_albums(self):
        songs = [s(1, artist="A", album="X", musicbrainz_albumid="m1"),
                 s(2, artist="A", album="X", musicbrainz_albumid="m2")]
        self.assertEqual(len(group_runs(songs)), 2)

    def test_streams_group_together(self):
        songs = [s(1, file="http://a"), s(2, file="https://b")]
        g = group_runs(songs)
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0].title, "Streams")

    def test_empty_queue(self):
        self.assertEqual(group_runs([]), [])


class TestTexts(unittest.TestCase):
    def test_header(self):
        self.assertEqual(header_title(s(1, albumartist="A", album="X", date="1999-05-01")), "A – X (1999)")
        self.assertEqual(header_title(s(1, artist="A", album="X")), "A – X")
        self.assertEqual(header_title(s(1, artist="A")), "A")
        self.assertEqual(header_title(s(1)), "Unknown")

    def test_summary(self):
        g = group_runs([s(1, album="X", duration="3000"), s(2, album="X", duration="61")])[0]
        self.assertEqual(g.summary(), "2 Tracks (51:01)")
        one = group_runs([s(1, album="X")])[0]
        self.assertEqual(one.summary(), "1 Track")

    def test_track_title(self):
        self.assertEqual(track_title(s(1, title="Song", track="3/12", artist="A")), "03  Song")
        # the artist is never repeated in the title (the second line shows it)
        self.assertEqual(track_title(s(1, title="Song", track="3", artist="Guest")), "03  Song")
        self.assertEqual(track_title(s(1, title="Song")), "Song")
        self.assertEqual(track_title(s(1, file="dir/x.flac")), "x.flac")


class TestCollapse(unittest.TestCase):
    def setUp(self):
        self.songs = [s(1, album="X"), s(2, album="X"), s(3, album="X"), s(4, album="Y")]
        self.groups = group_runs(self.songs)

    def test_layout_open(self):
        self.assertEqual(layout(self.groups, set()), [(False, False)] * 4)

    def test_layout_collapsed_keeps_first_as_summary(self):
        self.assertEqual(layout(self.groups, {"1"}),
                         [(False, True), (True, False), (True, False), (False, False)])

    def test_prune(self):
        self.assertEqual(prune_collapsed({"1", "99"}, self.groups), {"1"})

    def test_drop_on_collapsed_album(self):
        g = self.groups[0]
        self.assertEqual(drop_index(g, 0, 0, {"1"}), 0)   # upper half: before the album
        self.assertEqual(drop_index(g, 0, 1, {"1"}), 3)   # lower half: after the album
        self.assertEqual(drop_index(g, 0, 1, set()), 1)   # open album: between its tracks
        self.assertEqual(drop_index(self.groups[1], 3, 4, {"1"}), 4)


if __name__ == "__main__":
    unittest.main()


class TestShownRow(unittest.TestCase):
    class Row:
        def __init__(self, group=None, summary=False):
            self.group, self.summary = group, summary

    def test_own_row(self):
        R = self.Row
        g = object()
        header, a, b = R(g), R(g), R(g)
        self.assertEqual(shown_row([header, a, b], b), 2)

    def test_collapsed_album_gives_its_summary_row(self):
        R = self.Row
        g = object()
        other, header, summary, hidden = R(), R(g), R(g, summary=True), R(g)
        self.assertEqual(shown_row([other, header, summary], hidden), 2)

    def test_only_the_header_left_gives_the_header(self):
        R = self.Row
        g = object()
        header, other_track, hidden = R(g), R(g), R(g)
        self.assertEqual(shown_row([R(), header, other_track], hidden), 1)

    def test_not_shown(self):
        R = self.Row
        self.assertIsNone(shown_row([R(), R()], R()))
        self.assertIsNone(shown_row([R(object())], R(object())))
