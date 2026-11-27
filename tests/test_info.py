import json
import os
import shutil
import subprocess
import tempfile
import unittest
import urllib.parse

from toccata import info
from toccata.tags import lyrics_from_items, read_lyrics


def j(obj):
    return json.dumps(obj).encode()


class FakeGet:
    """get(url) answering from {(host+path, frozenset of some params): bytes}."""

    def __init__(self, answers):
        self.answers, self.urls = answers, []

    def __call__(self, url):
        self.urls.append(url)
        for match, data in self.answers:
            if match(url):
                return data
        return None


class NoWait:
    def wait(self):
        pass


def q(url):
    p = urllib.parse.urlparse(url)
    return p.netloc + p.path, dict(urllib.parse.parse_qsl(p.query))


ARTIST_SEARCH = j({"artists": [
    {"id": "low-1", "name": "Low", "score": 100},
    {"id": "low-2", "name": "LOW", "score": 95},
    {"id": "lower", "name": "Lower", "score": 80},
    {"id": "alias", "name": "Something", "score": 70, "aliases": [{"name": "Low"}]}]})
ARTIST_LINKS = j({"relations": [
    {"type": "wikidata", "url": {"resource": "https://www.wikidata.org/wiki/Q1137498"}},
    {"type": "wikipedia", "url": {"resource": "https://fr.wikipedia.org/wiki/Low_(groupe)"}},
    {"type": "discogs", "url": {"resource": "https://www.discogs.com/artist/1"}}]})
SITELINKS = j({"entities": {"Q1137498": {"sitelinks": {
    "enwiki": {"title": "Low (band)"}, "dewiki": {"title": "Low (Band)"}, "commonswiki": {"title": "x"},
    "zh_yuewiki": {"title": "y"}}}}})
EXTRACT = j({"query": {"pages": [{"title": "Low (band)", "extract":
    "Low was an American band.\nThey formed in 1993.\n\n== History ==\nFirst years.\n=== Early ===\nDetails.\n"
    "== References ==\nRef 1.\n=== Sub of refs ===\nMore refs.\n== Legacy ==\nInfluential.\n== Weblinks ==\nlink"}]}})
MISSING = j({"query": {"pages": [{"title": "Nope", "missing": True}]}})


class TestParsers(unittest.TestCase):
    def test_norm(self):
        self.assertEqual(info.norm("The Beatles"), info.norm("beatles"))
        self.assertEqual(info.norm("Sigur Rós"), info.norm("sigur ros"))
        self.assertEqual(info.norm("Simon & Garfunkel"), info.norm("Simon and Garfunkel"))
        self.assertNotEqual(info.norm("Low"), info.norm("Lower"))

    def test_artist_search_only_exact_names_best_first(self):
        self.assertEqual(info.parse_mb_artist_search(ARTIST_SEARCH, "low"), ["low-1", "low-2", "alias"])
        self.assertEqual(info.parse_mb_artist_search(ARTIST_SEARCH, "Nobody"), [])

    def test_search_urls_quote_names(self):
        host, params = q(info.mb_artist_search_url('AC/DC "live"'))
        self.assertEqual(host, "musicbrainz.org/ws/2/artist")
        self.assertEqual(params["query"], 'artist:"AC/DC \\"live\\""')
        _, params = q(info.mb_rg_search_url("Abbey Road", artist_id="abc"))
        self.assertEqual(params["query"], 'releasegroup:"Abbey Road" AND arid:abc')

    def test_links_and_sitelinks(self):
        qid, wiki = info.parse_links(ARTIST_LINKS)
        self.assertEqual(qid, "Q1137498")
        self.assertEqual(wiki, {"fr": "Low (groupe)"})
        self.assertEqual(info.parse_sitelinks(SITELINKS, "Q1137498"),
                         {"en": "Low (band)", "de": "Low (Band)", "zh-yue": "y"})

    def test_pick_article_by_language(self):
        titles = {"en": "A", "de": "B"}
        self.assertEqual(info.pick_article(titles, ["de", "en"]), ("de", "B"))
        self.assertEqual(info.pick_article(titles, ["fr", "en"]), ("en", "A"))
        self.assertIsNone(info.pick_article({}, ["en"]))

    def test_extract_is_the_lead_only(self):
        self.assertEqual(info.parse_extract(EXTRACT), ["Low was an American band.", "They formed in 1993."])
        self.assertEqual(info.parse_extract(MISSING), [])
        _, params = q(info.wikipedia_url("en", "Low (band)"))
        self.assertEqual((params["exintro"], params["explaintext"], params["titles"]), ("1", "1", "Low (band)"))

    def test_lastfm_similar(self):
        data = j({"similarartists": {"artist": [{"name": "Codeine", "match": "1"}, {"name": "Red House Painters"}]}})
        self.assertEqual(info.parse_lastfm_similar(data), ["Codeine", "Red House Painters"])
        with self.assertRaises(ValueError):
            info.parse_lastfm_similar(j({"error": 10, "message": "Invalid API key"}))
        _, params = q(info.lastfm_similar_url("Low", "KEY"))
        self.assertEqual((params["method"], params["artist"], params["api_key"], params["limit"]),
                         ("artist.getsimilar", "Low", "KEY", "100"))


