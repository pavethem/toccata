"""Tests the MPD client against an in-process mock server. Run: python3 -m unittest"""
import socketserver
import threading
import unittest

import socket

from toccata.mpd_client import (MPDClient, CommandError, ProtocolError, ack_code,
                                    describe_error, group_songs, probe, quote)

SONGS = [
    ("a/1.flac", "Intro", "Band", "Debut", 1),
    ("a/2.flac", "Song \"Two\"", "Band", "Debut", 2),
    ("b/1.flac", "Other", "Solo", "Album B", 1),
]
IMG = bytes(range(256)) * 3  # 768 bytes, delivered in 2 chunks
EMBEDDED = b"\x89PNG-embedded" * 50  # only available through readpicture
LISTALLINFO = (
    "directory: a\nLast-Modified: 2020-01-01T00:00:00Z\n"
    "file: a/1.flac\nLast-Modified: 2021-02-02T00:00:00Z\nFormat: 44100:16:2\nTime: 200\n"
    "duration: 200.500\nArtist: Band\nAlbumArtist: Band\nAlbum: Debut\nTitle: Intro\nTrack: 1\nDate: 2019\n"
    "playlist: foo.m3u\nLast-Modified: 1999-09-09T00:00:00Z\n"
    "directory: b\nLast-Modified: 2020-03-03T00:00:00Z\n"
    "file: b/1.flac\nTitle: Other\nArtist: Solo\nAlbum: Album B\nTrack: 1\n"
    "OK\n"
)


