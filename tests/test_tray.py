import os
import time
import unittest

from gi.repository import Gio, GLib

from toccata import tray

ICON_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "icons")

WATCHER_XML = """
<node><interface name="org.kde.StatusNotifierWatcher">
  <method name="RegisterStatusNotifierItem"><arg name="service" type="s" direction="in"/></method>
</interface></node>"""


BUS = None


def setUpModule():
    # Up before anything else: rendering the SVG (librsvg/GIO) may open the session
    # bus, and a session bus opened before TestDBus.up() makes down() wait 30 s for it.
    global BUS
    BUS = Gio.TestDBus.new(Gio.TestDBusFlags.NONE)
    BUS.up()


def tearDownModule():
    BUS.down()


def connect(bus):
    return Gio.DBusConnection.new_for_address_sync(
        bus.get_bus_address(),
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION)


def spin(until, timeout=3):
    ctx = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while not until() and time.monotonic() < end:
        ctx.iteration(False)
        time.sleep(0.005)
    return until()


class TestMenuModel(unittest.TestCase):
    def test_labels_follow_state(self):
        items = dict(tray.menu_items({"title": "Song – Art", "playing": True, "window_visible": True}))
        self.assertEqual(items[tray.NOW]["label"], "Song – Art")
        self.assertFalse(items[tray.NOW]["enabled"])
        self.assertEqual(items[tray.PLAY_PAUSE]["label"], "Pause")
        self.assertEqual(items[tray.SHOW]["label"], "Hide Window")
        items = dict(tray.menu_items({"playing": False, "window_visible": False}))
        self.assertEqual(items[tray.NOW]["label"], "Nothing playing")
        self.assertEqual(items[tray.PLAY_PAUSE]["label"], "Play")
        self.assertEqual(items[tray.SHOW]["label"], "Show Window")

    def test_long_title_and_artist_are_shortened_on_two_lines(self):
        long_title = ('Bukduchilseong (Gyemyeon Pyeongnong), "La Grande Ourse = The Plough" '
                      '(The Big Dipper)')
        items = tray.menu_items({"title": long_title, "artist": "Kim Wol-ha", "playing": True})
        ids = [i for i, _ in items]
        self.assertEqual(ids[:3], [tray.NOW, tray.ARTIST, tray.SEP1])
        labels = dict(items)
        self.assertLessEqual(len(labels[tray.NOW]["label"]), tray.MAX_LABEL)
        self.assertTrue(labels[tray.NOW]["label"].endswith("\u2026"))
        self.assertEqual(labels[tray.ARTIST]["label"], "Kim Wol-ha")
        self.assertFalse(labels[tray.ARTIST]["enabled"])
        self.assertNotIn(tray.ARTIST, [i for i, _ in tray.menu_items({"title": "Only a title"})])

    def test_shorten(self):
        self.assertEqual(tray.shorten("short"), "short")
        self.assertEqual(tray.shorten("  spaced   out  "), "spaced out")
        cut = tray.shorten("one two three four five six seven eight nine ten", 16)
        self.assertLessEqual(len(cut), 16)
        self.assertEqual(cut, "one two three\u2026")  # at a word boundary, not "one two three f…"
        self.assertEqual(tray.shorten("abcdefghijklmnopqrstuvwxyz", 10), "abcdefghi\u2026")  # no space: hard cut

    def test_underscores_are_not_access_keys(self):
        self.assertEqual(dict(tray.menu_items({"title": "My_Song"}))[tray.NOW]["label"], "My__Song")

    def test_layout_variant_shape(self):
        v = tray.layout_variant(tray.menu_items({}), 7)
        self.assertEqual(v.get_type_string(), "(u(ia{sv}av))")
        revision, (root_id, props, children) = v.unpack()
        self.assertEqual((revision, root_id, props["children-display"]), (7, 0, "submenu"))
        self.assertEqual(len(children), len(tray.menu_items({})))

    def test_pixmaps_from_the_real_icon(self):
        pix = tray.icon_pixmaps(os.path.join(ICON_DIR, "io.github.toccata-panel.svg"))
        self.assertEqual([(w, h) for w, h, _ in pix], [(22, 22), (44, 44)])
        w, h, data = pix[0]
        self.assertEqual(len(data), w * h * 4)
        self.assertTrue(any(data[i] for i in range(0, len(data), 4)))  # some non-transparent pixels
        # ARGB, alpha first (the SNI spec): opaque pixels of the #dfdfdf icon read ff df df df
        opaque = [data[i:i + 4] for i in range(0, len(data), 4) if data[i] == 255]
        self.assertTrue(opaque)
        self.assertTrue(all(p[1:] == b"\xdf\xdf\xdf" for p in opaque))