LRC = "[ar:Low]\n[00:01.50]First line\n[00:05.00][01:00.00]Chorus\n[00:09.25] Third \nno time\n"


class TestLyrics(unittest.TestCase):
    def test_parse_lrc(self):
        got = info.parse_lrc(LRC)
        self.assertEqual(got, [(1.5, "First line"), (5.0, "Chorus"), (9.25, "Third"), (60.0, "Chorus")])
        self.assertEqual(info.parse_lrc("just text\nno times"), [])

    def test_current_line(self):
        lines = info.parse_lrc(LRC)
        self.assertEqual([info.current_line(lines, t) for t in (0, 1.5, 4.9, 5, 30, 61)], [-1, 0, 0, 1, 2, 3])
        self.assertEqual(info.current_line([], 10), -1)

    def test_lyrics_result_plain_and_synced(self):
        r = info.lyrics_result(LRC, "tag")
        self.assertEqual(r["plain"], "First line\nChorus\nThird\nChorus")
        self.assertEqual(len(r["synced"]), 4)
        r = info.lyrics_result("Just words", "file")
        self.assertEqual((r["plain"], r["synced"]), ("Just words", []))

    def test_lrclib_get_and_search(self):
        self.assertEqual(info.parse_lrclib(j({"plainLyrics": "a\nb", "syncedLyrics": "[00:01.00]a"})), ("a\nb", "[00:01.00]a"))
        self.assertEqual(info.parse_lrclib(j({"instrumental": True})), ("[Instrumental]", ""))
        self.assertEqual(info.parse_lrclib(None), ("", ""))
        found = j([
            {"trackName": "Words", "artistName": "Low", "duration": 300, "plainLyrics": "far off"},
            {"trackName": "Words", "artistName": "Other", "duration": 200, "syncedLyrics": "[00:01]x"},
            {"trackName": "Words", "artistName": "Low", "duration": 201, "plainLyrics": "plain"},
            {"trackName": "Words", "artistName": "LOW", "duration": 199, "plainLyrics": "p", "syncedLyrics": "[00:01.00]s"}])
        self.assertEqual(info.pick_lrclib(found, "Low", "Words", 200), ("p", "[00:01.00]s"))
        self.assertEqual(info.pick_lrclib(found, "Low", "Nope", 200), ("", ""))
        only_far = j([{"trackName": "Words", "artistName": "Low", "duration": 300, "plainLyrics": "other version"}])
        self.assertEqual(info.pick_lrclib(only_far, "Low", "Words", 200), ("", ""))  # 100 s longer: not this one
        self.assertEqual(info.pick_lrclib(only_far, "Low", "Words", None), ("other version", ""))
        _, params = q(info.lrclib_get_url("Low", "Words", "Album", "200.4"))
        self.assertEqual(params, {"artist_name": "Low", "track_name": "Words", "album_name": "Album", "duration": "200"})

    def test_sidecar(self):
        d = tempfile.mkdtemp()
        try:
            song = os.path.join(d, "01 Song.flac")
            self.assertEqual(info.sidecar_lyrics(song), ("", ""))
            with open(os.path.join(d, "01 Song.lrc"), "w") as f:
                f.write("[00:01.00]hi\n")
            self.assertEqual(info.sidecar_lyrics(song), ("[00:01.00]hi", "01 Song.lrc"))
        finally:
            shutil.rmtree(d)

    def test_lyrics_from_items(self):
        class Frame:
            def __init__(self, text):
                self.text = text
        self.assertEqual(lyrics_from_items([("title", ["x"]), ("LYRICS", ["la la"])]), "la la")
        self.assertEqual(lyrics_from_items([("USLT::eng", Frame("words"))]), "words")
        self.assertEqual(lyrics_from_items([("©lyr", ["mp4 words"])]), "mp4 words")
        self.assertEqual(lyrics_from_items([("SYLT::eng", Frame([("one", 1500), ("two", 61000)]))]),
                         "[00:01.50]one\n[01:01.00]two")
        self.assertEqual(lyrics_from_items([("title", ["x"])]), "")


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class TestLyricsInRealFiles(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def make(self, name):
        path = os.path.join(self.dir, name)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                        "-t", "0.2", path], check=True)
        return path

    def test_flac_vorbis_lyrics(self):
        from mutagen.flac import FLAC
        path = self.make("a.flac")
        f = FLAC(path)
        f["LYRICS"] = "flac words"
        f.save()
        self.assertEqual(read_lyrics(path), "flac words")

    def test_mp3_uslt(self):
        from mutagen.id3 import ID3, USLT
        path = self.make("a.mp3")
        tags = ID3()
        tags.add(USLT(encoding=3, lang="eng", desc="", text="mp3 words"))
        tags.save(path)
        self.assertEqual(read_lyrics(path), "mp3 words")

    def test_no_lyrics_or_no_file(self):
        self.assertEqual(read_lyrics(self.make("b.flac")), "")
        self.assertEqual(read_lyrics(os.path.join(self.dir, "missing.flac")), "")


