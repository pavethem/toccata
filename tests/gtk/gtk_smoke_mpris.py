"""GTK smoke test of MPRIS, the tray icon and close-to-tray, against a mock MPD and a fake
tray host. Needs a private bus (it registers on the session bus):

    GDK_BACKEND=broadway BROADWAY_DISPLAY=:7 dbus-run-session -- python3 tests/gtk/gtk_smoke_mpris.py
"""
import argparse, os, sys, tempfile, threading, time
PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="toccata-smoke-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "cfg")
os.environ["XDG_CACHE_HOME"] = os.path.join(_TMP, "cache")
sys.path.insert(0, PROJECT)
from tests import search_mock
from tests.lazy_mock import render

STATE = {"state": "play", "log": []}

class H(search_mock.Handler):
    def respond(self, a):
        cmd = a[0]
        STATE["log"].append(" ".join(a))
        if cmd == "idle": return ""
        if cmd == "noidle": return "OK\n"
        if cmd == "status":
            return (f"state: {STATE['state']}\nsongid: 5\nelapsed: 12.0\nduration: 200.0\nvolume: 50\n"
                    "repeat: 0\nrandom: 0\nsingle: 0\nOK\n")
        if cmd == "currentsong":
            return render({"file": "rock/a1.flac", "Id": "5", "Title": "Alpha", "Artist": "Rocker",
                           "Album": "First", "duration": "200.0"}) + "OK\n"
        if cmd == "pause":
            STATE["state"] = "pause" if STATE["state"] == "play" else "play"
            return "OK\n"
        if cmd in ("playlistinfo", "replay_gain_status", "outputs", "next", "previous", "stop", "setvol"):
            return "OK\n"
        if cmd == "list" and len(a) == 2: return "OK\n"
        return super().respond(a)

def main():
    search_mock.start()
    srv = search_mock.Server(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    import gi
    gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
    from gi.repository import GLib, Gtk, Gio
    from toccata import app as appmod, mpris, tray
    # a fake tray host on this (private) bus
    host = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    registered = []
    info = Gio.DBusNodeInfo.new_for_xml("""<node><interface name="org.kde.StatusNotifierWatcher">
      <method name="RegisterStatusNotifierItem"><arg name="s" type="s" direction="in"/></method></interface></node>""").interfaces[0]
    def on_call(_c, sender, _p, _i, _m, params, inv):
        registered.append(params.unpack()[0]); inv.return_value(None)
    host.register_object(tray.WATCHER_PATH, info, on_call, None, None)
    Gio.bus_own_name_on_connection(host, tray.WATCHER_NAME, Gio.BusNameOwnerFlags.NONE, None, None)
    client = Gio.DBusConnection.new_for_address_sync(
        Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None),
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION)
    appmod.APP_ID = "io.github.toccata.smoke"
    application = appmod.App(argparse.Namespace(host="127.0.0.1", port=srv.server_address[1], password=None,
                                                music_dir=None, no_covers=True, no_highlight=False))
    class R(list):
        def append(self, x):
            print("STEP", *x, sep=" | ", flush=True); super().append(x)
    report = R()
    pending = {}
    def call(dest, path, iface, method, args=None):
        key = object(); pending[key] = None
        def done(c, r):
            try: pending[key] = c.call_finish(r).unpack()
            except GLib.Error as e: pending[key] = e
        client.call(dest, path, iface, method, args, None, Gio.DBusCallFlags.NONE, 3000, None, done)
        return key
    def steps():
        win = application.props.active_window
        yield 1500
        report.append(("tray registered", registered, win.tray.registered))
        k = call(mpris.BUS_NAME, mpris.OBJECT_PATH, "org.freedesktop.DBus.Properties", "GetAll", GLib.Variant("(s)", (mpris.PLAYER_IFACE,)))
        yield 300
        props = pending[k][0]
        report.append(("MPRIS", props["PlaybackStatus"], props["Metadata"].get("xesam:title"), props["Metadata"].get("xesam:artist"),
                       "pos", props["Position"], "vol", props["Volume"]))
        k = call(mpris.BUS_NAME, mpris.OBJECT_PATH, mpris.PLAYER_IFACE, "PlayPause")
        yield 300
        win.refresh_status()  # (the mock sends no idle events)
        yield 200
        k = call(mpris.BUS_NAME, mpris.OBJECT_PATH, "org.freedesktop.DBus.Properties", "Get", GLib.Variant("(ss)", (mpris.PLAYER_IFACE, "PlaybackStatus")))
        yield 300
        report.append(("after PlayPause via D-Bus", pending[k][0], "MPD got", [l for l in STATE["log"] if l.startswith("pause")]))
        svc = win.tray.service
        k = call(svc, tray.MENU_PATH, tray.MENU_IFACE, "GetLayout", GLib.Variant("(iias)", (0, -1, [])))
        yield 300
        labels = [c[1].get("label") for c in pending[k][1][2]]
        report.append(("tray menu", labels))
        k = call(svc, tray.ITEM_PATH, "org.freedesktop.DBus.Properties", "Get", GLib.Variant("(ss)", (tray.ITEM_IFACE, "ToolTip")))
        yield 300
        report.append(("tray tooltip", pending[k][0][2:]))
        call(svc, tray.ITEM_PATH, tray.ITEM_IFACE, "Activate", GLib.Variant("(ii)", (0, 0)))
        yield 400
        report.append(("tray click -> window visible", win.get_visible()))
        call(svc, tray.ITEM_PATH, tray.ITEM_IFACE, "Activate", GLib.Variant("(ii)", (0, 0)))
        yield 400
        report.append(("tray click again -> visible", win.get_visible()))
        win.close()
        yield 400
        report.append(("close button -> hidden to tray, app alive", win.get_visible(), win in application.get_windows()))
        application.activate()  # e.g. starting the app again
        yield 400
        report.append(("start again -> shown", win.get_visible()))
        call(svc, tray.MENU_PATH, tray.MENU_IFACE, "Event", GLib.Variant("(isvu)", (tray.QUIT, "clicked", GLib.Variant("s", ""), 0)))
        yield 800
        report.append(("tray Quit -> windows left", len(application.get_windows())))
        print("art dir", os.path.isdir(os.path.join(_TMP, "cache", "toccata")))
        for line in report: print(*line, sep=" | ")
        print("settings saved:", os.path.exists(os.path.join(_TMP, "cfg", "toccata", "settings.json")), flush=True)
    gen = None
    def tick():
        try: GLib.timeout_add(next(gen), tick)
        except StopIteration: pass
        except Exception:
            import traceback; traceback.print_exc(); application.quit()
        return False
    def act(_a):
        nonlocal gen
        if gen is None:
            gen = steps(); GLib.timeout_add(10, tick)
    application.connect("activate", act)
    rc = application.run([sys.argv[0]])
    print("app exited with", rc)

main()
