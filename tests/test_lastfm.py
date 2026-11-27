import io
import json
import threading
import unittest
import urllib.error
from unittest import mock

from toccata import coversearch, info, lastfm


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(round(s, 3))
        self.t += s


def guard(clock, **kw):
    return lastfm.Guard(clock=clock, sleep=clock.sleep, **kw)


def err(code, message="m"):
    return json.dumps({"error": code, "message": message}).encode()


def http429(url):
    raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, io.BytesIO(b""))


OK = json.dumps({"similarartists": {"artist": [{"name": "Codeine"}]}}).encode()


class TestKeys(unittest.TestCase):
    def test_user_key_wins_over_default(self):
        with mock.patch.object(lastfm, "DEFAULT_API_KEY", "builtin"):
            self.assertEqual(lastfm.effective_key(" mine123 "), "mine123")
            self.assertEqual(lastfm.effective_key(""), "builtin")
            self.assertTrue(lastfm.using_default(""))
            self.assertFalse(lastfm.using_default("mine123"))

    def test_garbage_user_key_falls_back(self):
        with mock.patch.object(lastfm, "DEFAULT_API_KEY", "builtin"):
            for bad in ("not a key!", "a&b=c", "x" * 101, None):
                self.assertEqual(lastfm.effective_key(bad), "builtin", bad)
        self.assertEqual(lastfm.valid_key("a&b"), "")

    def test_no_default_and_no_user_key_means_unused(self):
        with mock.patch.object(lastfm, "DEFAULT_API_KEY", ""):
            self.assertEqual(lastfm.effective_key(""), "")

    def test_shipped_key_looks_like_a_key(self):
        k = lastfm.DEFAULT_API_KEY
        if k:  # 32 hex digits, nothing that would break the URL
            self.assertEqual(len(k), 32)
            int(k, 16)


class TestSpacing(unittest.TestCase):
    def test_requests_are_spaced(self):
        c = Clock()
        g = guard(c)
        for _ in range(3):
            g.before("K")
        self.assertEqual(c.slept, [0.25, 0.25])

    def test_no_wait_after_a_pause(self):
        c = Clock()
        g = guard(c)
        g.before("K")
        c.t += 10
        g.before("K")
        self.assertEqual(c.slept, [])

    def test_spacing_holds_over_threads(self):
        # real clock: 8 requests from 4 threads take at least 7 intervals
        import time
        g = lastfm.Guard(interval=0.02)
        start = time.monotonic()
        threads = [threading.Thread(target=lambda: [g.before("K") for _ in range(2)]) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertGreaterEqual(time.monotonic() - start, 7 * 0.02 - 0.005)


class TestBackoff(unittest.TestCase):
    def test_rate_limit_answer_pauses_the_key(self):
        c = Clock()
        g = guard(c)
        calls = []
        get = lambda u: (calls.append(u), err(29, "Rate Limit Exceeded"))[1]
        with self.assertRaises(lastfm.Limited) as cm:
            lastfm.fetch(get, "u", "K", g)
        self.assertEqual(cm.exception.seconds, 300)
        self.assertIn("rate limit", str(cm.exception))
        self.assertIn("5 min", str(cm.exception))
        # while paused: no request at all
        c.t += 299
        with self.assertRaises(lastfm.Limited) as cm:
            lastfm.fetch(lambda u: OK, "u", "K", g)
        self.assertIn("1 s", str(cm.exception))
        self.assertEqual(len(calls), 1)
        # afterwards it is asked again
        c.t += 2
        self.assertEqual(lastfm.fetch(lambda u: OK, "u", "K", g), OK)

    def test_http_429_pauses_too(self):
        c = Clock()
        g = guard(c)
        with self.assertRaises(lastfm.Limited):
            lastfm.fetch(http429, "u", "K", g)
        self.assertIsNotNone(g.paused("K"))

    def test_pause_is_per_key(self):
        c = Clock()
        g = guard(c)
        with self.assertRaises(lastfm.Limited):
            lastfm.fetch(lambda u: err(26, "Suspended API key"), "u", "builtin", g)
        self.assertEqual(g.paused("builtin")[0], 3600)
        self.assertIn("suspended", g.paused("builtin")[1])
        self.assertEqual(lastfm.fetch(lambda u: OK, "u", "mine", g), OK)  # the user's own key still works

    def test_invalid_key_pauses_an_hour(self):
        g = guard(Clock())
        with self.assertRaises(lastfm.Limited):
            lastfm.fetch(lambda u: err(10, "Invalid API key"), "u", "K", g)
        self.assertEqual(g.paused("K")[0], 3600)

    def test_other_errors_pass_through_without_pause(self):
        g = guard(Clock())
        data = err(6, "The artist you supplied could not be found")
        self.assertEqual(lastfm.fetch(lambda u: data, "u", "K", g), data)
        self.assertIsNone(g.paused("K"))
        with self.assertRaises(ValueError):
            info.parse_lastfm_similar(data)

    def test_other_http_errors_pass_through(self):
        g = guard(Clock())

        def boom(url):
            raise urllib.error.HTTPError(url, 500, "x", {}, io.BytesIO(b""))
        with self.assertRaises(urllib.error.HTTPError):
            lastfm.fetch(boom, "u", "K", g)
        self.assertIsNone(g.paused("K"))

    def test_not_json_passes_through(self):
        g = guard(Clock())
        self.assertEqual(lastfm.fetch(lambda u: b"<html>", "u", "K", g), b"<html>")
        self.assertEqual(lastfm.fetch(lambda u: None, "u", "K", g), None)


class TestCallers(unittest.TestCase):
    def test_similar_goes_through_the_guard_and_is_not_cached_while_limited(self):
        c = Clock()
        g = guard(c)
        answers = [err(29, "Rate Limit Exceeded"), OK]
        calls = []
        get = lambda u: (calls.append(u), answers.pop(0))[1]

        class Cache(dict):
            def get(self, key, ttl):
                return dict.get(self, key)

            def put(self, key, value):
                self[key] = type("Hit", (), {"value": value})

        look = info.Lookup(get=get, cache=Cache(), lastfm_key="K", lastfm_guard=g)
        with self.assertRaises(lastfm.Limited):
            look.similar("Low")
        with self.assertRaises(lastfm.Limited):  # paused: Last.fm not asked
            look.similar("Low")
        self.assertEqual(len(calls), 1)
        c.t += 301
        self.assertEqual(look.similar("Low"), ["Codeine"])

    def test_cover_search_reports_the_pause_and_keeps_other_providers(self):
        c = Clock()
        g = guard(c)
        deezer = json.dumps({"data": []}).encode()
        get = lambda u: err(29, "Rate Limit Exceeded") if "audioscrobbler" in u else deezer
        providers = (("Deezer", lambda q, a, b: coversearch.deezer_url(q), coversearch.parse_deezer),)
        found, errors = coversearch.search("x", get=get, providers=providers, lastfm_key="K", lastfm_guard=g)
        self.assertEqual(found, [])
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("Last.fm rate limit reached"), errors)
        self.assertIsNotNone(g.paused("K"))


if __name__ == "__main__":
    unittest.main()
