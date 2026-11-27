import unittest

from toccata.library import Library, UNKNOWN, collate_key, int_prefix, parse_year


def song(file, **kw):
    return dict(file=file, **kw)


def build(*songs):
    return Library.from_songs(songs)


def names(lib):
    return [a.name for a in lib.artists]


class TestAlbumArtistGrouping(unittest.TestCase):
    """The point of this feature: group by *album artist*, not by artist."""

    def test_album_artist_beats_artist(self):
        lib = build(song("1", artist="Guest", albumartist="Main", album="X"))
        self.assertEqual(names(lib), ["Main"])

    def test_compilation_is_one_artist_with_one_album(self):
        lib = build(
            song("1", artist="A", albumartist="Various Artists", album="Hits", track="1"),
            song("2", artist="B", albumartist="Various Artists", album="Hits", track="2"),
            song("3", artist="C", albumartist="Various Artists", album="Hits", track="3"))
        self.assertEqual(names(lib), ["Various Artists"])
        self.assertEqual(len(lib.artists[0].albums), 1)
        self.assertEqual(lib.artists[0].albums[0].n_tracks, 3)

    def test_track_artists_do_not_appear_as_library_artists(self):
        lib = build(song("1", artist="A", albumartist="VA", album="H"),
                    song("2", artist="B", albumartist="VA", album="H"))
        self.assertNotIn("A", names(lib))
        self.assertNotIn("B", names(lib))

    def test_falls_back_to_artist_when_album_artist_missing(self):
        lib = build(song("1", artist="Solo", album="Y"))
        self.assertEqual(names(lib), ["Solo"])

    def test_falls_back_when_album_artist_is_empty_string(self):
        self.assertEqual(names(build(song("1", artist="Solo", albumartist="", album="Y"))), ["Solo"])

    def test_no_artist_at_all_is_unknown_not_dropped(self):
        lib = build(song("1", album="Z"), song("2", title="t"))
        self.assertEqual(names(lib), [UNKNOWN])
        self.assertEqual(lib.n_songs, 2)

    def test_untagged_songs_are_kept(self):
        lib = build(song("x/untagged.flac"))
        self.assertEqual(lib.artists[0].albums[0].title, UNKNOWN)
        self.assertEqual(lib.artists[0].albums[0].tracks[0]["file"], "x/untagged.flac")

    def test_same_album_name_under_different_artists_stays_separate(self):
        lib = build(song("1", albumartist="A", album="Greatest Hits"),
                    song("2", albumartist="B", album="Greatest Hits"))
        self.assertEqual([len(a.albums) for a in lib.artists], [1, 1])

    def test_lookup_by_name(self):
        lib = build(song("1", albumartist="A", album="x"))
        self.assertEqual(lib.artist("A").name, "A")
        self.assertIsNone(lib.artist("nobody"))


class TestArtistSorting(unittest.TestCase):
    def order(self, *artist_names, **extra):
        return names(build(*[song(str(i), albumartist=n, album="a") for i, n in enumerate(artist_names)]))

    def test_case_insensitive(self):
        self.assertEqual(self.order("beta", "Alpha", "gamma"), ["Alpha", "beta", "gamma"])

    def test_numbers_sort_numerically(self):
        self.assertEqual(self.order("Band 10", "Band 2", "Band 1"), ["Band 1", "Band 2", "Band 10"])

    def test_punctuation_ignored(self):
        # "A-ha" sorts as "aha": after "Acdc" ('c' < 'h'), not first because of the hyphen
        self.assertEqual(self.order("A-ha", "Abba", "Acdc"), ["Abba", "Acdc", "A-ha"])

    def test_accented_letters_sort_with_their_base_letter(self):
        # a plain code-point sort would put "Émile" after "Zed"
        self.assertEqual(self.order("Zed", "Émile", "Eve", "Fred"), ["Émile", "Eve", "Fred", "Zed"])

    def test_sort_tag_overrides_name(self):
        lib = build(song("1", albumartist="The Beatles", albumartistsort="Beatles, The", album="x"),
                    song("2", albumartist="Abba", album="x"),
                    song("3", albumartist="Cure", album="x"))
        self.assertEqual(names(lib), ["Abba", "The Beatles", "Cure"])

    def test_sorting_is_by_album_artist_not_track_artist(self):
        lib = build(song("1", artist="Zzz", albumartist="Aaa", album="x"),
                    song("2", artist="Aaa", albumartist="Mmm", album="x"))
        self.assertEqual(names(lib), ["Aaa", "Mmm"])

    def test_stable_for_identical_sort_keys(self):
        self.assertEqual(self.order("ab", "a-b"), self.order("ab", "a-b"))
        self.assertEqual(sorted(self.order("ab", "a-b")), sorted(["ab", "a-b"]))