class TestLookup(unittest.TestCase):
    def answers(self):
        return [
            (lambda u: "/ws/2/artist?" in u, ARTIST_SEARCH),
            (lambda u: "/ws/2/release-group?" in u and "arid%3Alow-2" in u,
             j({"release-groups": [{"id": "rg-things", "title": "Things We Lost in the Fire", "score": 100}]})),
            (lambda u: "/ws/2/release-group?" in u, j({"release-groups": []})),
            (lambda u: "/ws/2/artist/low-2?" in u, ARTIST_LINKS),
            (lambda u: "/ws/2/release-group/rg-things?" in u, j({"relations": [
                {"type": "wikidata", "url": {"resource": "https://www.wikidata.org/wiki/Q7"}}]})),
            (lambda u: "wikidata.org" in u and "Q1137498" in u, SITELINKS),
            (lambda u: "wikidata.org" in u and "Q7" in u, j({"entities": {"Q7": {"sitelinks": {"enwiki": {"title": "Things We Lost in the Fire"}}}}})),
            (lambda u: "en.wikipedia.org" in u and "Low" in u, EXTRACT),
            (lambda u: "en.wikipedia.org" in u, j({"query": {"pages": [{"title": "T", "extract": "An album.\n== Notes ==\nn"}]}})),
        ]

    def test_artist_with_same_name_picked_by_album(self):
        get = FakeGet(self.answers())
        look = info.Lookup(get=get, mb_limit=NoWait())  # English
        song = {"albumartist": "Low", "album": "Things We Lost in the Fire", "title": "x"}
        got = look.artist_wiki(song)
        self.assertEqual(got["mbid"], "low-2")
        self.assertEqual((got["wiki"]["lang"], got["wiki"]["title"]), ("en", "Low (band)"))
        self.assertEqual(got["wiki"]["url"], "https://en.wikipedia.org/wiki/Low_(band)")
        self.assertEqual(got["wiki"]["paragraphs"], ["Low was an American band.", "They formed in 1993."])
        self.assertFalse(any("de.wikipedia" in u for u in get.urls))

    def test_album_wiki(self):
        get = FakeGet(self.answers())
        look = info.Lookup(get=get, mb_limit=NoWait())
        song = {"albumartist": "Low", "album": "Things We Lost in the Fire"}
        got = look.album_wiki(song, artist_id="low-2")
        self.assertEqual(got["mbid"], "rg-things")
        self.assertEqual((got["wiki"]["lang"], got["wiki"]["paragraphs"]), ("en", ["An album."]))

    def test_no_english_article_means_none(self):
        get = FakeGet([(lambda u: "/ws/2/artist/x?" in u, j({"relations": [
                           {"type": "wikipedia", "url": {"resource": "https://de.wikipedia.org/wiki/Nur_Deutsch"}}]}))])
        got = info.Lookup(get=get, mb_limit=NoWait()).artist_wiki({"albumartist": "X", "musicbrainz_albumartistid": "x"})
        self.assertIsNone(got["wiki"])
        self.assertFalse(any("wikipedia.org/w/api.php" in u for u in get.urls))

    def test_mbids_from_tags_skip_the_search(self):
        get = FakeGet(self.answers())
        look = info.Lookup(get=get, mb_limit=NoWait())
        got = look.artist_wiki({"albumartist": "Low", "musicbrainz_albumartistid": "low-2"})
        self.assertEqual(got["mbid"], "low-2")
        self.assertFalse(any("/ws/2/artist?" in u for u in get.urls))

    def test_unknown_artist_gives_no_wiki(self):
        look = info.Lookup(get=FakeGet([(lambda u: True, j({"artists": []}))]), mb_limit=NoWait())
        self.assertEqual(look.artist_wiki({"albumartist": "Nobody"}), {"wiki": None, "mbid": None})

    def test_cache_used_and_none_cached(self):
        d = tempfile.mkdtemp()
        try:
            cache = info.Cache(d)
            get = FakeGet(self.answers())
            look = info.Lookup(get=get, mb_limit=NoWait(), cache=cache)
            song = {"albumartist": "Low", "musicbrainz_albumartistid": "low-2"}
            first = look.artist_wiki(song)
            n = len(get.urls)
            again = look.artist_wiki(song)
            self.assertEqual(len(get.urls), n)  # all from the cache
            self.assertEqual(json.loads(json.dumps(first)), again)
            look2 = info.Lookup(get=FakeGet([(lambda u: True, j({"artists": []}))]), mb_limit=NoWait(), cache=cache)
            self.assertEqual(look2.artist_wiki({"albumartist": "Nobody"})["wiki"], None)
            self.assertIsNotNone(cache.get(("artist-wiki", 3, "nobody", "", ("en",)), ttl=10))  # the miss is cached
        finally:
            shutil.rmtree(d)

    def test_cache_expiry(self):
        import time as _t
        d = tempfile.mkdtemp()
        try:
            cache = info.Cache(d)

            def store(key, value, age_days):
                cache.put(key, value)
                path = cache._path(key)
                with open(path) as f:
                    entry = json.load(f)
                entry["time"] = _t.time() - age_days * 86400
                with open(path, "w") as f:
                    json.dump(entry, f)
            store("found", {"a": 1}, 20)
            store("found-old", {"a": 1}, 40)
            store("missing", None, 3)
            store("missing-old", None, 8)
            self.assertEqual(cache.get("found", ttl=30 * 86400).value, {"a": 1})
            self.assertIsNone(cache.get("found-old", ttl=30 * 86400))
            self.assertIsNone(cache.get("missing", ttl=30 * 86400).value)  # a cached "nothing found"
            self.assertIsNone(cache.get("missing-old", ttl=30 * 86400))      # asked again after a week
        finally:
            shutil.rmtree(d)

    def test_similar_needs_a_key(self):
        look = info.Lookup(get=FakeGet([]), mb_limit=NoWait())
        self.assertIsNone(look.similar("Low"))
        get = FakeGet([(lambda u: "audioscrobbler" in u, j({"similarartists": {"artist": [{"name": "Codeine"}]}}))])
        self.assertEqual(info.Lookup(get=get, lastfm_key="K").similar("Low"), ["Codeine"])

    def test_online_lyrics_get_then_search(self):
        get = FakeGet([(lambda u: "/api/get?" in u, None),
                       (lambda u: "/api/search?" in u, j([{"trackName": "Words", "artistName": "Low", "duration": 200,
                                                           "syncedLyrics": "[00:02.00]hello"}]))])
        got = info.Lookup(get=get).online_lyrics({"artist": "Low", "title": "Words", "duration": "200.1"})
        self.assertEqual(got, {"plain": "hello", "synced": [(2.0, "hello")], "source": "LRCLIB"})
        none = info.Lookup(get=FakeGet([])).online_lyrics({"artist": "Low", "title": "Nothing"})
        self.assertIsNone(none)


