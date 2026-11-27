import unittest

from toccata import tagops as t


def song(**kw):
    base = dict(title="", artist="", album="", albumartist="", composer="",
                comment="", genre="", track=0, disc=0, year=0)
    base.update(kw)
    return base


class TestCapitalize(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(t.capitalize("hello big world"), "Hello Big World")

    def test_leaves_rest_of_word_alone(self):
        self.assertEqual(t.capitalize("mcDonald's farm"), "McDonald's Farm")

    def test_skips_leading_brackets(self):
        self.assertEqual(t.capitalize("song (live) [remix] {x}"), "Song (Live) [Remix] {X}")

    def test_bracket_only_word_unchanged(self):
        self.assertEqual(t.capitalize("a ( b"), "A ( B")

    def test_collapses_spaces_like_original(self):
        self.assertEqual(t.capitalize("a   b"), "A B")

    def test_empty(self):
        self.assertEqual(t.capitalize(""), "")

    def test_sharp_s_not_expanded(self):
        self.assertEqual(t.capitalize("ßtraße"), "ßtraße")

    def test_unicode(self):
        self.assertEqual(t.capitalize("élan vital"), "Élan Vital")

    def test_song_changed_flag(self):
        s = song(title="foo", artist="Bar")
        out, ch = t.capitalise_song(s)
        self.assertTrue(ch)
        self.assertEqual((out["title"], out["artist"]), ("Foo", "Bar"))
        self.assertEqual(s["title"], "foo")  # input untouched
        _, ch2 = t.capitalise_song(out)
        self.assertFalse(ch2)

    def test_comment_and_genre_not_capitalised(self):
        out, ch = t.capitalise_song(song(comment="keep me", genre="rock"))
        self.assertFalse(ch)
        self.assertEqual((out["comment"], out["genre"]), ("keep me", "rock"))


class TestAlbumArtist(unittest.TestCase):
    def test_sets_when_empty(self):
        out, ch = t.set_album_artist(song(artist="A"))
        self.assertTrue(ch)
        self.assertEqual(out["albumartist"], "A")

    def test_keeps_existing(self):
        _, ch = t.set_album_artist(song(artist="A", albumartist="B"))
        self.assertFalse(ch)

    def test_needs_artist(self):
        _, ch = t.set_album_artist(song())
        self.assertFalse(ch)


class TestVariousArtists(unittest.TestCase):
    def test_apply(self):
        s = song(title="Wobble", artist="Wibble", albumartist="Various Artists")
        out, ch = t.fix_various_artists(s)
        self.assertTrue(ch)
        self.assertEqual((out["title"], out["artist"]), ("Wibble - Wobble", "Various Artists"))

    def test_apply_only_for_various_artists(self):
        _, ch = t.fix_various_artists(song(title="x", artist="a", albumartist="Someone"))
        self.assertFalse(ch)

    def test_apply_custom_localised_name(self):
        s = song(title="T", artist="A", albumartist="Verschiedene Interpreten")
        _, ch = t.fix_various_artists(s, extra_names=["Verschiedene Interpreten"])
        self.assertTrue(ch)

    def test_artist_with_separator_is_sanitised(self):
        s = song(title="T", artist="A - B", albumartist="Various Artists")
        out, _ = t.fix_various_artists(s)
        self.assertEqual(out["title"], "A, B - T")

    def test_revert_matches_docstring_example(self):
        s = song(title="Wibble - Wobble", artist="X", albumartist="X")
        out, ch = t.revert_various_artists(s)
        self.assertTrue(ch)
        self.assertEqual((out["artist"], out["title"]), ("Wibble", "Wobble"))

    def test_apply_then_revert_round_trips(self):
        s = song(title="Wobble", artist="Wibble", albumartist="Various Artists")
        fixed, _ = t.fix_various_artists(s)
        back, ch = t.revert_various_artists(fixed)
        self.assertTrue(ch)
        self.assertEqual((back["artist"], back["title"]), ("Wibble", "Wobble"))

    def test_round_trip_with_dash_in_title(self):
        s = song(title="Foo - Bar", artist="Wibble", albumartist="Various Artists")
        back, _ = t.revert_various_artists(t.fix_various_artists(s)[0])
        self.assertEqual((back["artist"], back["title"]), ("Wibble", "Foo - Bar"))

    def test_revert_requires_artist_equals_albumartist(self):
        _, ch = t.revert_various_artists(song(title="A - B", artist="A", albumartist="Z"))
        self.assertFalse(ch)

    def test_revert_guards_degenerate_titles(self):
        for title in (" - x", "x - ", "x -", "no separator", ""):
            _, ch = t.revert_various_artists(song(title=title, artist="V", albumartist="V"))
            self.assertFalse(ch, title)


class TestTrackNumbers(unittest.TestCase):
    def test_adjust(self):
        out, ch = t.adjust_track_number(song(track=3), 2)
        self.assertEqual((out["track"], ch), (5, True))

    def test_zero_is_noop(self):
        _, ch = t.adjust_track_number(song(track=3), 0)
        self.assertFalse(ch)

    def test_clamps_at_zero(self):
        out, _ = t.adjust_track_number(song(track=2), -10)
        self.assertEqual(out["track"], 0)

    def test_no_change_when_already_zero_and_clamped(self):
        _, ch = t.adjust_track_number(song(track=0), -5)
        self.assertFalse(ch)


class TestApplyToAll(unittest.TestCase):
    def test_reports_changed_indexes_only(self):
        songs = [song(artist="a"), song(artist="b", albumartist="b"), song(artist="c")]
        new, changed = t.apply_to_all(songs, t.set_album_artist)
        self.assertEqual(changed, [0, 2])
        self.assertEqual([s["albumartist"] for s in new], ["a", "b", "c"])
        self.assertEqual(songs[0]["albumartist"], "")


if __name__ == "__main__":
    unittest.main()
