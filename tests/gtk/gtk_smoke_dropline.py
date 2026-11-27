"""GTK smoke test: the drop marker line of the queue (like Cantata's) sits on the
row edge where a drag would land, and agrees with the index the drop uses.
Run like gtk_smoke.py (or tests/gtk/run_headless_mutter.sh); exits 1 on failure."""
import argparse, os, sys, tempfile, threading
PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="toccata-smoke-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "cfg")
os.environ["XDG_CACHE_HOME"] = os.path.join(_TMP, "cache")
sys.path.insert(0, PROJECT)
from tests import search_mock
from tests.lazy_mock import render

QUEUE = []  # [(songid, uri)]


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
    uris = ["rock/a1.flac", "rock/a2.flac", "jazz/b1.flac"] + [f"many/{i:03d}.flac" for i in range(5)] + ["rock/a1.flac"]
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

    class G:
        def __init__(self, mods=0):
            self.mods = mods

    def shot(win, name):  # the window as rendered, into the temp dir
        from gi.repository import Gtk
        paintable = Gtk.WidgetPaintable.new(win)
        snap = Gtk.Snapshot()
        paintable.snapshot(snap, win.get_width(), win.get_height())
        node = snap.to_node()
        if node is not None:
            path = os.path.join(_TMP, name + ".png")
            win.get_native().get_renderer().render_texture(node, None).save_to_png(path)
            print("screenshot", path, flush=True)

    class T:  # stands in for the Gtk.DropTarget passed to the motion handler
        def get_current_drop(self):
            return None

    def steps():
        win = application.props.active_window
        win.set_size_request(1000, 900)
        yield 1500
        line, view = win._queue_drop_line, win.queue_view

        def motion(x, y):
            win._on_queue_drag_motion(T(), x, y)

        def line_mid():  # centre of the marker in queue list coordinates
            ok, b = line.compute_bounds(view)
            return round(b.get_y() + b.get_height() / 2) if ok and line.get_visible() else None

        def edges(w):
            return tuple(round(v) for v in win._row_edges(w))

        def song_row(i):
            return win._queue_songs[i].row

        # ungrouped: upper half of row 3 -> before it, on its top edge
        top, bottom = edges(song_row(3))
        x = 200
        motion(x, top + 5)
        yield 200
        check("upper half: index", win._queue_insert_index(x, top + 5), 3)
        check("upper half: line on the row's top edge", line_mid(), top)
        motion(x, bottom - 5)
        yield 200
        check("lower half: index", win._queue_insert_index(x, bottom - 5), 4)
        check("lower half: line on the row's bottom edge", line_mid(), bottom)
        shot(win, "dropline")
        check("bottom edge of row 3 = top edge of row 4", bottom, edges(song_row(4))[0])
        # sweeping down pixel by pixel: index and line only ever move down (a pointer on
        # the border between two rows once sent the line to the bottom of the queue)
        back, prev = [], (0, 0)
        for yy in range(0, view.get_height()):
            at, ly = win._queue_drop_target(x, yy)
            if ly is not None and (at, round(ly)) < prev:
                back.append((yy, prev, (at, round(ly))))
            if ly is not None:
                prev = (at, round(ly))
        check("sweep down: line and index never jump back", back[:3], [])
        check("on the border between rows 3 and 4: index 4", win._queue_insert_index(x, bottom), 4)
        n = len(win._queue_songs)
        last_bottom = edges(song_row(n - 1))[1]
        motion(x, last_bottom + 40)
        yield 200
        check("below the rows: index = end", win._queue_insert_index(x, last_bottom + 40), n)
        check("below the rows: line under the last row", line_mid(), last_bottom)
        motion(x, top + 5)
        yield 200
        win._on_queue_drop(None, "", x, top + 5)  # no drag in progress: nothing happens
        check("drop hides the line", line.get_visible(), False)
        motion(x, top + 5)
        yield 100
        line.set_visible(False)  # what the "leave" handler does

        # grouped: a header means before its album; a collapsed album's lower half after it
        win.activate_action("win.queue-grouped", None)
        yield 600
        mass = win._queue_groups[2]
        win.toggle_group(mass)
        yield 500
        mass = win._queue_groups[2]
        head = next(w for w in _walk(view, appmod.QueueHeader) if w.group is not None and w.group.start == mass.start)
        htop, hbottom = edges(head)
        motion(x, hbottom - 5)
        yield 200
        check("header: index = start of its album", win._queue_insert_index(x, hbottom - 5), mass.start)
        check("header: line on the header's top edge", line_mid(), htop)
        summary = win._queue_songs[mass.start].row
        stop, sbottom = edges(summary)
        motion(x, sbottom - 5)
        yield 200
        check("collapsed album, lower half: index = after the album", win._queue_insert_index(x, sbottom - 5), mass.end)
        check("collapsed album, lower half: line under its row", line_mid(), sbottom)
        motion(x, stop + 5)
        yield 200
        check("collapsed album, upper half: line on its top edge", line_mid(), stop)
        print("FAILED" if failures else "ALL OK", flush=True)
        application.quit()

    def _walk(w, cls):
        c = w.get_first_child()
        while c is not None:
            if isinstance(c, cls) and c.get_mapped():
                yield c
            else:
                yield from _walk(c, cls)
            c = c.get_next_sibling()
    gen = None

    def tick():
        try:
            GLib.timeout_add(next(gen), tick)
        except StopIteration:
            pass
        except Exception:
            import traceback
            traceback.print_exc(); failures.append("exception"); application.quit()
        return False

    def act(_a):
        nonlocal gen
        if gen is None:
            gen = steps(); GLib.timeout_add(10, tick)
    application.connect("activate", act)
    application.run([sys.argv[0]])
    sys.stdout.flush()
    os._exit(1 if failures else 0)


main()