if __name__ == "__main__":
    unittest.main()


def search_answer(*titles):
    return j({"query": {"search": [{"ns": 0, "title": t} for t in titles]}})


def page_answer(title, lead, disambiguation=False):
    page = {"title": title, "extract": lead + "\n== History ==\nmore"}
    if disambiguation:
        page["pageprops"] = {"disambiguation": ""}
    return j({"query": {"pages": [page]}})


class WikiGet(FakeGet):
    """MusicBrainz (no artist / album found unless given) and Wikipedia search + pages."""

    def __init__(self, search, pages, mb=()):
        super().__init__(list(mb) + [
            (lambda u: "musicbrainz.org" in u, j({"artists": [], "release-groups": []})),
            (lambda u: q(u)[1].get("list") == "search", search)])
        self.pages = pages

    def __call__(self, url):
        got = super().__call__(url)
        params = q(url)[1]
        if "pageprops" in params.get("prop", "") and params.get("titles") in self.pages:
            return self.pages[params["titles"]]
        return got

    def fetched_pages(self):
        return [q(u)[1]["titles"] for u in self.urls if "pageprops" in q(u)[1].get("prop", "")]


class TestWikipediaSearch(unittest.TestCase):
    def test_candidates_in_cantatas_order(self):
        titles = ["Low", "Lower (band)", "Low (band)", "Low (singer)"]
        self.assertEqual(info.search_candidates(titles, "low"), ["Low (band)", "Low (singer)", "Low"])
        titles = ["Fire", "Low Fire (album)", "Fire (album)", "Fire (Low album)", "Fire (Other album)"]
        self.assertEqual(info.search_candidates(titles, "Low", "Fire"),
                         ["Fire (Low album)", "Low Fire (album)", "Fire (album)", "Fire"])
        self.assertEqual(info.search_candidates(["AC/DC", "ASAP (band)"], "AC-DC"), ["AC/DC"])
        self.assertEqual(info.search_candidates(["ASAP (band)"], "A.S.A.P."), ["ASAP (band)"])
        self.assertEqual(info.search_candidates(["Lower"], "Low"), [])

    def test_search_url(self):
        host, params = q(info.wikipedia_search_url("en", "Low Embrace"))
        self.assertEqual((host, params["list"], params["srsearch"]), ("en.wikipedia.org/w/api.php", "search", "Low Embrace"))
        self.assertEqual(info.parse_search_titles(search_answer("A", "B")), ["A", "B"])
        self.assertEqual(info.parse_search_titles(j({"query": {"pages": []}})), [])

    def test_plausible(self):
        self.assertTrue(info.plausible(["Low was an American indie rock band."], "artist", "Low"))
        self.assertTrue(info.plausible(["Prince was an American singer, songwriter and musician."], "artist", "Prince"))
        self.assertFalse(info.plausible(["James is a common English given name and surname."], "artist", "James"))
        self.assertTrue(info.plausible(["Things We Lost in the Fire is the fifth album by Low."], "album", "Low"))
        self.assertTrue(info.plausible(["Abbey Road is the 11th studio album by the Beatles."], "album", "The Beatles"))
        self.assertFalse(info.plausible(["Embrace is the debut album by Thrice."], "album", "Low"))
        self.assertFalse(info.plausible(["Lowland is a region. Low lies there."], "album", "Low"))  # no release word
        self.assertFalse(info.plausible(["Lower is an album by Lower Dens."], "album", "Low"))  # whole words only

    def test_disambiguation_page(self):
        self.assertTrue(info.is_disambiguation(page_answer("Embrace", "Embrace may refer to:", disambiguation=True)))
        self.assertFalse(info.is_disambiguation(page_answer("Low (band)", "Low was a band.")))

    def test_unknown_to_musicbrainz_found_by_search(self):
        get = WikiGet(search_answer("James", "James (band)"), {
            "James (band)": page_answer("James (band)", "James are an English rock band from Manchester.")})
        got = info.Lookup(get=get, mb_limit=NoWait()).artist_wiki({"albumartist": "James"})
        self.assertEqual((got["wiki"]["title"], got["wiki"]["url"], got["mbid"]),
                         ("James (band)", "https://en.wikipedia.org/wiki/James_(band)", None))
        self.assertEqual(got["wiki"]["paragraphs"], ["James are an English rock band from Manchester."])
        self.assertEqual(get.fetched_pages(), ["James (band)"])  # the plain "James" not needed

    def test_wrong_pages_are_skipped(self):
        get = WikiGet(search_answer("James (singer)", "James"), {
            "James (singer)": page_answer("James (singer)", "James may refer to: James (band), an English rock band",
                                          disambiguation=True),  # (would pass plausible())
            "James": page_answer("James (band)", "James are an English rock band.")})  # (a redirect)
        got = info.Lookup(get=get, mb_limit=NoWait()).artist_wiki({"albumartist": "James"})
        self.assertEqual(got["wiki"]["title"], "James (band)")
        get = WikiGet(search_answer("James"), {"James": page_answer("James", "James is a given name.")})
        self.assertIsNone(info.Lookup(get=get, mb_limit=NoWait()).artist_wiki({"albumartist": "James"})["wiki"])

    def test_at_most_three_pages(self):
        titles = ["X (artist)", "X (band)", "X (singer)", "X (vocalist)", "X"]
        get = WikiGet(search_answer(*titles), {t: page_answer(t, "X is a town.") for t in titles})
        self.assertIsNone(info.Lookup(get=get, mb_limit=NoWait()).artist_wiki({"albumartist": "X"})["wiki"])
        self.assertEqual(get.fetched_pages(), titles[:info.SEARCH_TRIES])

    def test_album_of_another_artist_is_not_taken(self):
        get = WikiGet(search_answer("Embrace", "Embrace (album)"), {
            "Embrace (album)": page_answer("Embrace (album)", "Embrace is the debut album by Thrice."),
            "Embrace": page_answer("Embrace", "Embrace may refer to:", disambiguation=True)})
        got = info.Lookup(get=get, mb_limit=NoWait()).album_wiki({"albumartist": "Low", "album": "Embrace"})
        self.assertEqual(got, {"wiki": None, "mbid": None})
        get = WikiGet(search_answer("Embrace (Low EP)", "Embrace"), {
            "Embrace (Low EP)": page_answer("Embrace (Low EP)", "Embrace is an EP by the American band Low.")})
        got = info.Lookup(get=get, mb_limit=NoWait()).album_wiki({"albumartist": "Low", "album": "Embrace"})
        self.assertIsNone(got["wiki"])  # "(Low EP)" is no pattern: Cantata would not take it either
        get = WikiGet(search_answer("Embrace (Low album)"), {
            "Embrace (Low album)": page_answer("Embrace (Low album)", "Embrace is an album by the American band Low.")})
        got = info.Lookup(get=get, mb_limit=NoWait()).album_wiki({"albumartist": "Low", "album": "Embrace"})
        self.assertEqual(got["wiki"]["title"], "Embrace (Low album)")
        self.assertEqual(q(next(u for u in get.urls if "list=search" in u))[1]["srsearch"], "Low Embrace")

    def test_musicbrainz_without_any_link_then_search(self):
        mb = [(lambda u: "/ws/2/artist?" in u, j({"artists": [{"id": "j", "name": "James", "score": 100}]})),
              (lambda u: "/ws/2/artist/j?" in u, j({"relations": []}))]
        get = WikiGet(search_answer("James (band)"), {"James (band)": page_answer("James (band)", "James are a band.")}, mb)
        got = info.Lookup(get=get, mb_limit=NoWait()).artist_wiki({"albumartist": "James"})
        self.assertEqual((got["wiki"]["title"], got["mbid"]), ("James (band)", "j"))

    def test_musicbrainz_link_without_english_article_means_no_search(self):
        mb = [(lambda u: "/ws/2/release-group?" in u, j({"release-groups": [{"id": "rg", "title": "Nur", "score": 100}]})),
              (lambda u: "/ws/2/release-group/rg?" in u, j({"relations": [
                  {"type": "wikipedia", "url": {"resource": "https://de.wikipedia.org/wiki/Nur"}}]}))]
        get = WikiGet(search_answer("Nur (album)"), {"Nur (album)": page_answer("Nur (album)", "Nur is an album by X.")}, mb)
        got = info.Lookup(get=get, mb_limit=NoWait()).album_wiki({"albumartist": "X", "album": "Nur"})
        self.assertEqual(got, {"wiki": None, "mbid": "rg"})
        self.assertFalse(any("list=search" in u for u in get.urls))

    def busy_get(self, wiki):
        import email.message
        import urllib.error

        def get(url):
            if "musicbrainz" in url:
                raise urllib.error.HTTPError(url, 503, "busy", email.message.Message(), None)
            return wiki(url)
        return get

    def test_musicbrainz_busy_then_search_not_cached(self):
        wiki = WikiGet(search_answer("Low (band)"), {"Low (band)": page_answer("Low (band)", "Low was a band.")})
        d = tempfile.mkdtemp()
        try:
            cache = info.Cache(d)
            look = info.Lookup(get=self.busy_get(wiki), mb_limit=NoWait(), sleep=lambda s: None, cache=cache)
            got = look.artist_wiki({"albumartist": "Low"})
            self.assertEqual((got["wiki"]["title"], got["mbid"]), ("Low (band)", None))
            self.assertEqual(os.listdir(d), [])  # MusicBrainz is asked next time
        finally:
            shutil.rmtree(d)

    def test_musicbrainz_busy_and_nothing_found_is_busy(self):
        import urllib.error
        wiki = WikiGet(search_answer(), {})
        look = info.Lookup(get=self.busy_get(wiki), mb_limit=NoWait(), sleep=lambda s: None)
        with self.assertRaises(info.Busy):
            look.album_wiki({"albumartist": "Low", "album": "Embrace"})

        def failing(url):
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", None, None)
        look = info.Lookup(get=self.busy_get(failing), mb_limit=NoWait(), sleep=lambda s: None)
        with self.assertRaises(info.Busy):
            look.artist_wiki({"albumartist": "Low"})


