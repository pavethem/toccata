import threading
import unittest

from toccata.covers import CoverFetcher, cover_fit_rect
from toccata.mpd_client import ProtocolError


class FakeClient:
    """Stands in for MPDClient. `script` maps folder -> bytes | None | Exception."""

    def __init__(self, script):
        self.script, self.calls, self.connects, self.connected = script, [], 0, False

    def connect(self):
        self.connects += 1
        self.connected = True

    def close(self):
        self.connected = False

    def cover(self, uri):
        folder = uri.rsplit("/", 1)[0]
        self.calls.append(folder)
        result = self.script[folder]
        if isinstance(result, list):  # sequence of outcomes, consumed in order
            result = result.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def fetch(f, uri, timeout=3):
    """Request and wait for the callback; returns the image (or 'TIMEOUT')."""
    done, out = threading.Event(), []
    f.request(uri, lambda img: (out.append(img), done.set()))
    return out[0] if done.wait(timeout) else "TIMEOUT"


def make(script, **kw):
    client = FakeClient(script)
    f = CoverFetcher(client, kw.pop("decode", lambda b: b"IMG:" + b),
                     lambda fn, *a: fn(*a), **kw)
    return f, client


class TestCoverFetcher(unittest.TestCase):
    def tearDown(self):
        pass

    def test_fetch_and_decode(self):
        f, _ = make({"a": b"x"})
        self.assertEqual(fetch(f, "a/1.flac"), b"IMG:x")
        f.shutdown()

    def test_second_track_of_same_album_uses_cache(self):
        f, c = make({"a": b"x"})
        fetch(f, "a/1.flac")
        self.assertEqual(fetch(f, "a/2.flac"), b"IMG:x")
        self.assertEqual(c.calls, ["a"])  # only one network fetch
        f.shutdown()

    def test_missing_cover_is_cached(self):
        f, c = make({"a": None})
        self.assertIsNone(fetch(f, "a/1.flac"))
        self.assertIsNone(fetch(f, "a/2.flac"))
        self.assertEqual(c.calls, ["a"])
        f.shutdown()

    def test_failure_is_not_cached_and_is_retried(self):
        f, c = make({"a": [ProtocolError("x"), ProtocolError("y"), b"ok"]})
        self.assertIsNone(fetch(f, "a/1.flac"))       # both attempts failed
        self.assertEqual(fetch(f, "a/1.flac"), b"IMG:ok")  # retried later
        f.shutdown()

    def test_reconnects_once_within_a_request(self):
        f, c = make({"a": [ProtocolError("dropped"), b"ok"]})
        self.assertEqual(fetch(f, "a/1.flac"), b"IMG:ok")
        self.assertEqual(c.connects, 2)
        f.shutdown()

    def test_undecodable_image_counts_as_no_cover(self):
        def bad(_b):
            raise ValueError("not an image")
        f, c = make({"a": b"junk"}, decode=bad)
        self.assertIsNone(fetch(f, "a/1.flac"))
        self.assertIsNone(fetch(f, "a/2.flac"))
        self.assertEqual(c.calls, ["a"])  # cached as "none", not refetched
        f.shutdown()

    def test_lru_eviction(self):
        f, c = make({k: k.encode() for k in "abc"}, cache_size=2)
        for k in "abc":
            fetch(f, f"{k}/1.flac")
        fetch(f, "a/1.flac")  # 'a' was evicted, must be fetched again
        self.assertEqual(c.calls, ["a", "b", "c", "a"])
        f.shutdown()

    def test_recent_use_protects_from_eviction(self):
        f, c = make({k: k.encode() for k in "abc"}, cache_size=2)
        fetch(f, "a/1.flac")
        fetch(f, "b/1.flac")
        fetch(f, "a/2.flac")   # touch 'a'
        fetch(f, "c/1.flac")   # evicts 'b', not 'a'
        fetch(f, "a/3.flac")
        self.assertEqual(c.calls, ["a", "b", "c"])
        f.shutdown()

    def test_key_for(self):
        self.assertEqual(CoverFetcher.key_for("Artist/Album/01.flac"), "Artist/Album")
        self.assertEqual(CoverFetcher.key_for("loose.flac"), "")


