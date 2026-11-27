"""GTK smoke test: undo / redo of queue changes (Ctrl+Z, Ctrl+Shift+Z, Ctrl+Y).
Run like gtk_smoke.py (broadway + dbus-run-session) or run_headless_mutter.sh."""
import argparse, os, sys, tempfile, threading
PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="toccata-smoke-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "cfg")
os.environ["XDG_CACHE_HOME"] = os.path.join(_TMP, "cache")
sys.path.insert(0, PROJECT)
from tests import search_mock
from tests.lazy_mock import render

QUEUE = []  # [(songid, uri)]: ids stay stable, like MPD's
NEXT = [100]
PLAYING = [None]
LOG = []  # queue commands MPD received


class H(search_mock.Handler):
    def respond(self, a):
        cmd = a[0]
        if cmd in ("deleteid", "moveid", "add", "addid", "clear"):
            LOG.append(tuple(a))
        if cmd == "idle":
            return ""
        if cmd in ("noidle", "currentsong", "replay_gain_status", "outputs"):
            return "OK\n"
        if cmd == "status":
            if PLAYING[0] is None:
                return "state: stop\nOK\n"
            pos = [i for i, _ in QUEUE].index(PLAYING[0])
            return f"state: play\nsong: {pos}\nsongid: {PLAYING[0]}\nOK\n"
        if cmd == "playlistinfo":
            by = {s["file"]: s for s in self.db}
            return "".join(render(dict(by.get(u, {"file": u}), Id=str(i), Pos=str(p)))
                           for p, (i, u) in enumerate(QUEUE)) + "OK\n"
        if cmd == "deleteid":
            QUEUE[:] = [(i, u) for i, u in QUEUE if str(i) != a[1]]
            return "OK\n"
        if cmd == "moveid":
            entry = next(e for e in QUEUE if str(e[0]) == a[1])
            QUEUE.remove(entry)
            QUEUE.insert(int(a[2]), entry)
            return "OK\n"
        if cmd in ("add", "addid"):
            NEXT[0] += 1
            QUEUE.insert(int(a[2]) if len(a) > 2 else len(QUEUE), (NEXT[0], a[1]))
            return (f"Id: {NEXT[0]}\n" if cmd == "addid" else "") + "OK\n"
        if cmd == "clear":
            QUEUE.clear()
            PLAYING[0] = None
            return "OK\n"
        return super().respond(a)


def main():
    search_mock.start()
    uris = ["rock/a1.flac", "rock/a2.flac", "jazz/b1.flac", "x/1.flac", "x/2.flac", "x/3.flac"]
    QUEUE[:] = [(10 + n, u) for n, u in enumerate(uris)]
    PLAYING[0] = 12  # jazz/b1.flac plays
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
    C, S = Gdk.ModifierType.CONTROL_MASK, Gdk.ModifierType.SHIFT_MASK

    def steps():
        win = application.props.active_window
        yield 1500
        def uris_now():
            return [u for _, u in QUEUE]
        def key(keyval, mods):
            handled = win._on_library_key(None, keyval, 0, Gdk.ModifierType(mods))
            win.refresh_queue(); win.refresh_status()  # (the mock sends no idle events)
            return handled
        def enabled():
            return (win.lookup_action("queue-undo").get_enabled(), win.lookup_action("queue-redo").get_enabled())
        def check(name, cond, *info):
            report.append((("OK " if cond else "FAIL ") + name, *info))
        check("start: undo/redo off", enabled() == (False, False), enabled())
        win.queue_view.grab_focus()
        # remove two tracks (one of them before the playing one)
        win.remove_songs([s for s in win._queue_songs if s.uri in ("rock/a2.flac", "x/2.flac")])
        win.refresh_queue(); yield 300
        check("removed", uris_now() == ["rock/a1.flac", "jazz/b1.flac", "x/1.flac", "x/3.flac"], uris_now())
        check("undo on", enabled() == (True, False), enabled())
        del LOG[:]
        check("Ctrl+Z handled", key(Gdk.KEY_z, C)); yield 300
        check("undo restores", uris_now() == uris, uris_now())
        check("playing entry kept its id", (12, "jazz/b1.flac") in QUEUE, list(QUEUE))
        check("no clear, only adds", all(c[0] == "add" for c in LOG), list(LOG))
        check("redo on", enabled() == (False, True), enabled())
        check("Ctrl+Shift+Z handled", key(Gdk.KEY_Z, C | S)); yield 300
        check("redo removes again", uris_now() == ["rock/a1.flac", "jazz/b1.flac", "x/1.flac", "x/3.flac"], uris_now())
        check("Ctrl+Y = nothing left to redo", key(Gdk.KEY_y, C) and uris_now()[1] == "jazz/b1.flac")
        key(Gdk.KEY_z, C); yield 300
        # a drag inside the queue: the last track to the top
        last = win._queue_songs[-1]
        win._drag = ("queue", [last.data["id"]])
        win._on_queue_drop(None, None, 10, 1)
        win.refresh_queue(); yield 300
        check("dragged to the top", uris_now()[0] == "x/3.flac", uris_now())
        check("a new change drops redo", enabled() == (True, False), enabled())
        del LOG[:]
        key(Gdk.KEY_z, C); yield 300
        check("undo of the move", uris_now() == uris, uris_now())
        check("one entry moved back", [c[1] for c in LOG if c[0] == "moveid"] and len({c[1] for c in LOG}) == 1, list(LOG))
        # Clear queue
        win.change_queue(win.client.clear); win.refresh_queue(); win.refresh_status(); yield 300
        check("cleared", QUEUE == [])
        key(Gdk.KEY_z, C); yield 300
        check("undo of Clear", uris_now() == uris, uris_now())
        # adding tracks
        win.add_tracks([{"file": "new/1.flac"}, {"file": "new/2.flac"}])
        win.refresh_queue(); yield 300
        key(Gdk.KEY_z, C); yield 300
        check("undo of Add", uris_now() == uris, uris_now())
        # text fields keep their own Ctrl+Z
        win.open_queue_filter(); yield 300
        focus = win.get_focus()
        check("focus in the filter entry", isinstance(focus, appmod.Gtk.Editable), type(focus).__name__)
        check("Ctrl+Z in an entry is not taken", not key(Gdk.KEY_z, C))
        win.qfilter_bar.set_search_mode(False) if hasattr(win, "qfilter_bar") else None
        win.queue_view.grab_focus(); yield 200
        # the actions (no buttons any more)
        win.remove_songs(win._queue_songs[:1]); win.refresh_queue(); yield 300
        win.activate_action("win.queue-undo", None); win.refresh_queue(); yield 300
        check("Undo action", uris_now() == uris, uris_now())
        win.activate_action("win.queue-redo", None); win.refresh_queue(); yield 300
        check("Redo action", uris_now() == uris[1:], uris_now())
        # limit
        for n in range(15):
            win.add_tracks([{"file": f"lim/{n}.flac"}]); win.refresh_queue()
        yield 300
        n = 0
        while win.lookup_action("queue-undo").get_enabled():
            key(Gdk.KEY_z, C); n += 1
            if n > 30:
                break
        yield 300
        check("10 undo steps", n == 10, n, len(QUEUE))
        for line in report:
            print(*line, sep=" | ")
        print("FAILED" if any(l[0].startswith("FAIL") for l in report) else "ALL OK")
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
