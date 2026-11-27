import json
import unittest
import urllib.parse

from toccata import coversearch as cs
from toccata import lastfm

ITUNES = json.dumps({"resultCount": 2, "results": [
    {"artistName": "The Beatles", "collectionName": "Abbey Road",
     "artworkUrl100": "https://is1.example/a/100x100bb.jpg"},
    {"artistName": "X", "collectionName": "No art"}]}).encode()
DEEZER = json.dumps({"data": [
    {"title": "Abbey Road", "artist": {"name": "The Beatles"},
     "cover_medium": "https://cdn.example/250.jpg", "cover_xl": "https://cdn.example/1000.jpg"},
    {"title": "Nothing", "artist": {"name": "Y"}}]}).encode()
MB = json.dumps({"release-groups": [
    {"id": "9162580e", "title": "Abbey Road",
     "artist-credit": [{"name": "The Beatles", "joinphrase": ""}]},
    {"id": "abc", "title": "Duet", "artist-credit": [{"name": "A", "joinphrase": " & "}, {"name": "B"}]}]}).encode()


class TestParse(unittest.TestCase):
    def test_itunes(self):
        got = cs.parse_itunes(ITUNES)
        self.assertEqual(len(got), 1)  # the entry without artwork is skipped
        self.assertEqual(got[0].thumb_url, "https://is1.example/a/250x250bb.jpg")
        self.assertEqual(got[0].full_url, "https://is1.example/a/1200x1200bb.jpg")
        self.assertEqual(got[0].label, "The Beatles – Abbey Road")

    def test_deezer(self):
        got = cs.parse_deezer(DEEZER)
        self.assertEqual([(c.thumb_url, c.full_url) for c in got],
                         [("https://cdn.example/250.jpg", "https://cdn.example/1000.jpg")])

    def test_musicbrainz(self):
        got = cs.parse_musicbrainz(MB)
        self.assertEqual(got[0].full_url, "https://coverartarchive.org/release-group/9162580e/front")
        self.assertEqual(got[0].thumb_url, "https://coverartarchive.org/release-group/9162580e/front-250")
        self.assertEqual(got[1].artist, "A & B")


class TestUrls(unittest.TestCase):
    def test_query_is_url_encoded(self):
        u = cs.deezer_url("AC/DC & Co")
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlparse(u).query)["q"], ["AC/DC & Co"])

    def test_musicbrainz_fields_and_escaping(self):
        u = cs.musicbrainz_url("x", artist="AC/DC", album='Back "In" Black')
        q = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)["query"][0]
        self.assertEqual(q, 'releasegroup:"Back \\"In\\" Black" AND artist:"AC\\/DC"')

    def test_default_query(self):
        self.assertEqual(cs.default_query("Pita", "Get Down"), "Pita Get Down")
        self.assertEqual(cs.default_query("Unknown", "Hits"), "Hits")

    def test_user_agent_says_nothing_about_the_user(self):
        self.assertTrue(cs.USER_AGENT.startswith("Toccata/"))
        self.assertNotIn("@", cs.USER_AGENT)


class TestSearch(unittest.TestCase):
    def fake_get(self, responses, log):
        def get(url):
            log.append(url)
            host = urllib.parse.urlparse(url).netloc
            r = responses[host]
            if isinstance(r, Exception):
                raise r
            return r
        return get

    def test_all_providers_and_failures_are_tolerated(self):
        log = []
        responses = {"musicbrainz.org": MB, "api.deezer.com": OSError("offline"), "itunes.apple.com": ITUNES}
        found, errors = cs.search("abbey road", "The Beatles", "Abbey Road", get=self.fake_get(responses, log),
                                  mb_limit=cs.RateLimit(0))
        self.assertEqual([c.provider for c in found], ["Cover Art Archive", "Cover Art Archive", "iTunes"])
        self.assertEqual(len(errors), 1)
        self.assertIn("Deezer", errors[0])
        self.assertEqual(len(log), 3)

    def test_musicbrainz_503_is_retried_once(self):
        import urllib.error
        calls = []

        def get(url):
            calls.append(url)
            if "musicbrainz" in url and len([c for c in calls if "musicbrainz" in c]) == 1:
                raise urllib.error.HTTPError(url, 503, "Service Temporarily Unavailable", {}, None)
            return {"musicbrainz.org": MB, "api.deezer.com": DEEZER, "itunes.apple.com": ITUNES}[urllib.parse.urlparse(url).netloc]
        found, errors = cs.search("x", get=get, mb_limit=cs.RateLimit(0, sleep=lambda d: None))
        self.assertEqual(errors, [])
        self.assertEqual(len([c for c in calls if "musicbrainz" in c]), 2)
        self.assertIn("Cover Art Archive", [c.provider for c in found])

    def test_empty_query_sends_nothing(self):
        log = []
        self.assertEqual(cs.search("  ", get=self.fake_get({}, log)), ([], []))
        self.assertEqual(log, [])

    def test_duplicates_dropped(self):
        responses = {"musicbrainz.org": MB, "api.deezer.com": DEEZER, "itunes.apple.com": ITUNES}
        p = cs.PROVIDERS + (("iTunes again", lambda q, a, b: cs.itunes_url(q), cs.parse_itunes),)
        found, _ = cs.search("x", get=self.fake_get(responses, []), providers=p, mb_limit=cs.RateLimit(0))
        urls = [c.full_url for c in found]
        self.assertEqual(len(urls), len(set(urls)))