class TestWanted(unittest.TestCase):
    def test_unwanted_request_is_skipped_without_fetching(self):
        f, c = make({"a": b"x"})
        done = threading.Event()
        f.request("a/1.flac", lambda img: done.set(), wanted=lambda: False)
        f._pool.shutdown(wait=True)  # let the queue drain
        self.assertEqual(c.calls, [])      # nothing fetched
        self.assertFalse(done.is_set())    # callback never called

    def test_wanted_request_proceeds(self):
        f, c = make({"a": b"x"})
        done, out = threading.Event(), []
        f.request("a/1.flac", lambda img: (out.append(img), done.set()), wanted=lambda: True)
        self.assertTrue(done.wait(3))
        self.assertEqual(out, [b"IMG:x"])
        f.shutdown()

    def test_request_cancelled_while_queued_behind_another(self):
        gate = threading.Event()

        class SlowClient(FakeClient):
            def cover(self, uri):
                gate.wait(3)
                return super().cover(uri)
        client = SlowClient({"a": b"1", "b": b"2"})
        f = CoverFetcher(client, lambda b: b, lambda fn, *a: fn(*a))
        still_wanted = {"b": True}
        first, second = threading.Event(), threading.Event()
        f.request("a/1.flac", lambda img: first.set())
        f.request("b/1.flac", lambda img: second.set(), wanted=lambda: still_wanted["b"])
        still_wanted["b"] = False  # user moved on while "a" was still loading
        gate.set()
        self.assertTrue(first.wait(3))
        f._pool.shutdown(wait=True)
        self.assertEqual(client.calls, ["a"])   # "b" was never fetched
        self.assertFalse(second.is_set())

    def test_cached_hit_still_calls_back_even_if_unwanted_is_passed(self):
        f, _ = make({"a": b"x"})
        fetch(f, "a/1.flac")
        out = []
        f.request("a/2.flac", out.append, wanted=lambda: False)
        self.assertEqual(out, [b"IMG:x"])  # cache path is synchronous; caller filters stale results
        f.shutdown()


class TestCoverFitRect(unittest.TestCase):
    def test_square_image_fills_box_exactly(self):
        self.assertEqual(cover_fit_rect(72, 72, 1000, 1000), (0, 0, 72, 72))

    def test_large_image_is_scaled_down_not_drawn_at_native_size(self):
        _, _, w, h = cover_fit_rect(72, 72, 3000, 3000)
        self.assertEqual((w, h), (72, 72))

    def test_wide_image_fills_height_and_is_cropped_sideways(self):
        x, y, w, h = cover_fit_rect(72, 72, 200, 100)
        self.assertEqual((y, h), (0, 72))
        self.assertEqual(w, 144)
        self.assertEqual(x, -36)  # centred: 36px cropped on each side

    def test_tall_image_fills_width_and_is_cropped_vertically(self):
        x, y, w, h = cover_fit_rect(72, 72, 100, 200)
        self.assertEqual((x, w), (0, 72))
        self.assertEqual(h, 144)
        self.assertEqual(y, -36)

    def test_small_image_is_scaled_up(self):
        self.assertEqual(cover_fit_rect(72, 72, 36, 36), (0, 0, 72, 72))

    def test_degenerate_sizes_fall_back_to_box(self):
        self.assertEqual(cover_fit_rect(72, 72, 0, 0), (0.0, 0.0, 72.0, 72.0))


if __name__ == "__main__":
    unittest.main()


