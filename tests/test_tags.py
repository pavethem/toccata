import os
import tempfile
import unittest

from toccata import tags


class TestTagHelpers(unittest.TestCase):
    def test_merge_same_and_mixed(self):
        a = {k: "x" for k, _ in tags.FIELDS}
        b = dict(a, title="y")
        m = tags.merge_for_edit([a, b])
        self.assertEqual(m["artist"], ("x", False))
        self.assertEqual(m["title"], ("", True))

    def test_merge_single_file(self):
        a = {k: "v" for k, _ in tags.FIELDS}
        self.assertEqual(tags.merge_for_edit([a])["album"], ("v", False))

    def test_resolve_path_blocks_escape(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "ok.flac"), "w").close()
            self.assertTrue(tags.resolve_path(d, "ok.flac").endswith("ok.flac"))
            with self.assertRaises(tags.TagError):
                tags.resolve_path(d, "../etc/passwd")
            with self.assertRaises(tags.TagError):
                tags.resolve_path(d, "missing.flac")

    def test_resolve_path_requires_music_dir(self):
        with self.assertRaises(tags.TagError):
            tags.resolve_path(None, "a.flac")

    def test_write_empty_changes_is_noop(self):
        tags.write_tags("/nonexistent", {})  # must not even try to open


try:
    import mutagen  # noqa: F401
    HAVE_MUTAGEN = True
except ImportError:
    HAVE_MUTAGEN = False


@unittest.skipUnless(HAVE_MUTAGEN, "mutagen not installed")
class TestRoundTrip(unittest.TestCase):
    def test_unsupported_file_raises(self):
        with tempfile.NamedTemporaryFile(suffix=".txt") as f:
            f.write(b"not audio")
            f.flush()
            with self.assertRaises(tags.TagError):
                tags.read_tags(f.name)


if __name__ == "__main__":
    unittest.main()


class TestMultiEdit(unittest.TestCase):
    def files(self):
        base = {k: "" for k, _ in tags.FIELDS}
        return [dict(base, title="One", album="Hits", artist="A", tracknumber="1/2"),
                dict(base, title="Two", album="Hits", artist="B", tracknumber="2/2")]

    def test_all_tracks_sets_every_file_and_keeps_mixed(self):
        m = tags.MultiEdit(self.files())
        self.assertEqual(m.view(None)["artist"], ("", True))
        m.commit(None, {"album": "Best of", "artist": "", "title": ""})  # mixed + empty: untouched
        self.assertEqual(m.changes(), [(0, {"album": "Best of"}), (1, {"album": "Best of"})])

    def test_single_track_only_that_file(self):
        m = tags.MultiEdit(self.files())
        m.commit(1, {"title": " Zwei ", "album": "Hits"})
        self.assertEqual(m.changes(), [(1, {"title": "Zwei"})])
        self.assertEqual(m.view(1)["title"], ("Zwei", False))
        self.assertEqual(m.view(0)["title"], ("One", False))

    def test_switching_keeps_both_kinds_of_edits(self):
        m = tags.MultiEdit(self.files())
        m.commit(0, {"artist": "Same"})
        m.commit(1, {"artist": "Same"})
        self.assertEqual(m.view(None)["artist"], ("Same", False))  # no longer mixed
        m.commit(None, {"genre": "Pop", "artist": "Same"})
        self.assertEqual(m.changes(), [(0, {"artist": "Same", "genre": "Pop"}), (1, {"artist": "Same", "genre": "Pop"})])

    def test_clearing_a_common_field_deletes_it(self):
        m = tags.MultiEdit(self.files())
        m.commit(None, {"album": ""})
        self.assertEqual(m.changes(), [(0, {"album": ""}), (1, {"album": ""})])

    def test_changing_back_is_no_change(self):
        m = tags.MultiEdit(self.files())
        m.commit(0, {"title": "Uno"})
        m.commit(0, {"title": "One"})
        self.assertEqual(m.changes(), [])
        self.assertFalse(m.edited(0))

    def test_labels(self):
        m = tags.MultiEdit(self.files())
        self.assertEqual(m.label(0), "1. One")
        m.commit(1, {"title": "", "tracknumber": ""})
        self.assertEqual(m.label(1), "Track 2 •")


class TestReplayGain(unittest.TestCase):
    def test_vorbis_style(self):
        rg = tags.replaygain_from_items([("REPLAYGAIN_TRACK_GAIN", ["-7.23 dB"]), ("replaygain_track_peak", ["0.988"]),
                                         ("REPLAYGAIN_ALBUM_GAIN", ["-6.50 dB"])])
        self.assertEqual(rg, {"track_gain": -7.23, "track_peak": 0.988, "album_gain": -6.5, "album_peak": None})

    def test_id3_txxx_and_mp4_freeform(self):
        class Frame:
            text = ["+1,5 dB"]
        rg = tags.replaygain_from_items([("TXXX:REPLAYGAIN_TRACK_GAIN", Frame()),
                                         ("----:com.apple.iTunes:replaygain_album_gain", [b"-3.00 dB"])])
        self.assertEqual((rg["track_gain"], rg["album_gain"]), (1.5, -3.0))

    def test_opus_r128_converted_unless_replaygain_present(self):
        rg = tags.replaygain_from_items([("R128_TRACK_GAIN", ["-1280"])])  # -5 dB at -23 LUFS
        self.assertEqual(rg["track_gain"], 0.0)
        rg = tags.replaygain_from_items([("R128_TRACK_GAIN", ["-1280"]), ("REPLAYGAIN_TRACK_GAIN", ["-2 dB"])])
        self.assertEqual(rg["track_gain"], -2.0)

    def test_missing_and_garbage(self):
        rg = tags.replaygain_from_items([("REPLAYGAIN_TRACK_GAIN", ["n/a"]), ("title", ["x"])])
        self.assertEqual(rg, {f: None for f in tags.RG_FIELDS})

    def test_formatting(self):
        self.assertEqual(tags.format_gain(-7.234), "-7.23 dB")
        self.assertEqual(tags.format_gain(1.5), "+1.50 dB")
        self.assertEqual(tags.format_gain(None), "–")
        self.assertEqual(tags.format_peak(1.0), "1.000 (0.00 dBFS)")
        self.assertEqual(tags.format_peak(0.5), "0.500 (-6.02 dBFS)")
        self.assertEqual(tags.format_peak(0.0), "0.000")


class TestReplayGainFiles(unittest.TestCase):
    """Real files made with ffmpeg (skipped without it)."""

    def make(self, suffix, **meta):
        import shutil, subprocess
        if not shutil.which("ffmpeg"):
            self.skipTest("no ffmpeg")
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        p = os.path.join(d, "t" + suffix)
        args = ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "0.1"]
        for k, v in meta.items():
            args += ["-metadata", f"{k}={v}"]
        subprocess.run(args + [p], check=True)
        return p

    def test_flac_and_mp3(self):
        for suffix in (".flac", ".mp3"):
            p = self.make(suffix, REPLAYGAIN_TRACK_GAIN="-7.23 dB", REPLAYGAIN_TRACK_PEAK="0.988000",
                          REPLAYGAIN_ALBUM_GAIN="-6.50 dB")
            rg = tags.read_replaygain(p)
            self.assertEqual((rg["track_gain"], rg["track_peak"], rg["album_gain"], rg["album_peak"]),
                             (-7.23, 0.988, -6.5, None), suffix)

    def test_no_tags(self):
        p = self.make(".flac")
        self.assertEqual(tags.read_replaygain(p), {f: None for f in tags.RG_FIELDS})
