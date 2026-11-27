import time
import unittest

from gi.repository import Gio, GLib

from toccata import mpris


SONG = {"file": "A/X/03.flac", "id": "17", "title": "Song", "artist": "Art", "albumartist": "AA",
        "album": "X", "track": "3/12", "disc": "1/2", "duration": "200.5", "genre": "Jazz"}


class TestValues(unittest.TestCase):
    def test_status_and_loop(self):
        self.assertEqual(mpris.playback_status({"state": "play"}), "Playing")
        self.assertEqual(mpris.playback_status({"state": "pause"}), "Paused")
        self.assertEqual(mpris.playback_status({"state": "stop"}), "Stopped")
        self.assertEqual(mpris.playback_status(None), "Stopped")
        self.assertEqual(mpris.loop_status({"repeat": "1", "single": "0"}), "Playlist")
        self.assertEqual(mpris.loop_status({"repeat": "1", "single": "1"}), "Track")
        self.assertEqual(mpris.loop_status({"repeat": "0", "single": "1"}), "None")

    def test_metadata(self):
        md = mpris.metadata(SONG, "file:///c.png")
        self.assertEqual(md["mpris:trackid"].unpack(), "/io/github/toccata/track/17")
        self.assertEqual(md["xesam:title"].unpack(), "Song")
        self.assertEqual(md["xesam:artist"].unpack(), ["Art"])
        self.assertEqual(md["xesam:albumArtist"].unpack(), ["AA"])
        self.assertEqual(md["xesam:trackNumber"].unpack(), 3)
        self.assertEqual(md["xesam:discNumber"].unpack(), 1)
        self.assertEqual(md["mpris:length"].unpack(), 200_500_000)
        self.assertEqual(md["mpris:artUrl"].unpack(), "file:///c.png")

    def test_metadata_without_song_and_without_tags(self):
        self.assertEqual(list(mpris.metadata({})), ["mpris:trackid"])
        self.assertEqual(mpris.metadata({})["mpris:trackid"].unpack(), mpris.NO_TRACK)
        md = mpris.metadata({"file": "dir/untagged.mp3", "id": "4"})
        self.assertEqual(md["xesam:title"].unpack(), "untagged.mp3")
        self.assertNotIn("mpris:length", md)

    def test_volume_and_seekable(self):
        self.assertEqual(mpris.volume({"volume": "55"}), 0.55)
        self.assertEqual(mpris.volume({"volume": "-1"}), 0.0)  # no mixer
        p = mpris.player_properties({"state": "play"}, {"file": "http://radio", "id": "1"})
        self.assertFalse(p["CanSeek"].unpack())  # streams cannot seek
        self.assertTrue(mpris.player_properties({"state": "play"}, SONG)["CanSeek"].unpack())

    def test_changed_properties(self):
        a = mpris.player_properties({"state": "play"}, SONG)
        b = mpris.player_properties({"state": "pause"}, SONG)
        self.assertEqual(list(mpris.changed_properties(a, b)), ["PlaybackStatus"])
        self.assertEqual(mpris.changed_properties(a, a), {})


class FakePlayer:
    def __init__(self):
        self._status = {"state": "play", "volume": "40", "repeat": "0", "random": "0", "single": "0"}
        self._song = dict(SONG)
        self.calls = []
        self.pos = 10.0

    def status(self): return self._status
    def song(self): return self._song
    def position(self): return self.pos
    def art_url(self): return ""

    def __getattr__(self, name):  # play, next, seek_to, set_volume, ... are recorded
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *a: self.calls.append((name,) + a)