class TestAlbums(unittest.TestCase):
    def albums(self, *songs):
        return build(*songs).artists[0].albums

    def test_sorted_by_year_then_name(self):
        a = self.albums(song("1", albumartist="A", album="Zulu", date="1990"),
                        song("2", albumartist="A", album="Alpha", date="2005"),
                        song("3", albumartist="A", album="Bravo", date="1990"))
        self.assertEqual([x.title for x in a], ["Bravo", "Zulu", "Alpha"])

    def test_albums_without_year_come_last(self):
        a = self.albums(song("1", albumartist="A", album="NoYear"),
                        song("2", albumartist="A", album="Dated", date="2001"))
        self.assertEqual([x.title for x in a], ["Dated", "NoYear"])

    def test_album_sort_tag_used(self):
        a = self.albums(song("1", albumartist="A", album="The Zebra", albumsort="Zebra, The", date="2000"),
                        song("2", albumartist="A", album="Apple", date="2000"))
        self.assertEqual([x.title for x in a], ["Apple", "The Zebra"])

    def test_musicbrainz_id_separates_albums_with_same_name(self):
        a = self.albums(song("1", albumartist="A", album="Live", musicbrainz_albumid="id1"),
                        song("2", albumartist="A", album="Live", musicbrainz_albumid="id2"))
        self.assertEqual(len(a), 2)

    def test_missing_musicbrainz_id_groups_by_name(self):
        a = self.albums(song("1", albumartist="A", album="Live"), song("2", albumartist="A", album="Live"))
        self.assertEqual(len(a), 1)

    def test_year_comes_from_first_track_with_a_year(self):
        a = self.albums(song("1", albumartist="A", album="x", track="2", date="1999"),
                        song("2", albumartist="A", album="x", track="1", date="2005"))
        self.assertEqual(a[0].year, 2005)  # track 1 wins

    def test_year_skips_tracks_without_one(self):
        a = self.albums(song("1", albumartist="A", album="x", track="1"),
                        song("2", albumartist="A", album="x", track="2", date="1999-05-03"))
        self.assertEqual(a[0].year, 1999)

    def test_title_uses_first_non_empty_album_name(self):
        a = self.albums(song("1", albumartist="A", musicbrainz_albumid="m"),
                        song("2", albumartist="A", album="Real Name", musicbrainz_albumid="m"))
        self.assertEqual(a[0].title, "Real Name")

    def test_totals_and_cover(self):
        a = self.albums(song("d/2.flac", albumartist="A", album="x", track="2", duration="100.5"),
                        song("d/1.flac", albumartist="A", album="x", track="1", duration="200"))[0]
        self.assertEqual(a.n_tracks, 2)
        self.assertAlmostEqual(a.duration, 300.5)
        self.assertEqual(a.cover_uri, "d/1.flac")  # first track in track order

    def test_duration_falls_back_to_time_and_survives_garbage(self):
        a = self.albums(song("1", albumartist="A", album="x", time="90"),
                        song("2", albumartist="A", album="x", duration="oops"))[0]
        self.assertEqual(a.duration, 90.0)


class TestTrackOrder(unittest.TestCase):
    def files(self, *songs):
        return [t["file"] for t in build(*songs).artists[0].albums[0].tracks]

    def test_by_track_number(self):
        self.assertEqual(self.files(song("c", albumartist="A", album="x", track="3"),
                                    song("a", albumartist="A", album="x", track="1"),
                                    song("b", albumartist="A", album="x", track="2")), ["a", "b", "c"])

    def test_track_with_total_is_parsed(self):
        self.assertEqual(self.files(song("b", albumartist="A", album="x", track="10/12"),
                                    song("a", albumartist="A", album="x", track="2/12")), ["a", "b"])

    def test_numeric_not_lexicographic(self):
        self.assertEqual(self.files(song("t10", albumartist="A", album="x", track="10"),
                                    song("t9", albumartist="A", album="x", track="9")), ["t9", "t10"])

    def test_discs_come_before_tracks(self):
        self.assertEqual(self.files(song("d2t1", albumartist="A", album="x", disc="2", track="1"),
                                    song("d1t9", albumartist="A", album="x", disc="1", track="9")), ["d1t9", "d2t1"])

    def test_disc_with_total(self):
        self.assertEqual(self.files(song("b", albumartist="A", album="x", disc="2/2", track="1"),
                                    song("a", albumartist="A", album="x", disc="1/2", track="1")), ["a", "b"])

    def test_untracked_songs_fall_back_to_title(self):
        self.assertEqual(self.files(song("1", albumartist="A", album="x", title="Zulu"),
                                    song("2", albumartist="A", album="x", title="alpha")), ["2", "1"])

    def test_artist_tracks_follow_album_order(self):
        lib = build(song("late1", albumartist="A", album="Late", date="2010", track="1"),
                    song("early1", albumartist="A", album="Early", date="1990", track="1"),
                    song("early2", albumartist="A", album="Early", date="1990", track="2"))
        self.assertEqual([t["file"] for t in lib.artist("A").tracks()], ["early1", "early2", "late1"])


