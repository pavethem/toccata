"""GTK smoke test: the play option buttons (repeat, single, shuffle, consume).
Run like gtk_smoke.py (broadway + dbus-run-session)."""
import argparse, os, sys, tempfile, threading
PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="toccata-smoke-")
OUT = os.path.join(_TMP, "shots"); os.makedirs(OUT)
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "cfg")
os.environ["XDG_CACHE_HOME"] = os.path.join(_TMP, "cache")
sys.path.insert(0, PROJECT)
from tests import search_mock

OPTIONS = {"repeat": "0", "single": "0", "random": "0", "consume": "0"}
SENT = []


class H(search_mock.Handler):
    def respond(self, a):
        cmd = a[0]
        if cmd == "idle":
            return ""
        if cmd in ("noidle", "currentsong", "replay_gain_status", "outputs", "playlistinfo"):
            return "OK\n"
        if cmd == "status":
            return "state: stop\nvolume: 50\n" + "".join(f"{k}: {v}\n" for k, v in OPTIONS.items()) + "OK\n"
        if cmd in OPTIONS:
            SENT.append((cmd, a[1]))
            OPTIONS[cmd] = a[1]
            return "OK\n"
        return super().respond(a)


def main():
    search_mock.start()
    srv = search_mock.Server(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    import gi
    gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
    from gi.repository import GLib, Gtk
    from toccata import app as appmod
    appmod.APP_ID = "io.github.toccata.smoke"
    application = appmod.App(argparse.Namespace(host="127.0.0.1", port=srv.server_address[1], password=None,
                                                music_dir=None, no_covers=True, no_highlight=False))
    report = []

    def shot(win, name):
        p = Gtk.WidgetPaintable.new(win); snap = Gtk.Snapshot(); p.snapshot(snap, win.get_width(), win.get_height())
        node = snap.to_node()
        if node is not None:
            win.get_native().get_renderer().render_texture(node, None).save_to_png(f"{OUT}/{name}.png")

    def steps():
        win = application.props.active_window
        yield 1500
        b = win.option_buttons
        report.append(("buttons", list(b), "all off", not any(x.get_active() for x in b.values()),
                       "consume icon found", Gtk.IconTheme.get_for_display(win.get_display()).has_icon(
                           "io.github.toccata-consume-symbolic")))
        b["repeat"].set_active(True)
        b["consume"].set_active(True)
        yield 300
        report.append(("clicked repeat + consume -> MPD got", list(SENT)))
        win.refresh_status()  # (MPD would send an "options" idle event)
        yield 200
        report.append(("after refresh", {k: x.get_active() for k, x in b.items()}))
        SENT.clear()
        OPTIONS.update(random="1", single="oneshot", repeat="0")  # another client changed them
        win.refresh_status()
        yield 300
        report.append(("changed elsewhere", {k: x.get_active() for k, x in b.items()}, "sent back to MPD", list(SENT)))
        yield 2000
        shot(win, "options")
        for line in report:
            print(*line, sep=" | ")
        print("screenshots in", OUT, flush=True)
        application.quit()

    gen = None

    def tick():
        nonlocal gen
        try:
            GLib.timeout_add(next(gen), tick)
        except StopIteration:
            pass
        except Exception:
            import traceback
            traceback.print_exc()
            application.quit()
        return False

    def act(_a):
        nonlocal gen
        if gen is None:
            gen = steps()
            GLib.timeout_add(10, tick)
    application.connect("activate", act)
    application.run([sys.argv[0]])


main()