class Handler(socketserver.StreamRequestHandler):
    rg_supported = True
    received = []  # commands received inside command lists
    log = []       # every command, in order, in raw wire form
    rg_mode = "off"

    def w(self, s):
        self.wfile.write(s.encode() if isinstance(s, str) else s)

    def handle(self):
        self.n_idle = 0
        self.in_list = self.list_failed = False
        self.w("OK MPD 0.23.5\n")
        for raw in self.rfile:
            line = raw.decode().strip()
            cmd = line.split(" ")[0]
            plain = line.replace('"', '')
            if cmd not in ("command_list_begin", "command_list_end") and not self.list_failed:
                Handler.log.append(line)
            if cmd == "command_list_begin":
                self.in_list, self.list_failed = True, False
                continue
            if cmd == "command_list_end":
                self.in_list = False
                if not self.list_failed:  # after an ACK the list is already answered
                    self.w("OK\n")
                continue
            if self.in_list:
                if not self.list_failed:
                    Handler.received.append(line)  # raw wire form, quotes and all
                    if "bad" in plain:
                        self.list_failed = True
                        self.w("ACK [50@1] {add} No such directory\n")
                continue
            if cmd == "status":
                self.w("volume: 80\nstate: play\nsong: 0\nsongid: 7\nelapsed: 12.5\nduration: 200.0\nOK\n")
            elif cmd == "currentsong":
                self.w("file: a/1.flac\nTitle: Intro\nArtist: Band\nOK\n")
            elif cmd == "playlistinfo":
                for i, (f, t, a, al, n) in enumerate(SONGS):
                    self.w(f"file: {f}\nTitle: {t}\nArtist: {a}\nAlbum: {al}\nTrack: {n}\nPos: {i}\nId: {i+10}\n")
                self.w("OK\n")
            elif plain.startswith("list albumartist"):
                for a in ["Solo", "Band", "Band"]:
                    self.w(f"AlbumArtist: {a}\n")
                self.w("OK\n")
            elif plain.startswith("find albumartist Band"):
                for f, t, a, al, n in reversed(SONGS[:2]):
                    self.w(f"file: {f}\nTitle: {t}\nArtist: {a}\nAlbum: {al}\nTrack: {n}\n")
                self.w("OK\n")
            elif cmd in ("albumart", "readpicture"):
                parts = plain.split(" ")
                uri, off = parts[1], int(parts[-1])
                if uri.startswith("d/"):  # misbehaving server: promises data, sends none
                    self.w("size: 100\nbinary: 0\n\nOK\n")
                elif cmd == "albumart" and not uri.startswith("a/"):
                    self.w("ACK [50@0] {albumart} No file exists\n")
                elif cmd == "readpicture" and uri.startswith("b/"):
                    chunk = EMBEDDED[off:off + 512]
                    self.w(f"size: {len(EMBEDDED)}\ntype: image/png\nbinary: {len(chunk)}\n")
                    self.w(chunk)
                    self.w("\nOK\n")
                elif cmd == "readpicture":
                    self.w("OK\n")  # file has no embedded picture
                else:
                    chunk = IMG[off:off + 512]
                    self.w(f"size: {len(IMG)}\nbinary: {len(chunk)}\n")
                    self.w(chunk)
                    self.w("\nOK\n")
            elif cmd in ("clear", "play"):
                self.w("OK\n")
            elif cmd == "addid":
                self.w("Id: 42\nOK\n")
            elif cmd == "listallinfo":
                self.w(LISTALLINFO)
            elif cmd == "config":
                self.w('ACK [4@0] {config} you are not on a local connection\n')
            elif cmd == "idle" and "hang" in plain:
                self.rfile.readline()  # block until the client goes away
                return
            elif cmd == "idle":
                self.n_idle += 1
                if self.n_idle > 1:  # nothing more changed: block like a real server
                    self.rfile.readline()
                    return
                self.w("changed: player\nchanged: mixer\nOK\n")
            elif cmd == "password":
                if plain == "password secret":
                    self.w("OK\n")
                else:
                    self.w("ACK [3@0] {password} incorrect password\n")
            elif cmd == "replay_gain_status":
                if Handler.rg_supported:
                    self.w(f"replay_gain_mode: {Handler.rg_mode}\nOK\n")
                else:
                    self.w('ACK [5@0] {replay_gain_status} unknown command "replay_gain_status"\n')
            elif cmd == "replay_gain_mode":
                Handler.rg_mode = plain.split(" ")[1]
                self.w("OK\n")
            elif cmd == "pause":
                self.w("OK\n")
            elif cmd in ("repeat", "random", "single", "consume", "binarylimit"):
                self.w("OK\n")
            else:
                self.w(f'ACK [5@0] {{}} unknown command "{cmd}"\n')


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class TestClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = Server(("127.0.0.1", 0), Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def client(self):
        c = MPDClient("127.0.0.1", self.port)
        c.connect()
        self.addCleanup(c.close)
        return c

    def test_greeting_and_status(self):
        c = self.client()
        self.assertEqual(c.protocol_version, "0.23.5")
        st = c.status()
        self.assertEqual((st["state"], st["volume"], st["songid"]), ("play", "80", "7"))

    def test_addid_returns_the_new_id(self):
        c = self.client()
        start = len(Handler.log)
        self.assertEqual(c.addid('a/b "c".flac'), "42")
        self.assertEqual(Handler.log[start:], ['addid "a/b \\"c\\".flac"'])

    def test_queue_grouping(self):
        q = self.client().queue()
        self.assertEqual(len(q), 3)
        self.assertEqual(q[1]["title"], 'Song "Two"')
        self.assertEqual(q[2]["id"], "12")

    def test_repeat_random_single(self):
        c = self.client()
        start = len(Handler.log)
        c.set_repeat(True)
        c.set_random(False)
        c.set_single(True)
        self.assertEqual(Handler.log[start:], ['repeat "1"', 'random "0"', 'single "1"'])

    def test_play_options(self):
        c = self.client()
        start = len(Handler.log)
        c.set_consume(True)
        c.set_play_option("random", True)
        c.set_play_option("single", False)
        self.assertEqual(Handler.log[start:], ['consume "1"', 'random "1"', 'single "0"'])
        with self.assertRaises(ValueError):
            c.set_play_option("crossfade", True)

    def test_binary_limit(self):
        c = self.client()
        self.assertTrue(c.set_binary_limit(1048576))
        self.assertEqual(Handler.log[-1], 'binarylimit "1048576"')

    def test_binary_limit_unknown_to_old_servers(self):
        from tests import lazy_mock
        srv, port = lazy_mock.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        c = MPDClient("127.0.0.1", port)
        c.connect()
        self.addCleanup(c.close)
        self.assertFalse(c.set_binary_limit(1048576))  # ACK: covers just come in 8 KiB pieces
        self.assertEqual(c.status()["state"], "stop")  # and the connection is still fine

    def test_artists_deduped_and_sorted(self):
        self.assertEqual(self.client().list_artists(), ["Band", "Solo"])

    def test_tracks_sorted_by_album_then_track(self):
        t = self.client().tracks_for_artist("Band")
        self.assertEqual([s["track"] for s in t], ["1", "2"])

    def test_albumart_chunked_binary(self):
        self.assertEqual(self.client().albumart("a/1.flac"), IMG)

    def test_cover_prefers_folder_art(self):
        self.assertEqual(self.client().cover("a/1.flac"), IMG)

    def test_cover_falls_back_to_embedded_picture(self):
        self.assertEqual(self.client().cover("b/1.flac"), EMBEDDED)

    def test_cover_none_when_nothing_available(self):
        self.assertIsNone(self.client().cover("c/none.flac"))

    def test_zero_length_chunk_does_not_loop_forever(self):
        c = self.client()
        result = []
        t = threading.Thread(target=lambda: result.append(c.albumart("d/x.flac")), daemon=True)
        t.start()
        t.join(3)
        self.assertFalse(t.is_alive(), "albumart looped forever")
        self.assertEqual(result, [None])

    def test_connection_usable_after_cover_miss(self):
        c = self.client()
        c.cover("c/none.flac")
        self.assertEqual(c.status()["state"], "play")

    def test_ack_raises(self):
        with self.assertRaises(CommandError):
            self.client().command("bogus")

    def test_config_refused_returns_none(self):
        self.assertIsNone(self.client().music_directory())

    def test_idle(self):
        self.assertEqual(self.client().idle("player", "mixer"), ["player", "mixer"])

    def test_quote_escaping(self):
        self.assertEqual(quote('a"b\\c'), '"a\\"b\\\\c"')

    def test_group_songs_ignores_leading_noise(self):
        self.assertEqual(group_songs([("Foo", "x"), ("file", "f"), ("Title", "T")]),
                         [{"file": "f", "title": "T"}])


class TestSettingsSupport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = Server(("127.0.0.1", 0), Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        Handler.rg_supported, Handler.rg_mode = True, "off"

    def client(self, password=None):
        c = MPDClient("127.0.0.1", self.port, password)
        c.connect()
        self.addCleanup(c.close)
        return c

    # -- replay gain
    def test_replay_gain_read_and_set(self):
        c = self.client()
        self.assertEqual(c.replay_gain_status(), "off")
        c.set_replay_gain_mode("album")
        self.assertEqual(c.replay_gain_status(), "album")

    def test_replay_gain_unsupported_server_returns_none(self):
        Handler.rg_supported = False
        self.assertIsNone(self.client().replay_gain_status())

    def test_replay_gain_rejects_invalid_mode_without_sending(self):
        c = self.client()
        with self.assertRaises(ValueError):
            c.set_replay_gain_mode("loud")
        self.assertEqual(c.status()["state"], "play")  # connection still in sync

    # -- passwords
    def test_correct_password(self):
        self.assertEqual(self.client("secret").status()["state"], "play")

    def test_wrong_password_raises_code_3(self):
        c = MPDClient("127.0.0.1", self.port, "nope")
        self.addCleanup(c.close)
        with self.assertRaises(CommandError) as cm:
            c.connect()
        self.assertEqual(ack_code(cm.exception), 3)
        self.assertFalse(c.connected)  # not left half-connected

    # -- probe
    def test_probe_success(self):
        info = probe("127.0.0.1", self.port)
        self.assertEqual(info["version"], "0.23.5")
        self.assertIsNone(info["music_directory"])  # mock refuses `config`
        self.assertEqual(info["replaygain"], "off")

    def test_probe_wrong_password(self):
        with self.assertRaises(CommandError) as cm:
            probe("127.0.0.1", self.port, "nope")
        self.assertIn("Wrong password", describe_error(cm.exception))

    def test_probe_connection_refused(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
        s.close()  # nothing listens here now
        with self.assertRaises(ConnectionRefusedError) as cm:
            probe("127.0.0.1", free, timeout=2)
        self.assertIn("Connection refused at 127.0.0.1:%d" % free,
                      describe_error(cm.exception, "127.0.0.1", free))

    def test_probe_not_an_mpd_server(self):
        srv = socketserver.ThreadingTCPServer(
            ("127.0.0.1", 0), type("H", (socketserver.StreamRequestHandler,),
                                   {"handle": lambda self: self.wfile.write(b"SSH-2.0-OpenSSH\n")}))
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        with self.assertRaises(ProtocolError) as cm:
            probe("127.0.0.1", srv.server_address[1], timeout=2)
        self.assertIn("does not look like an MPD server", describe_error(cm.exception))

    # -- abort (used to stop the idle thread when the connection changes)
    def test_abort_wakes_a_blocked_idle(self):
        c = self.client()
        result = []

        def run():
            try:
                c.idle("hang")
                result.append("returned")
            except (OSError, ProtocolError) as e:
                result.append(type(e).__name__)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(0.3)
        self.assertTrue(t.is_alive(), "idle should be blocking")
        c.abort()
        t.join(3)
        self.assertFalse(t.is_alive(), "abort did not wake idle")
        self.assertEqual(result, ["ProtocolError"])

    def test_abort_on_unconnected_client_is_harmless(self):
        MPDClient("127.0.0.1", 1).abort()


class TestListingAndCommandLists(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = Server(("127.0.0.1", 0), Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        Handler.received.clear()

    def client(self):
        c = MPDClient("127.0.0.1", self.port)
        c.connect()
        self.addCleanup(c.close)
        return c

    # -- iter_songs
    def test_iter_songs_skips_directories_and_playlists(self):
        songs = list(self.client().iter_songs("listallinfo"))
        self.assertEqual([s["file"] for s in songs], ["a/1.flac", "b/1.flac"])

    def test_directory_and_playlist_fields_do_not_leak_into_songs(self):
        first = list(self.client().iter_songs("listallinfo"))[0]
        # the song's own date, not the directory's (before it) or the playlist's (after it)
        self.assertEqual(first["last-modified"], "2021-02-02T00:00:00Z")

    def test_keep_filters_keys(self):
        songs = list(self.client().iter_songs("listallinfo", keep={"title", "artist"}))
        self.assertEqual(set(songs[0]), {"file", "title", "artist"})
        self.assertEqual(songs[0]["title"], "Intro")

    def test_connection_in_sync_after_full_listing(self):
        c = self.client()
        list(c.iter_songs("listallinfo"))
        self.assertEqual(c.status()["state"], "play")

    def test_group_songs_does_not_leak_either(self):
        pairs = [("file", "a"), ("Title", "T"), ("playlist", "p.m3u"), ("Last-Modified", "x")]
        self.assertEqual(group_songs(pairs), [{"file": "a", "title": "T"}])

    # -- command lists
    def test_add_many_sends_one_list_with_all_commands(self):
        c = self.client()
        c.add_many(["a/1.flac", "a/2.flac", "b/1.flac"])
        self.assertEqual(Handler.received, ['add "a/1.flac"', 'add "a/2.flac"', 'add "b/1.flac"'])
        self.assertEqual(c.status()["state"], "play")

    def test_add_many_chunks_large_batches(self):
        c = self.client()
        c.add_many([f"x/{i}.flac" for i in range(7)], chunk=3)
        self.assertEqual(len(Handler.received), 7)

    def test_replace_queue_order(self):
        c = self.client()
        Handler.log.clear()
        c.replace_queue(["x.flac", "y.flac"])
        self.assertEqual(Handler.log, ["clear", 'add "x.flac"', 'add "y.flac"', "play"])

    def test_replace_queue_without_play(self):
        c = self.client()
        Handler.log.clear()
        c.replace_queue(["x.flac"], play=False)
        self.assertEqual(Handler.log, ["clear", 'add "x.flac"'])

    def test_replace_queue_stops_before_play_on_failure(self):
        c = self.client()
        Handler.log.clear()
        with self.assertRaises(CommandError):
            c.replace_queue(["bad/x.flac"])
        self.assertNotIn("play", Handler.log)

    def test_add_many_empty_sends_nothing(self):
        c = self.client()
        c.add_many([])
        self.assertEqual(Handler.received, [])

    def test_failure_in_list_raises_and_connection_stays_in_sync(self):
        c = self.client()
        with self.assertRaises(CommandError):
            c.add_many(["a/1.flac", "bad/x.flac", "a/2.flac"])
        self.assertEqual(Handler.received, ['add "a/1.flac"', 'add "bad/x.flac"'])  # stopped at the failure
        self.assertEqual(c.status()["state"], "play")  # no stray reply left on the wire

    def test_quoting_inside_command_list(self):
        c = self.client()
        c.command_list([("add", 'a "quoted" song.flac')])
        self.assertEqual(Handler.received, ['add "a \\"quoted\\" song.flac"'])  # quotes escaped on the wire

    def test_command_list_requires_connection(self):
        with self.assertRaises(ProtocolError):
            MPDClient("127.0.0.1", 1).command_list([("add", "x")])


class TestDescribeError(unittest.TestCase):
    def test_ack_codes(self):
        self.assertIn("Wrong password", describe_error(CommandError("ACK [3@0] {password} incorrect password")))
        self.assertIn("Permission denied", describe_error(CommandError("ACK [4@0] {status} you don't have permission")))

    def test_other_ack_shows_message_only(self):
        self.assertEqual(describe_error(CommandError('ACK [50@0] {albumart} No file exists')), "No file exists")

    def test_ack_code_parsing(self):
        self.assertEqual(ack_code(CommandError("ACK [50@2] {x} y")), 50)
        self.assertIsNone(ack_code(CommandError("garbage")))

    def test_network_errors(self):
        self.assertIn("Timed out", describe_error(socket.timeout(), "h", 1))
        self.assertIn("unknown host", describe_error(socket.gaierror(-2, "x"), "nohost", 6600))
        self.assertIn("Socket not found", describe_error(FileNotFoundError()))
        self.assertIn("No permission", describe_error(PermissionError()))
        self.assertEqual(describe_error(OSError("[Errno 113] No route to host")), "[Errno 113] No route to host")

    def test_connection_closed_message_passes_through(self):
        self.assertEqual(describe_error(ProtocolError("Connection closed by server")), "Connection closed by server")


if __name__ == "__main__":
    unittest.main()
