"""GTK smoke test of the queue grouped by album (see gtk_smoke.py for how to run).
Checks grouping, collapse/expand, selection of a collapsed album, dragging an
album, and that the first album header stays visible at the top (GTK anchors on
the first row; see Window._pin_queue_top).

Based on gtk_smoke.py: drives the real window against an in-process mock MPD and
prints what happened, plus screenshots (dialogs do not render in them).

Not part of `unittest discover` (needs a display). Run headless with:

    gtk4-broadwayd :7 &
    GDK_BACKEND=broadway BROADWAY_DISPLAY=:7 python3 tests/gtk/gtk_smoke.py

Uses a temporary config dir and its own application id, so it neither touches
your settings nor talks to a running toccata."""
import argparse
import os
import sys

import tempfile

PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="toccata-smoke-")
OUT = os.path.join(_TMP, "shots")
os.makedirs(OUT, exist_ok=True)
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "cfg")
sys.path.insert(0, PROJECT)

from tests import search_mock  # noqa: E402
from tests.lazy_mock import render  # noqa: E402


class H(search_mock.Handler):
    def respond(self, a):
        cmd, rest = a[0], a[1:]
        if cmd == "idle":
            return ""  # never answer: nothing happens on the mock
        if cmd == "noidle":
            return "OK\n"
        if cmd in ("currentsong", "replay_gain_status", "outputs", "clear", "play"):
            if cmd == "clear":
                search_mock.Handler.queue[:] = []
            return "OK\n"
        if cmd == "playlistinfo":
            by = {s["file"]: s for s in self.db}
            return "".join(render(dict(by.get(u, {"file": u}), Id=str(i + 1), Pos=str(i)))
                           for i, u in enumerate(search_mock.Handler.queue)) + "OK\n"
        if cmd == "add":
            search_mock.Handler.queue.append(rest[0])
            return "OK\n"
        if cmd == "list" and rest and rest[0].lower() == "albumartist" and len(rest) == 1:
            vals = sorted({s.get("AlbumArtist") or s.get("Artist") for s in self.db})
            return "".join(f"AlbumArtist: {v}\n" for v in vals) + "OK\n"
        if cmd == "find" and rest and rest[0].lower() == "albumartist":
            songs = [s for s in self.db if (s.get("AlbumArtist") or s.get("Artist")) == rest[1]]
            if len(rest) >= 4 and rest[2] == "window":
                lo, hi = rest[3].split(":")
                songs = songs[int(lo):int(hi)]
            return "".join(render(s) for s in songs) + "OK\n"
        return super().respond(a)