class TestRateLimit(unittest.TestCase):
    def test_waits_for_the_rest_of_the_interval(self):
        now, slept = [10.0], []
        rl = cs.RateLimit(1.0, clock=lambda: now[0], sleep=lambda d: (slept.append(round(d, 2)), now.__setitem__(0, now[0] + d)))
        rl.wait()
        now[0] += 0.3
        rl.wait()
        self.assertEqual(slept, [0.7])
        now[0] += 5
        rl.wait()
        self.assertEqual(slept, [0.7])


if __name__ == "__main__":
    unittest.main()


LASTFM = json.dumps({"results": {"albummatches": {"album": [
    {"name": "Abbey Road", "artist": "The Beatles", "image": [
        {"#text": "https://lastfm.example/i/u/34s/a.png", "size": "small"},
        {"#text": "https://lastfm.example/i/u/174s/a.png", "size": "large"},
        {"#text": "https://lastfm.example/i/u/300x300/a.png", "size": "extralarge"}]},
    {"name": "Nothing", "artist": "Nobody", "image": [
        {"#text": f"https://lastfm.example/i/u/300x300/{cs.LASTFM_PLACEHOLDER}.png", "size": "extralarge"}]},
    {"name": "Empty", "artist": "X", "image": [{"#text": "", "size": "extralarge"}]}]}}}).encode()


class TestLastfm(unittest.TestCase):
    def test_parse_skips_placeholder_and_empty(self):
        got = cs.parse_lastfm(LASTFM)
        self.assertEqual([(c.album, c.thumb_url, c.full_url) for c in got],
                         [("Abbey Road", "https://lastfm.example/i/u/174s/a.png", "https://lastfm.example/i/u/300x300/a.png")])

    def test_single_match_as_object(self):
        one = json.dumps({"results": {"albummatches": {"album": {"name": "A", "artist": "B", "image": [
            {"#text": "https://x/300.png", "size": "extralarge"}]}}}}).encode()
        self.assertEqual(len(cs.parse_lastfm(one)), 1)

    def test_api_error_is_reported(self):
        with self.assertRaises(ValueError) as e:
            cs.parse_lastfm(json.dumps({"error": 10, "message": "Invalid API key"}).encode())
        self.assertIn("Invalid API key", str(e.exception))

    def test_url_has_the_key(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(cs.lastfm_url("abbey road", "KEY123")).query)
        self.assertEqual((q["method"], q["album"], q["api_key"], q["format"]),
                         (["album.search"], ["abbey road"], ["KEY123"], ["json"]))

    def test_only_searched_with_a_key(self):
        log = []

        def get(url):
            log.append(urllib.parse.urlparse(url).netloc)
            return {"musicbrainz.org": MB, "api.deezer.com": DEEZER, "itunes.apple.com": ITUNES,
                    "ws.audioscrobbler.com": LASTFM}[log[-1]]
        cs.search("x", get=get, mb_limit=cs.RateLimit(0))
        self.assertNotIn("ws.audioscrobbler.com", log)
        log.clear()
        found, errors = cs.search("x", get=get, mb_limit=cs.RateLimit(0), lastfm_key=" KEY ")
        self.assertIn("ws.audioscrobbler.com", log)
        self.assertIn("Last.fm", [c.provider for c in found])

    def test_bad_key_does_not_break_the_others(self):
        def get(url):
            host = urllib.parse.urlparse(url).netloc
            if host == "ws.audioscrobbler.com":
                return json.dumps({"error": 10, "message": "Invalid API key"}).encode()
            return {"musicbrainz.org": MB, "api.deezer.com": DEEZER, "itunes.apple.com": ITUNES}[host]
        guard = lastfm.Guard(interval=0)  # own guard: the pause must not leak into other tests
        found, errors = cs.search("x", get=get, mb_limit=cs.RateLimit(0), lastfm_key="bad", lastfm_guard=guard)
        self.assertTrue(found)
        self.assertEqual(errors, ["This Last.fm API key is invalid - asked again in 60 min"])
        self.assertIsNotNone(guard.paused("bad"))