class TestMusicBrainzBusy(unittest.TestCase):
    def busy(self, retry_after=None):
        import email.message
        import urllib.error
        h = email.message.Message()
        if retry_after is not None:
            h["Retry-After"] = str(retry_after)
        return urllib.error.HTTPError("https://musicbrainz.org/x", 503, "Service Temporarily Unavailable", h, None)

    def test_503_is_retried_with_pauses(self):
        calls, pauses = [], []

        def get(url):
            calls.append(url)
            if "musicbrainz" in url and len(calls) <= 2:
                raise self.busy()
            return j({"artists": [{"id": "dylan", "name": "Bob Dylan", "score": 100}]}) if "/artist?" in url else None
        look = info.Lookup(get=get, mb_limit=NoWait(), sleep=pauses.append)
        self.assertEqual(look.artist_id({"albumartist": "Bob Dylan"}), "dylan")
        self.assertEqual(pauses, [2.0, 4.0])

    def test_retry_after_header_and_giving_up(self):
        pauses = []

        def get(url):
            raise self.busy(retry_after=5)
        look = info.Lookup(get=get, mb_limit=NoWait(), sleep=pauses.append)
        with self.assertRaises(info.Busy):
            look.artist_id({"albumartist": "Bob Dylan"})
        self.assertEqual(pauses, [5.0, 5.0, 6.0])  # Retry-After, but at least 2, 4, 6 s
        self.assertEqual(len(pauses), info.MB_RETRIES)

    def test_other_http_errors_are_not_retried(self):
        import urllib.error
        pauses = []

        def get(url):
            raise urllib.error.HTTPError(url, 400, "Bad Request", None, None)
        with self.assertRaises(urllib.error.HTTPError):
            info.Lookup(get=get, mb_limit=NoWait(), sleep=pauses.append).artist_id({"albumartist": "X"})
        self.assertEqual(pauses, [])

    def test_two_threads_never_ask_musicbrainz_at_once(self):
        import threading
        import time as _t
        active, worst = [0], [0]
        lock = threading.Lock()

        def get(url):
            with lock:
                active[0] += 1
                worst[0] = max(worst[0], active[0])
            _t.sleep(0.05)
            with lock:
                active[0] -= 1
            return j({"artists": [], "release-groups": []})
        look = info.Lookup(get=get, mb_limit=NoWait())
        ts = [threading.Thread(target=look.artist_id, args=({"albumartist": "A"},)),
              threading.Thread(target=look.album_id, args=({"albumartist": "A", "album": "B"},))]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(worst[0], 1)


