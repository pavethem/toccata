import json
import os
import stat
import tempfile
import unittest

from toccata import settings as st


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "sub", "settings.json")

    def test_defaults_when_file_missing(self):
        self.assertEqual(st.load(self.path), st.Settings())

    def test_round_trip(self):
        s = st.Settings(host="srv", port=6601, password="pw", music_dir="/m",
                        replaygain="album", apply_replaygain=False)
        st.save(s, self.path)
        self.assertEqual(st.load(self.path), s)

    def test_last_artist_round_trips(self):
        st.save(st.Settings(last_artist="Guns N' Roses"), self.path)
        self.assertEqual(st.load(self.path).last_artist, "Guns N' Roses")

    def test_search_category_and_view_round_trip(self):
        st.save(st.Settings(search_category="composer", view="search"), self.path)
        s = st.load(self.path)
        self.assertEqual((s.search_category, s.view), ("composer", "search"))

    def test_queue_compact_round_trips_and_rejects_non_bools(self):
        self.assertFalse(st.Settings().queue_compact)
        st.save(st.Settings(queue_compact=True), self.path)
        self.assertTrue(st.load(self.path).queue_compact)
        self.assertFalse(st._clean({"queue_compact": "yes"}).queue_compact)

    def test_queue_grouped_round_trips_and_rejects_non_bools(self):
        st.save(st.Settings(queue_grouped=True), self.path)
        self.assertTrue(st.load(self.path).queue_grouped)
        self.assertFalse(st._clean({"queue_grouped": "yes"}).queue_grouped)

    def test_genre_round_trips_and_rejects_non_strings(self):
        st.save(st.Settings(genre="Acid Jazz"), self.path)
        self.assertEqual(st.load(self.path).genre, "Acid Jazz")
        self.assertEqual(st._clean({"genre": ["x"]}).genre, "")

    def test_lastfm_key(self):
        self.assertEqual(st.Settings().lastfm_api_key, "")
        st.save(st.Settings(lastfm_api_key="0123abcd"), self.path)
        self.assertEqual(st.load(self.path).lastfm_api_key, "0123abcd")
        self.assertEqual(st._clean({"lastfm_api_key": "a b; rm"}).lastfm_api_key, "")

    def test_composer_genres_default_off_and_round_trip(self):
        self.assertEqual(st.Settings().composer_genres, [])
        st.save(st.Settings(composer_genres=["Classical", "Opera"]), self.path)
        self.assertEqual(st.load(self.path).composer_genres, ["Classical", "Opera"])
        self.assertEqual(st._clean({"composer_genres": "Classical"}).composer_genres, [])

    def test_ignore_prefixes_default_and_round_trip(self):
        self.assertEqual(st.Settings().ignore_prefixes, ["The"])
        st.save(st.Settings(ignore_prefixes=["The", "Die"]), self.path)
        self.assertEqual(st.load(self.path).ignore_prefixes, ["The", "Die"])
        self.assertEqual(st._clean({"ignore_prefixes": "The"}).ignore_prefixes, ["The"])  # garbage: default
        self.assertEqual(st._clean({"ignore_prefixes": []}).ignore_prefixes, [])  # none: allowed

    def test_albums_sort_defaults_to_recently_added(self):
        self.assertEqual(st.Settings().albums_sort, "added")
        st.save(st.Settings(albums_sort="year"), self.path)
        self.assertEqual(st.load(self.path).albums_sort, "year")

    def test_playlist_grouped_defaults_on_and_round_trips(self):
        self.assertTrue(st.Settings().playlist_grouped)
        st.save(st.Settings(playlist_grouped=False), self.path)
        self.assertFalse(st.load(self.path).playlist_grouped)

    def test_tray_settings_default_on_and_round_trip(self):
        self.assertTrue(st.Settings().tray_icon)
        self.assertTrue(st.Settings().close_to_tray)
        st.save(st.Settings(tray_icon=False, close_to_tray=False), self.path)
        s = st.load(self.path)
        self.assertEqual((s.tray_icon, s.close_to_tray), (False, False))
        self.assertTrue(st._clean({"tray_icon": "no"}).tray_icon)

    def test_search_category_and_view_reject_garbage(self):
        s = st._clean({"search_category": 3, "view": "x" * 100})
        self.assertEqual((s.search_category, s.view), ("", ""))

    def test_last_artist_defaults_to_empty_and_rejects_non_strings(self):
        self.assertEqual(st.Settings().last_artist, "")
        self.assertEqual(st._clean({"last_artist": 5}).last_artist, "")

    def test_old_settings_file_without_last_artist_still_loads(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            json.dump({"host": "box", "port": 6601}, f)
        s = st.load(self.path)
        self.assertEqual((s.host, s.port, s.last_artist), ("box", 6601, ""))

    def test_divider_positions_round_trip(self):
        st.save(st.Settings(main_split=777, artist_split=222), self.path)
        s = st.load(self.path)
        self.assertEqual((s.main_split, s.artist_split), (777, 222))

    def test_divider_defaults_to_zero_meaning_use_default(self):
        s = st.Settings()
        self.assertEqual((s.main_split, s.artist_split), (0, 0))

    def test_garbage_divider_values_fall_back_to_default(self):
        for bad in (-5, 0, st.MAX_SPLIT + 1, True, "300", 12.5, None, [1]):
            c = st._clean({"main_split": bad, "artist_split": bad})
            self.assertEqual((c.main_split, c.artist_split), (0, 0), repr(bad))

    def test_divider_limits_are_inclusive(self):
        c = st._clean({"main_split_max": 900, "artist_split_max": -1})
        self.assertEqual((c.main_split_max, c.artist_split_max), (900, 0))
        c = st._clean({"main_split": 700, "artist_split": 250})  # older file: no *_max yet
        self.assertEqual((c.main_split_max, c.artist_split_max), (700, 250))
        c = st._clean({"main_split": 700, "main_split_max": 0})  # 0 = never dragged maximized
        self.assertEqual(c.main_split_max, 0)
        c = st._clean({"main_split": 1, "artist_split": st.MAX_SPLIT})
        self.assertEqual((c.main_split, c.artist_split), (1, st.MAX_SPLIT))

    def test_one_bad_divider_does_not_spoil_the_other(self):
        c = st._clean({"main_split": "x", "artist_split": 300})
        self.assertEqual((c.main_split, c.artist_split), (0, 300))

    def test_old_file_without_dividers_keeps_other_settings(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            json.dump({"host": "box", "last_artist": "Band"}, f)
        s = st.load(self.path)
        self.assertEqual((s.host, s.last_artist, s.main_split, s.artist_split), ("box", "Band", 0, 0))

    def test_window_state_round_trips(self):
        st.save(st.Settings(window_width=1000, window_height=650, window_maximized=True), self.path)
        s = st.load(self.path)
        self.assertEqual((s.window_width, s.window_height, s.window_maximized), (1000, 650, True))

    def test_window_defaults(self):
        s = st.Settings()
        self.assertEqual((s.window_width, s.window_height, s.window_maximized), (0, 0, False))

    def test_garbage_window_values_fall_back_to_default(self):
        for bad in (-1, 0, st.MAX_WINDOW + 1, True, "800", 800.5, None):
            c = st._clean({"window_width": bad, "window_height": bad})
            self.assertEqual((c.window_width, c.window_height), (0, 0), repr(bad))
            c = st._clean({"cover_dialog_width": bad, "cover_dialog_height": bad})
            self.assertEqual((c.cover_dialog_width, c.cover_dialog_height), (0, 0), repr(bad))

    def test_cover_dialog_size_round_trips(self):
        st.save(st.Settings(cover_dialog_width=900, cover_dialog_height=700), self.path)
        s = st.load(self.path)
        self.assertEqual((s.cover_dialog_width, s.cover_dialog_height), (900, 700))
        for bad in (1, 0, "yes", None, [True]):
            self.assertFalse(st._clean({"window_maximized": bad}).window_maximized, repr(bad))

    def test_maximized_must_be_a_real_bool_and_does_not_disturb_apply_replaygain(self):
        c = st._clean({"window_maximized": True, "apply_replaygain": False})
        self.assertEqual((c.window_maximized, c.apply_replaygain), (True, False))
        c = st._clean({"window_maximized": "yes"})
        self.assertEqual((c.window_maximized, c.apply_replaygain), (False, True))  # defaults

    def test_old_file_without_window_state_loads(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            json.dump({"host": "box", "main_split": 700}, f)
        s = st.load(self.path)
        self.assertEqual((s.host, s.main_split, s.window_width, s.window_maximized), ("box", 700, 0, False))

    def test_unicode_survives(self):
        s = st.Settings(music_dir="/home/me/Musik/Björk ♥", password="pässwörd")
        st.save(s, self.path)
        self.assertEqual(st.load(self.path), s)

    def test_file_is_private(self):
        st.save(st.Settings(password="secret"), self.path)
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode & 0o077, 0, oct(mode))

    def test_no_temp_files_left_behind(self):
        st.save(st.Settings(), self.path)
        st.save(st.Settings(host="x"), self.path)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["settings.json"])

    def test_failed_save_cleans_up_and_keeps_old_file(self):
        st.save(st.Settings(host="old"), self.path)
        with self.assertRaises(TypeError):
            st.save(st.Settings(host=object()), self.path)  # not JSON-serialisable
        self.assertEqual(st.load(self.path).host, "old")
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["settings.json"])

    def test_corrupt_json_gives_defaults(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write("{not json")
        self.assertEqual(st.load(self.path), st.Settings())

    def test_wrong_top_level_type_gives_defaults(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            json.dump([1, 2, 3], f)
        self.assertEqual(st.load(self.path), st.Settings())

    def test_invalid_fields_fall_back_individually(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            json.dump({"host": "  ", "port": 99999, "password": 5, "replaygain": "loud",
                       "apply_replaygain": "yes", "music_dir": "/ok", "unknown": 1}, f)
        s = st.load(self.path)
        self.assertEqual((s.host, s.port, s.password, s.replaygain, s.apply_replaygain, s.music_dir),
                         ("localhost", 6600, "", "", True, "/ok"))

    def test_port_must_be_real_int(self):
        self.assertEqual(st._clean({"port": True}).port, 6600)
        self.assertEqual(st._clean({"port": "6600"}).port, 6600)
        self.assertEqual(st._clean({"port": 0}).port, 6600)

    def test_host_is_stripped(self):
        self.assertEqual(st._clean({"host": "  box  "}).host, "box")

    def test_config_path_honours_xdg(self):
        self.assertEqual(st.config_path({"XDG_CONFIG_HOME": "/x"}), "/x/toccata/settings.json")

    def test_connection_tuple_maps_empty_password_to_none(self):
        self.assertEqual(st.Settings(password="").connection(), ("localhost", 6600, None))
        self.assertEqual(st.Settings(password="p").connection(), ("localhost", 6600, "p"))


class TestParseMpdHost(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(st.parse_mpd_host("box"), ("box", None))

    def test_password(self):
        self.assertEqual(st.parse_mpd_host("pw@box"), ("box", "pw"))

    def test_password_containing_at_splits_on_first(self):
        self.assertEqual(st.parse_mpd_host("p@ss@box"), ("ss@box", "p"))

    def test_abstract_socket_is_not_a_password(self):
        self.assertEqual(st.parse_mpd_host("@mpd"), ("@mpd", None))

    def test_unix_socket_path(self):
        self.assertEqual(st.parse_mpd_host("/run/mpd/socket"), ("/run/mpd/socket", None))


class TestEffective(unittest.TestCase):
    saved = st.Settings(host="saved", port=7000, password="savedpw", music_dir="/saved")

    def test_saved_used_when_nothing_overrides(self):
        eff, over = st.effective(self.saved, env={})
        self.assertEqual(eff, self.saved)
        self.assertEqual(over, {})

    def test_env_overrides_saved(self):
        eff, over = st.effective(self.saved, env={"MPD_HOST": "envhost", "MPD_PORT": "6601",
                                                  "TOCCATA_MUSIC_DIR": "/env"})
        self.assertEqual((eff.host, eff.port, eff.music_dir), ("envhost", 6601, "/env"))
        self.assertEqual(eff.password, "savedpw")  # env had no password
        self.assertEqual(over, {"host": "MPD_HOST", "port": "MPD_PORT",
                                "music_dir": "TOCCATA_MUSIC_DIR"})

    def test_password_in_mpd_host_overrides_saved_password(self):
        eff, over = st.effective(self.saved, env={"MPD_HOST": "pw@box"})
        self.assertEqual((eff.host, eff.password), ("box", "pw"))
        self.assertEqual(over["password"], "MPD_HOST")

    def test_flags_beat_env_and_saved(self):
        eff, over = st.effective(self.saved, cli_host="flaghost", cli_port=1234,
                                 cli_password="flagpw", cli_music_dir="/flag",
                                 env={"MPD_HOST": "pw@envhost", "MPD_PORT": "9999"})
        self.assertEqual((eff.host, eff.port, eff.password, eff.music_dir),
                         ("flaghost", 1234, "flagpw", "/flag"))
        self.assertEqual(set(over.values()), {"--host", "--port", "--password", "--music-dir"})

    def test_password_flag_beats_password_from_env_host(self):
        eff, over = st.effective(self.saved, cli_password="flagpw", env={"MPD_HOST": "envpw@box"})
        self.assertEqual(eff.password, "flagpw")
        self.assertEqual(over["password"], "--password")

    def test_invalid_env_port_ignored(self):
        for bad in ("abc", "0", "70000", ""):
            eff, over = st.effective(self.saved, env={"MPD_PORT": bad})
            self.assertEqual(eff.port, 7000, bad)
            self.assertNotIn("port", over)

    def test_does_not_mutate_saved(self):
        before = st.Settings(**self.saved.__dict__)
        st.effective(self.saved, cli_host="x", env={"MPD_PORT": "1"})
        self.assertEqual(self.saved, before)

    def test_empty_env_host_is_ignored(self):
        eff, over = st.effective(self.saved, env={"MPD_HOST": ""})
        self.assertEqual(eff.host, "saved")
        self.assertEqual(over, {})


if __name__ == "__main__":
    unittest.main()