class TestHelpers(unittest.TestCase):
    def test_parse_year(self):
        for text, year in [("2019", 2019), ("2019-05-03", 2019), ("(1999)", 1999), ("1999/2005", 1999),
                           ("", 0), (None, 0), ("abcd", 0), ("12345", 0), ("0000", 0)]:
            self.assertEqual(parse_year(text), year, text)

    def test_int_prefix(self):
        for text, n in [("3", 3), ("3/12", 3), (" 7", 7), ("", 0), (None, 0), ("x", 0), ("A1", 0)]:
            self.assertEqual(int_prefix(text), n, text)

    def test_collate_key_orders_like_a_human(self):
        self.assertEqual(sorted(["track 10", "Track 2", "track 1"], key=collate_key),
                         ["track 1", "Track 2", "track 10"])
        self.assertLess(collate_key("apple"), collate_key("Éclair"))
        self.assertLess(collate_key("Éclair"), collate_key("Zebra"))
        self.assertLess(collate_key("track 2"), collate_key("track 10"))

    def test_collate_key_handles_empty_and_symbol_only(self):
        self.assertLess(collate_key(""), collate_key("a"))
        self.assertLess(collate_key("!!!"), collate_key("a"))
        collate_key(None)

    def test_collate_key_survives_odd_unicode(self):
        sorted(["²", "①", "日本", "Ω", "ß", "ǅ"], key=collate_key)  # must not raise

    def test_empty_library(self):
        lib = build()
        self.assertEqual((lib.artists, lib.n_songs), ([], 0))


class TestGroupAs(unittest.TestCase):
    def test_group_as_files_everything_under_the_given_artist(self):
        lib = Library.from_songs([song("1", artist="Guest", albumartist="Other", album="X"),
                                  song("2", album="Y")], group_as="Chosen")
        self.assertEqual(names(lib), ["Chosen"])
        self.assertEqual(sorted(a.title for a in lib.artist("Chosen").albums), ["X", "Y"])

    def test_group_as_with_no_songs_gives_an_empty_library(self):
        self.assertEqual(Library.from_songs([], group_as="Chosen").artists, [])


if __name__ == "__main__":
    unittest.main()


class TestIgnorePrefixes(unittest.TestCase):
    def test_strip_prefix(self):
        from toccata.library import strip_prefix
        self.assertEqual(strip_prefix("The Beatles", ("The",)), "Beatles, The")
        self.assertEqual(strip_prefix("the the", ("The",)), "the, the")
        self.assertEqual(strip_prefix("Theatre of Tragedy", ("The",)), "Theatre of Tragedy")  # whole word only
        self.assertEqual(strip_prefix("The", ("The",)), "The")  # nothing after it
        self.assertEqual(strip_prefix("Die Ärzte", ("The", "Die")), "Ärzte, Die")
        self.assertEqual(strip_prefix("The Beatles", ()), "The Beatles")

    def test_sort_names(self):
        from toccata.library import sort_names
        names = ["The Beatles", "Blur", "Abba", "The The", "Theatre of Tragedy", "Air"]
        self.assertEqual(sort_names(names, ("The",)),
                         ["Abba", "Air", "The Beatles", "Blur", "Theatre of Tragedy", "The The"])  # "thethe" > "theatre"
        self.assertEqual(sort_names(names)[:3], ["Abba", "Air", "Blur"])  # without prefixes: "The ..." under T

    def test_parse_prefixes(self):
        from toccata.library import parse_prefixes
        self.assertEqual(parse_prefixes("The, Die,  Les ,the,, "), ("The", "Die", "Les"))
        self.assertEqual(parse_prefixes(""), ())