class TestAlbumCandidates(unittest.TestCase):
    def test_same_title_twice_the_one_with_an_article_wins(self):
        get = FakeGet([
            (lambda u: "/ws/2/release-group?" in u, j({"release-groups": [
                {"id": "bare", "title": "Highway 61 Revisited", "score": 100},
                {"id": "linked", "title": "Highway 61 Revisited", "score": 100},
                {"id": "other", "title": "Highway 61 Revisited Again", "score": 93}]})),
            (lambda u: "/ws/2/release-group/bare?" in u, j({"relations": []})),
            (lambda u: "/ws/2/release-group/linked?" in u, j({"relations": [
                {"type": "wikidata", "url": {"resource": "https://www.wikidata.org/wiki/Q217211"}}]})),
            (lambda u: "wikidata.org" in u, j({"entities": {"Q217211": {"sitelinks": {
                "enwiki": {"title": "Highway 61 Revisited"}}}}})),
            (lambda u: "en.wikipedia.org" in u, j({"query": {"pages": [{"title": "Highway 61 Revisited",
                                                                      "extract": "The sixth album."}]}}))])
        got = info.Lookup(get=get, mb_limit=NoWait()).album_wiki(
            {"albumartist": "Bob Dylan", "album": "Highway 61 Revisited"})
        self.assertEqual((got["mbid"], got["wiki"]["paragraphs"]), ("linked", ["The sixth album."]))

    def test_none_has_an_article(self):
        get = FakeGet([(lambda u: "/ws/2/release-group?" in u, j({"release-groups": [
                           {"id": "a", "title": "X", "score": 100}, {"id": "b", "title": "X", "score": 90}]})),
                       (lambda u: "/ws/2/release-group/" in u, j({"relations": []}))])
        got = info.Lookup(get=get, mb_limit=NoWait()).album_wiki({"albumartist": "Y", "album": "X"})
        self.assertEqual(got, {"wiki": None, "mbid": "a"})


class TestSimilarMarkup(unittest.TestCase):
    def test_library_artists_are_links_others_text(self):
        got = info.similar_markup(["Codeine", "the beatles", "Nobody <&> Else"], ["Beatles, The", "The Beatles", "Codeine"])
        self.assertEqual(got, '<a href="toccata-artist:Codeine" title="Show in Artists">Codeine</a>, '
                              '<a href="toccata-artist:The%20Beatles" title="Show in Artists">the beatles</a>, '
                              'Nobody &lt;&amp;&gt; Else')

    def test_names_with_quotes_and_slashes_stay_intact(self):
        import urllib.parse
        got = info.similar_markup(['AC/DC "x"'], ['AC/DC "x"'])
        href = got.split('href="')[1].split('"')[0]
        self.assertEqual(urllib.parse.unquote(href[len(info.SIMILAR_LINK):]), 'AC/DC "x"')
        self.assertEqual(info.similar_markup([], ["A"]), "")