class TestMissExpiry(unittest.TestCase):
    def test_no_cover_is_asked_again_after_ttl(self):
        now = [1000.0]
        f, c = make({"a": [None, b"new"]}, miss_ttl=600, clock=lambda: now[0])
        self.assertIsNone(fetch(f, "a/1.flac"))
        now[0] += 599
        self.assertIsNone(fetch(f, "a/1.flac"))   # still believed
        self.assertEqual(c.calls, ["a"])
        now[0] += 2
        self.assertEqual(fetch(f, "a/1.flac"), b"IMG:new")  # cover.jpg appeared meanwhile
        self.assertEqual(c.calls, ["a", "a"])
        f.shutdown()

    def test_found_cover_does_not_expire(self):
        now = [0.0]
        f, c = make({"a": b"x"}, miss_ttl=1, clock=lambda: now[0])
        fetch(f, "a/1.flac")
        now[0] += 10 ** 6
        fetch(f, "a/1.flac")
        self.assertEqual(c.calls, ["a"])
        f.shutdown()


class TestFolderCovers(unittest.TestCase):
    """Album folders whose cover MPD's albumart cannot see (real cases from the user's library)."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def put(self, rel, data=b"img"):
        import os
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def cover(self, uri, **song):
        from toccata.covers import read_folder_cover
        return read_folder_cover(self.root, uri, song)

    def test_album_name_file(self):
        self.put("Can/Tago Mago/01.mp3")
        self.put("Can/Tago Mago/Can_-_Tago_Mago-front.jpg", b"front")
        self.put("Can/Tago Mago/Tago Mago.jpg", b"named")
        # the album name beats an arbitrary image, although it sorts later
        self.assertEqual(self.cover("Can/Tago Mago/01.mp3", albumartist="CAN", album="Tago Mago"), b"named")

    def test_artist_dash_album_ignores_case(self):
        self.put("Can/Saw/Can - Saw Delight.jpg", b"ad")
        self.put("Can/Saw/0front.png", b"other")  # sorts before the named file
        self.assertEqual(self.cover("Can/Saw/1.mp3", albumartist="CAN", album="Saw Delight"), b"ad")

    def test_standard_names_come_first(self):
        self.put("A/X/X.jpg", b"album")
        self.put("A/X/folder.png", b"folder")
        self.put("A/X/Cover.JPG", b"cover")
        self.assertEqual(self.cover("A/X/1.flac", artist="A", album="X"), b"cover")

    def test_any_image_by_name_when_nothing_matches(self):
        self.put("A/X/zz.jpg", b"z")
        self.put("A/X/front.jpeg", b"f")
        self.put("A/X/notes.txt", b"t")
        self.assertEqual(self.cover("A/X/1.flac", artist="A", album="X"), b"f")

    def test_subfolder(self):
        self.put("A/X/Scans/front.jpg", b"scan")
        self.assertEqual(self.cover("A/X/1.flac", artist="A", album="X"), b"scan")

    def test_nothing(self):
        self.put("A/X/1.flac", b"audio")
        self.assertIsNone(self.cover("A/X/1.flac", artist="A", album="X"))

    def test_no_music_dir_or_stream_or_escape(self):
        from toccata.covers import read_folder_cover, song_folder
        self.put("outside.jpg")
        self.assertIsNone(read_folder_cover(None, "A/1.flac", {}))
        self.assertIsNone(song_folder(self.root, "http://radio/x"))
        self.assertIsNone(song_folder(self.root + "/A", "../../x.flac"))
        self.assertIsNone(song_folder(self.root, "/etc/passwd"))

    def test_fetcher_falls_back_to_the_folder(self):
        self.put("A/X/X.jpg", b"folder")
        f, _ = make({"A/X": None}, music_dir=lambda: self.root)
        done, out = threading.Event(), []
        f.request("A/X/1.flac", lambda img: (out.append(img), done.set()), song={"artist": "A", "album": "X"})
        done.wait(3)
        self.assertEqual(out, [b"IMG:folder"])
        f.shutdown()


class TestPreviewSize(unittest.TestCase):
    def test_big_cover_is_scaled_down_to_fit(self):
        from toccata.covers import preview_size
        self.assertEqual(preview_size(1200, 1200, 600, 500), (500, 500))

    def test_aspect_is_kept_not_cropped(self):
        from toccata.covers import preview_size
        self.assertEqual(preview_size(1000, 500, 600, 600), (600, 300))

    def test_native_size_when_it_fits(self):
        from toccata.covers import preview_size
        self.assertEqual(preview_size(500, 400, 800, 800), (500, 400))

    def test_small_cover_is_enlarged_to_min_side(self):
        from toccata.covers import preview_size
        self.assertEqual(preview_size(100, 100, 800, 800), (320, 320))
        self.assertEqual(preview_size(100, 100, 200, 800), (200, 200))  # still within the box

    def test_degenerate(self):
        from toccata.covers import preview_size
        self.assertEqual(preview_size(0, 10, 100, 100), (0, 0))
        self.assertEqual(preview_size(10, 10, 0, 100), (0, 0))


class TestThumbnailsAndDiskCache(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = [1_000_000.0]

    def make(self, script, **kw):
        return make(script, thumbnail=lambda data, size: b"T%d:" % size + data,
                    disk_dir=self.tmp.name, wall_clock=lambda: self.now[0], **kw)

    def get(self, f, uri, size):
        done, out = threading.Event(), []
        f.request(uri, lambda img: (out.append(img), done.set()), size=size)
        self.assertTrue(done.wait(3))
        return out[0]

    def test_thumbnail_is_made_written_and_used_next_time(self):
        import os
        f, c = self.make({"a": b"big"})
        self.assertEqual(self.get(f, "a/1.flac", 128), b"IMG:T128:big")
        f.shutdown()
        self.assertEqual(len(os.listdir(self.tmp.name)), 1)
        g, c2 = self.make({"a": b"other"})  # a new session: memory cache empty
        self.assertEqual(self.get(g, "a/1.flac", 128), b"IMG:T128:big")
        self.assertEqual(c2.calls, [])  # straight from disk, MPD not asked
        g.shutdown()

    def test_old_thumbnail_is_fetched_again(self):
        from toccata.covers import DISK_MAX_AGE
        import os
        f, _ = self.make({"a": b"v1"})
        self.get(f, "a/1.flac", 128)
        f.shutdown()
        path = os.path.join(self.tmp.name, os.listdir(self.tmp.name)[0])
        os.utime(path, (self.now[0] - DISK_MAX_AGE - 10,) * 2)
        g, c = self.make({"a": b"v2"})
        self.assertEqual(self.get(g, "a/1.flac", 128), b"IMG:T128:v2")
        self.assertEqual(c.calls, ["a"])
        g.shutdown()

    def test_sizes_are_kept_apart_and_full_size_is_not_on_disk(self):
        import os
        f, c = self.make({"a": b"x"})
        self.assertEqual(self.get(f, "a/1.flac", 128), b"IMG:T128:x")
        self.assertEqual(self.get(f, "a/1.flac", None), b"IMG:x")  # full image, decoded as is
        self.assertEqual(len(os.listdir(self.tmp.name)), 1)
        f.shutdown()

    def test_missing_cover_is_not_written(self):
        import os
        f, _ = self.make({"a": None})
        self.assertIsNone(self.get(f, "a/1.flac", 128))
        self.assertEqual(os.listdir(self.tmp.name), [])
        f.shutdown()

    def test_failing_thumbnailer_counts_as_no_cover(self):
        def bad(data, size):
            raise ValueError("not an image")
        f, _ = make({"a": b"junk"}, thumbnail=bad, disk_dir=self.tmp.name)
        self.assertIsNone(self.get(f, "a/1.flac", 128))
        f.shutdown()


class TestBinaryLimit(unittest.TestCase):
    def test_sent_once_per_connection(self):
        from toccata.covers import BINARY_LIMIT

        class Limited(FakeClient):
            limits = []

            def set_binary_limit(self, n):
                Limited.limits.append(n)
                return True
        client = Limited({"a": b"x", "b": b"y"})
        f = CoverFetcher(client, lambda b: b, lambda fn, *a: fn(*a))
        fetch(f, "a/1.flac")
        fetch(f, "b/1.flac")
        self.assertEqual(Limited.limits, [BINARY_LIMIT])
        client.connected = False  # dropped: the next connection needs it again
        client.script["c"] = b"z"
        fetch(f, "c/1.flac")
        self.assertEqual(Limited.limits, [BINARY_LIMIT, BINARY_LIMIT])
        f.shutdown()


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 20
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20


class TestSetCover(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        for d in ("A/X", "A/Y/CD1", "A/Y/CD2"):
            os.makedirs(os.path.join(self.root, d))

    def test_album_folders(self):
        from toccata.covers import album_folders
        import os
        got = album_folders(self.root, ["A/Y/CD1/1.flac", "A/Y/CD1/2.flac", "A/Y/CD2/1.flac", "../etc/x", "http://s"])
        self.assertEqual(got, [os.path.join(self.root, "A/Y/CD1"), os.path.join(self.root, "A/Y/CD2")])

    def test_extension_from_content(self):
        from toccata.covers import cover_extension
        self.assertEqual(cover_extension(JPEG), ".jpg")
        self.assertEqual(cover_extension(PNG), ".png")
        self.assertEqual(cover_extension(b"RIFF\x00\x00\x00\x00WEBPVP8 "), ".webp")
        self.assertIsNone(cover_extension(b"GIF89a"))

    def test_write_new_cover(self):
        from toccata.covers import write_cover
        import os
        folder = os.path.join(self.root, "A/X")
        path = write_cover(folder, JPEG)
        self.assertEqual(path, os.path.join(folder, "cover.jpg"))
        self.assertEqual(open(path, "rb").read(), JPEG)
        self.assertEqual(sorted(os.listdir(folder)), ["cover.jpg"])  # no temporary file left

    def test_replacing_keeps_the_old_one_and_removes_competitors(self):
        from toccata.covers import write_cover, existing_covers
        import os
        folder = os.path.join(self.root, "A/X")
        open(os.path.join(folder, "cover.jpg"), "wb").write(b"old jpeg")
        open(os.path.join(folder, "Cover.PNG"), "wb").write(b"old png")
        self.assertEqual(len(existing_covers(folder)), 2)
        write_cover(folder, PNG)
        self.assertEqual(sorted(os.listdir(folder)), ["Cover.PNG.old", "cover.jpg.old", "cover.png"])
        self.assertEqual(existing_covers(folder), [os.path.join(folder, "cover.png")])

    def test_errors(self):
        from toccata.covers import CoverWriteError, write_cover
        import os
        with self.assertRaises(CoverWriteError):
            write_cover(os.path.join(self.root, "A/X"), b"not an image")
        ro = os.path.join(self.root, "A/Y/CD1")
        os.chmod(ro, 0o555)
        self.addCleanup(os.chmod, ro, 0o755)
        if not os.access(ro, os.W_OK):  # (root can write anyway)
            with self.assertRaises(CoverWriteError):
                write_cover(ro, JPEG)
            self.assertEqual(os.listdir(ro), [])


class TestForget(unittest.TestCase):
    def test_forget_drops_memory_and_disk(self):
        import os
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        f, c = make({"a": [b"v1", b"v1 full", b"v2"], "b": b"other"}, thumbnail=lambda d, s: b"T:" + d,
                    disk_dir=tmp.name)
        done = threading.Event()
        out = []
        for uri, size in (("a/1.flac", 128), ("a/1.flac", None), ("b/1.flac", 128)):
            done.clear()
            f.request(uri, lambda img: (out.append(img), done.set()), size=size)
            done.wait(3)
        self.assertEqual(len(os.listdir(tmp.name)), 2)
        f.forget("a")
        self.assertEqual(len(os.listdir(tmp.name)), 1)  # b's thumbnail stays
        self.assertEqual([k[0] for k in f._cache], ["b"])
        done.clear()
        f.request("a/1.flac", lambda img: (out.append(img), done.set()), size=128)
        done.wait(3)
        self.assertEqual(out[-1], b"IMG:T:v2")  # fetched again: the new cover
        f.shutdown()
