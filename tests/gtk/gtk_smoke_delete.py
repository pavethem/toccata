"""GTK smoke test: the Delete key removes the selected queue tracks. Run like
gtk_smoke.py (broadway + dbus-run-session)."""
import argparse, os, sys, tempfile, threading
PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="toccata-smoke-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "cfg")
os.environ["XDG_CACHE_HOME"] = os.path.join(_TMP, "cache")
sys.path.insert(0, PROJECT)
from tests import search_mock
from tests.lazy_mock import render

QUEUE = []  # [(songid, uri)]: ids stay stable, like MPD's


class H(search_mock.Handler):
    def respond(self, a):
        cmd = a[0]
        if cmd == "idle":
            return ""
        if cmd in ("noidle", "currentsong", "replay_gain_status", "outputs"):
            return "OK\n"
        if cmd == "playlistinfo":
            by = {s["file"]: s for s in self.db}
            return "".join(render(dict(by.get(u, {"file": u}), Id=str(i), Pos=str(p)))
                           for p, (i, u) in enumerate(QUEUE)) + "OK\n"
        if cmd == "deleteid":
            QUEUE[:] = [(i, u) for i, u in QUEUE if str(i) != a[1]]
            return "OK\n"
        return super().respond(a)


def main():
    search_mock.start()
    uris = ["rock/a1.flac", "rock/a2.flac", "jazz/b1.flac"] + [f"many/{i:03d}.flac" for i in range(5)]
    QUEUE[:] = [(10 + n, u) for n, u in enumerate(uris)]
    srv = search_mock.Server(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    import gi
    gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
    from gi.repository import Gdk, GLib
    from toccata import app as appmod
    appmod.APP_ID = "io.github.toccata.smoke"
    application = appmod.App(argparse.Namespace(host="127.0.0.1", port=srv.server_address[1], password=None,
                                                music_dir=None, no_covers=True, no_highlight=False))
    report = []

    def steps():
        win = application.props.active_window
        yield 1500
        def titles():
            return [win.queue_sel.get_item(i).data.get("title") for i in range(win.queue_sel.get_n_items())]
        def selected():
            return [s.data.get("title") for s in win._queue_selected()]
        def delete(mods=0):
            return win._on_queue_key(None, Gdk.KEY_Delete, 0, Gdk.ModifierType(mods))
        report.append(("queue", titles()))
        win.queue_sel.select_item(1, True)  # Beta
        win.queue_view.grab_focus()
        yield 200
        report.append(("Delete handled", delete()))
        win.refresh_queue()  # (the mock sends no idle events; MPD would)
        yield 400
        report.append(("after Delete", titles(), "now selected", selected()))
        delete(); win.refresh_queue(); yield 400
        report.append(("Delete again", titles(), "now selected", selected()))
        win.queue_sel.select_range(0, 2, True)  # two at once
        delete(); win.refresh_queue(); yield 400
        report.append(("two selected + Delete", titles(), "MPD ids left", [i for i, _ in QUEUE]))
        report.append(("Ctrl+Delete ignored", delete(Gdk.ModifierType.CONTROL_MASK)))
        win.queue_sel.unselect_all()
        report.append(("nothing selected: not handled", delete()))
        # a collapsed album counts as all its tracks
        win.activate_action("win.queue-grouped", None)
        yield 400
        win.toggle_group(win._queue_groups[0])
        yield 300
        win.select_group(win._queue_groups[0])
        delete(); win.refresh_queue(); yield 400
        report.append(("collapsed album + Delete", titles(), "MPD queue", [u for _, u in QUEUE]))
        for line in report:
            print(*line, sep=" | ")
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