def main():
    search_mock.start()  # resets the shared state on search_mock.Handler
    search_mock.Handler.playlists.update({
        "Mix": ["rock/a1.flac", "jazz/b1.flac", "rock/a1.flac", "gone.flac"],
        "Evening": ["jazz/b1.flac"],
    })
    search_mock.Handler.queue[:] = ["rock/a1.flac", "rock/a2.flac", "jazz/b1.flac"] + \
        [f"many/{i:03d}.flac" for i in range(5)] + ["rock/a1.flac"]
    srv = search_mock.Server(("127.0.0.1", 0), H)
    import threading
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    port = srv.server_address[1]

    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import GLib, Gtk
    from toccata import app as appmod
    appmod.APP_ID = "io.github.toccata.smoke"
    args = argparse.Namespace(host="127.0.0.1", port=port, password=None, music_dir=None,
                              no_covers=True, no_highlight=False)
    application = appmod.App(args)
    report = []

    def shot(win, name):
        w, h = win.get_width(), win.get_height()
        paintable = Gtk.WidgetPaintable.new(win)
        snap = Gtk.Snapshot()
        paintable.snapshot(snap, w, h)
        node = snap.to_node()
        if node is None:
            report.append(("screenshot", name, "empty (not drawn yet)"))
            return
        tex = win.get_native().get_renderer().render_texture(node, None)
        tex.save_to_png(f"{OUT}/{name}.png")

    def steps():
        win = application.props.active_window
        hl = search_mock.Handler
        yield 1500
        adj = win.queue_view.get_vadjustment()
        trace = [("before", adj.get_value(), adj.get_upper())]
        import time as _t
        t0 = _t.monotonic()
        adj.connect("value-changed", lambda a: trace.append(("value-changed", round(_t.monotonic() - t0, 3), a.get_value())))
        adj.connect("changed", lambda a: trace.append(("changed", round(_t.monotonic() - t0, 3), a.get_value(), a.get_upper())))
        win.activate_action("win.queue-grouped", None)  # toggle on
        trace.append(("right after", adj.get_value(), adj.get_upper()))
        yield 800
        report.append(("trace", trace))
        report.append(("grouped", win.settings.queue_grouped, "groups",
                       [(g.title, g.n, g.summary()) for g in win._queue_groups]))
        sw = win.queue_view.get_parent()
        report.append(("scroll", type(sw).__name__, sw.get_vadjustment().get_value(), "view adj", win.queue_view.get_vadjustment().get_value(), sw.get_vadjustment() is win.queue_view.get_vadjustment(), "headers bound", [getattr(g, "header", None) is not None for g in win._queue_groups]))
        report.append(("first rows", [s.texts[0] for s in win._queue_songs[:3]]))
        shot(win, "g1-grouped")
        win.toggle_group(win._queue_groups[2])  # collapse "Mass - Lots"
        yield 600
        report.append(("scroll after collapse", win.queue_view.get_vadjustment().get_value()))
        win.refresh_queue()
        yield 600
        report.append(("scroll after queue refresh", win.queue_view.get_vadjustment().get_value()))
        win.toggle_group(win._queue_groups[2])  # open again: more to scroll
        yield 1500
        adj2 = win.queue_view.get_vadjustment()
        adj2.set_value(100)  # the user scrolls down
        yield 600
        report.append(("user scrolled to 100 ->", adj2.get_value()))
        win.refresh_queue()
        yield 1500
        report.append(("after refresh while scrolled ->", adj2.get_value()))
        adj2.set_value(0)  # user scrolls back up
        yield 300
        win.refresh_queue()
        yield 1500
        report.append(("back at top + refresh ->", adj2.get_value()))
        win.toggle_group(win._queue_groups[2])
        yield 300
        report.append(("visible rows after collapse", win.queue_sel.get_n_items(), "of", len(win._queue_songs)))
        shot(win, "g2-collapsed")
        win.select_group(win._queue_groups[2])
        yield 200
        report.append(("selecting collapsed album ->", len(win._queue_selected()), "tracks"))
        win.select_group(win._queue_groups[0])
        yield 200
        report.append(("selecting open album ->", [s.data["file"] for s in win._queue_selected()]))
        report.append(("drag album 0 ids", win.drag_group(win._queue_groups[0]) is not None, win._drag))
        win.collapse_all(True)
        yield 400
        report.append(("collapse all -> visible", win.queue_sel.get_n_items()))
        win.collapse_all(False)
        yield 400
        report.append(("expand all -> visible", win.queue_sel.get_n_items()))
        win.activate_action("win.queue-grouped", None)  # off again
        yield 600
        report.append(("ungrouped: visible", win.queue_sel.get_n_items(), "texts", win._queue_songs[0].texts,
                       "setting", win.settings.queue_grouped))
        shot(win, "g3-ungrouped")
        for line in report:
            print(*line, sep=" | ")
        print("screenshots in", OUT)
        application.quit()

    gen = None

    def tick():
        nonlocal gen
        try:
            delay = next(gen)
        except StopIteration:
            return False
        except Exception:
            import traceback
            traceback.print_exc()
            application.quit()
            return False
        GLib.timeout_add(delay, tick)
        return False

    def on_activate(_app):
        nonlocal gen
        if gen is None:
            gen = steps()
            GLib.timeout_add(10, tick)
    application.connect("activate", on_activate)
    application.run([sys.argv[0]])


if __name__ == "__main__":
    main()