class TestOnTheBus(unittest.TestCase):
    def setUp(self):
        self.bus = BUS
        # a fake tray host (what GNOME's AppIndicator extension provides)
        self.host = connect(self.bus)
        self.registered = []

        def on_call(_c, sender, _p, _i, method, params, invocation):
            self.registered.append((sender, params.unpack()[0]))
            invocation.return_value(None)
        info = Gio.DBusNodeInfo.new_for_xml(WATCHER_XML).interfaces[0]
        self.host_reg = self.host.register_object(tray.WATCHER_PATH, info, on_call, None, None)
        self.host_name = Gio.bus_own_name_on_connection(self.host, tray.WATCHER_NAME, Gio.BusNameOwnerFlags.NONE,
                                                        None, None)
        self.calls = []
        self.state = {"title": "Song", "playing": False, "window_visible": True}
        cb = {name: (lambda name=name, *a: self.calls.append((name,) + a))
              for name in ("toggle_window", "play_pause", "previous", "next", "stop", "quit")}
        cb["volume_step"] = lambda d: self.calls.append(("volume_step", d))
        cb["state"] = lambda: self.state
        self.tray = tray.Tray(cb, ICON_DIR, connection=connect(self.bus))
        self.tray.start()
        self.addCleanup(self.cleanup)
        self.assertTrue(spin(lambda: self.tray.registered))

    def cleanup(self):
        self.tray.stop()
        Gio.bus_unown_name(self.host_name)
        self.host.unregister_object(self.host_reg)
        for conn in (self.tray._conn, self.host):
            conn.close_sync(None)  # else the test bus waits 30 s for them on shutdown

    def call(self, path, iface, method, args=None):
        result = []

        def done(c, r):
            try:
                result.append(c.call_finish(r))
            except GLib.Error as e:
                result.append(e)
        self.host.call(self.tray.service, path, iface, method, args, None, Gio.DBusCallFlags.NONE, 2000, None, done)
        self.assertTrue(spin(lambda: result))
        if isinstance(result[0], GLib.Error):
            raise result[0]
        return result[0].unpack() if result[0] is not None else None

    def get(self, prop):
        return self.call(tray.ITEM_PATH, "org.freedesktop.DBus.Properties", "Get",
                         GLib.Variant("(ss)", (tray.ITEM_IFACE, prop)))[0]

    def test_registers_with_the_watcher(self):
        self.assertEqual(self.registered[0][1], self.tray.service)

    def test_item_properties(self):
        self.assertEqual(self.get("IconName"), "io.github.toccata-panel")
        self.assertEqual(self.get("IconThemePath"), ICON_DIR)
        self.assertEqual(self.get("Menu"), tray.MENU_PATH)
        self.assertEqual(len(self.get("IconPixmap")), 2)
        self.tray.set_tooltip("Toccata", "Song – Art")
        self.assertEqual(self.get("ToolTip")[2:], ("Toccata", "Song – Art"))

    def test_clicks(self):
        self.call(tray.ITEM_PATH, tray.ITEM_IFACE, "Activate", GLib.Variant("(ii)", (0, 0)))
        self.call(tray.ITEM_PATH, tray.ITEM_IFACE, "SecondaryActivate", GLib.Variant("(ii)", (0, 0)))
        self.call(tray.ITEM_PATH, tray.ITEM_IFACE, "Scroll", GLib.Variant("(is)", (-120, "vertical")))
        self.call(tray.ITEM_PATH, tray.ITEM_IFACE, "Scroll", GLib.Variant("(is)", (120, "vertical")))
        self.call(tray.ITEM_PATH, tray.ITEM_IFACE, "Scroll", GLib.Variant("(is)", (120, "horizontal")))
        self.assertEqual(self.calls, [("toggle_window",), ("play_pause",), ("volume_step", 1), ("volume_step", -1)])

    def test_menu_layout_and_click(self):
        revision, (root, _props, children) = self.call(
            tray.MENU_PATH, tray.MENU_IFACE, "GetLayout", GLib.Variant("(iias)", (0, -1, [])))
        labels = [c[1].get("label") for c in children]
        self.assertEqual(labels[0], "Song")
        self.assertIn("Play", labels)
        self.call(tray.MENU_PATH, tray.MENU_IFACE, "Event",
                  GLib.Variant("(isvu)", (tray.NEXT, "clicked", GLib.Variant("s", ""), 0)))
        self.assertTrue(spin(lambda: self.calls))
        self.assertEqual(self.calls, [("next",)])

    def test_menu_updates_when_state_changes(self):
        got = []
        self.host.signal_subscribe(None, tray.MENU_IFACE, "LayoutUpdated", tray.MENU_PATH, None,
                                   Gio.DBusSignalFlags.NONE, lambda *a: got.append(a[5].unpack()))
        self.state = dict(self.state, playing=True)
        self.tray.refresh()
        self.assertTrue(spin(lambda: got))
        _rev, (_r, _p, children) = self.call(tray.MENU_PATH, tray.MENU_IFACE, "GetLayout",
                                             GLib.Variant("(iias)", (0, -1, [])))
        self.assertIn("Pause", [c[1].get("label") for c in children])
        got.clear()
        self.tray.refresh()  # nothing changed: no signal
        spin(lambda: got, timeout=0.3)
        self.assertEqual(got, [])


if __name__ == "__main__":
    unittest.main()