class TestOnTheBus(unittest.TestCase):
    """A real (private) session bus, an independent client, like GNOME Shell is one."""

    @classmethod
    def setUpClass(cls):
        cls.bus = Gio.TestDBus.new(Gio.TestDBusFlags.NONE)
        cls.bus.up()

    @classmethod
    def tearDownClass(cls):
        cls.bus.down()

    def setUp(self):
        self.player = FakePlayer()
        self.server_conn = Gio.DBusConnection.new_for_address_sync(
            self.bus.get_bus_address(),
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION)
        self.server = mpris.MprisServer(self.player, connection=self.server_conn)
        self.server.start()
        self.addCleanup(self.server_conn.close_sync, None)
        self.addCleanup(self.server.stop)
        self.client = Gio.DBusConnection.new_for_address_sync(
            self.bus.get_bus_address(),
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION)
        self.addCleanup(self.client.close_sync, None)
        self.spin(lambda: self.server.name_acquired)
        self.server.update()

    def spin(self, until, timeout=3):
        ctx = GLib.MainContext.default()
        end = time.monotonic() + timeout
        while not until() and time.monotonic() < end:
            ctx.iteration(False)
            time.sleep(0.005)
        return until()

    def call(self, iface, method, args=None, sig=None):
        result = []

        def done(conn, res):
            try:
                result.append(conn.call_finish(res))
            except GLib.Error as e:
                result.append(e)
        self.client.call(mpris.BUS_NAME, mpris.OBJECT_PATH, iface, method, args, None,
                         Gio.DBusCallFlags.NONE, 2000, None, done)
        self.assertTrue(self.spin(lambda: result))
        if isinstance(result[0], GLib.Error):
            raise result[0]
        return result[0]

    def get(self, prop, iface=mpris.PLAYER_IFACE):
        return self.call("org.freedesktop.DBus.Properties", "Get",
                         GLib.Variant("(ss)", (iface, prop))).unpack()[0]

    def set(self, prop, value):
        self.call("org.freedesktop.DBus.Properties", "Set",
                  GLib.Variant("(ssv)", (mpris.PLAYER_IFACE, prop, value)))

    def test_name_and_identity(self):
        self.assertEqual(self.get("Identity", mpris.ROOT_IFACE), "Toccata")
        self.assertEqual(self.get("DesktopEntry", mpris.ROOT_IFACE), "io.github.toccata")

    def test_properties(self):
        self.assertEqual(self.get("PlaybackStatus"), "Playing")
        self.assertEqual(self.get("Metadata")["xesam:title"], "Song")
        self.assertEqual(self.get("Position"), 10_000_000)
        self.assertEqual(self.get("Volume"), 0.4)

    def test_buttons(self):
        for method in ("PlayPause", "Next", "Previous", "Stop", "Play", "Pause"):
            self.call(mpris.PLAYER_IFACE, method)
        self.call(mpris.ROOT_IFACE, "Raise")
        self.assertEqual([c[0] for c in self.player.calls],
                         ["play_pause", "next", "previous", "stop", "play", "pause", "raise_window"])

    def test_seek_relative_and_absolute(self):
        self.call(mpris.PLAYER_IFACE, "Seek", GLib.Variant("(x)", (5_000_000,)))
        self.assertEqual(self.player.calls[-1], ("seek_to", 15.0))
        self.call(mpris.PLAYER_IFACE, "SetPosition",
                  GLib.Variant("(ox)", ("/io/github/toccata/track/17", 60_000_000)))
        self.assertEqual(self.player.calls[-1], ("seek_to", 60.0))
        self.call(mpris.PLAYER_IFACE, "SetPosition",
                  GLib.Variant("(ox)", ("/io/github/toccata/track/99", 1_000_000)))  # stale track: ignored
        self.assertEqual(self.player.calls[-1], ("seek_to", 60.0))
        self.call(mpris.PLAYER_IFACE, "Seek", GLib.Variant("(x)", (500_000_000,)))  # past the end
        self.assertEqual(self.player.calls[-1], ("next",))

    def test_writable_properties(self):
        self.set("Volume", GLib.Variant("d", 0.73))
        self.set("Shuffle", GLib.Variant("b", True))
        self.set("LoopStatus", GLib.Variant("s", "Track"))
        self.assertEqual(self.player.calls, [("set_volume", 73), ("set_random", True),
                                             ("set_repeat", True), ("set_single", True)])

    def test_open_uri_is_refused(self):
        with self.assertRaises(GLib.Error):
            self.call(mpris.PLAYER_IFACE, "OpenUri", GLib.Variant("(s)", ("file:///x",)))

    def test_properties_changed_signal(self):
        got = []
        self.client.signal_subscribe(None, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                                     mpris.OBJECT_PATH, None, Gio.DBusSignalFlags.NONE,
                                     lambda *a: got.append(a[5].unpack()))
        self.player._status = dict(self.player._status, state="pause")
        self.server.update()
        self.assertTrue(self.spin(lambda: got))
        iface, changed, _ = got[0]
        self.assertEqual(iface, mpris.PLAYER_IFACE)
        self.assertEqual(changed, {"PlaybackStatus": "Paused"})
        got.clear()
        self.server.update()  # nothing new: no signal
        self.spin(lambda: got, timeout=0.3)
        self.assertEqual(got, [])


if __name__ == "__main__":
    unittest.main()
