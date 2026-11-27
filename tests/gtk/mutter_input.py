"""Real pointer / keyboard input for GTK smoke tests under the headless mutter of
tests/gtk/run_headless_mutter.sh, through mutter's RemoteDesktop D-Bus API (on the
private session bus). Coordinates are those of the virtual monitor; a fullscreen
window starts at (0, 0). Calls block: run them off the GTK thread (start())."""
import threading, time

from gi.repository import Gio, GLib

BTN_LEFT = 272  # evdev codes
KEY_LEFTCTRL, KEY_ESC = 29, 1  # evdev key codes
# Note: without keyboard focus (the headless window gets it only on a press) a
# modifier pressed before a click reaches GTK late: GTK sees Ctrl after the press.
# A second (transient) window was active only after a key press, not after a click:
# press and release a harmless key (e.g. 42, left Shift) first (gtk_smoke_coverwin).


class Input:
    def __init__(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        rd = "org.gnome.Mutter.RemoteDesktop"
        self.session = self._call(rd, "/org/gnome/Mutter/RemoteDesktop", rd, "CreateSession").unpack()[0]
        sid = self._call(rd, self.session, "org.freedesktop.DBus.Properties", "Get",
                         GLib.Variant("(ss)", (rd + ".Session", "SessionId"))).unpack()[0]
        sc = "org.gnome.Mutter.ScreenCast"
        cast = self._call(sc, "/org/gnome/Mutter/ScreenCast", sc, "CreateSession",
                          GLib.Variant("(a{sv})", ({"remote-desktop-session-id": GLib.Variant("s", sid)},))).unpack()[0]
        self.stream = self._call(sc, cast, sc + ".Session", "RecordMonitor",
                                 GLib.Variant("(sa{sv})", ("", {}))).unpack()[0]
        self._session("Start")
        time.sleep(0.5)
        self.move(1, 1)  # the first motion only enters the surface; presses before it got lost
        time.sleep(0.2)

    def _call(self, name, path, iface, method, args=None):
        return self.bus.call_sync(name, path, iface, method, args, None, 0, -1, None)

    def _session(self, method, args=None):
        return self._call("org.gnome.Mutter.RemoteDesktop", self.session,
                          "org.gnome.Mutter.RemoteDesktop.Session", method, args)

    def move(self, x, y):
        self._session("NotifyPointerMotionAbsolute", GLib.Variant("(sdd)", (self.stream, float(x), float(y))))
        time.sleep(0.02)

    def button(self, down, code=BTN_LEFT):
        self._session("NotifyPointerButton", GLib.Variant("(ib)", (code, down)))
        time.sleep(0.05)

    def key(self, down, code):
        self._session("NotifyKeyboardKeycode", GLib.Variant("(ub)", (code, down)))
        time.sleep(0.05)

    def drag(self, x0, y0, x1, y1, steps=20, release=True, hold=0.0):
        """Press at (x0, y0), move in steps to (x1, y1), wait `hold` s, release."""
        self.move(x0, y0)
        self.button(True)
        for i in range(1, steps + 1):
            self.move(x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps)
        time.sleep(hold)
        if release:
            self.button(False)


def start(fn):
    """Run fn() on a thread; the GTK side waits with `while t.is_alive(): yield 50`."""
    t = threading.Thread(target=fn, daemon=True)
    t.start()
    return t
