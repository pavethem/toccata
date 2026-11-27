"""GTK smoke test: clicking an album header in the grouped queue moves the keyboard
focus into the queue, so Delete reaches it (user report 2026-11-11: Delete only
worked after a track had been clicked). Run like gtk_smoke.py. Exits 1 on failure."""
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
    failures = []
    def check(name, got, want):
        ok = got == want
        print("ok  " if ok else "FAIL", name, got, "" if ok else f"(wanted {want})", flush=True)
        if not ok:
            failures.append(name)
    class FakeGesture:
        def get_current_event_state(self): return Gdk.ModifierType(0)
    def in_queue(win):
        f = win.get_focus()
        return f is not None and (f is win.queue_view or f.is_ancestor(win.queue_view))
    def steps():
        win = application.props.active_window
        yield 1500
        win.activate_action("win.queue-grouped", None)
        yield 500
        g = win._queue_groups[0]
        check("header shown", g.header is not None, True)
        win.art_view.grab_focus()  # focus somewhere else, as after using the library
        yield 200
        check("focus starts outside the queue", in_queue(win), False)
        g.header._on_click(FakeGesture(), 1, -5, -5)  # (-5,-5): picks nothing, not the toggle
        yield 200
        check("album selected", [s.data.get("title") for s in win._queue_selected()],
              [s.data.get("title") for s in win._queue_songs[g.start:g.end]])
        check("focus in the queue after a header click", in_queue(win), True)
        # the real key event path: deliver Delete to the focused widget's key controllers
        n_before = len(QUEUE)
        handled = win._on_queue_key(None, Gdk.KEY_Delete, 0, Gdk.ModifierType(0))
        win.refresh_queue(); yield 400
        check("Delete removed the album", (handled, n_before - len(QUEUE)), (True, g.end - g.start))
        # collapsed album too
        win.art_view.grab_focus(); yield 100
        g = win._queue_groups[0]
        win.toggle_group(g); yield 300
        g = win._queue_groups[0]
        if g.header is not None:
            g.header._on_click(FakeGesture(), 1, -5, -5); yield 200
            check("collapsed header click: focus in the queue", in_queue(win), True)
        print("FAILED" if failures else "ALL OK", flush=True)
        application.quit()
    gen = None
    def tick():
        try:
            GLib.timeout_add(next(gen), tick)
        except StopIteration:
            pass
        except Exception:
            import traceback; traceback.print_exc(); failures.append("exception"); application.quit()
        return False
    def act(_a):
        nonlocal gen
        if gen is None:
            gen = steps(); GLib.timeout_add(10, tick)
    application.connect("activate", act)
    application.run([sys.argv[0]])
    sys.exit(1 if failures else 0)
main()
