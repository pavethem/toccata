"""GTK smoke test: queue filter (Ctrl+Shift+F). Run like gtk_smoke.py (broadway + dbus-run-session)."""
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
        yield 1500
        def titles():
            items = [win.queue_sel.get_item(i) for i in range(win.queue_sel.get_n_items())]
            return [s.data.get("title") for s in items if isinstance(s, appmod.Song)]  # not album header rows
        report.append(("queue", titles()))
        win.activate_action("win.queue-filter", None)  # Ctrl+Shift+F
        yield 300
        report.append(("bar open", win.qfilter_bar.get_search_mode(), "toggle", win.qfilter_toggle.get_active(),
                       "focus in entry", win.get_focus() is not None and win.get_focus().is_ancestor(win.qfilter_entry)))
        win.qfilter_entry.set_text("rocker")
        yield 600
        report.append(("'rocker'", titles(), win.qfilter_count.get_text()))
        win.qfilter_entry.set_text("many 3")
        yield 600
        report.append(("'many 3'", titles(), win.qfilter_count.get_text()))
        win.qfilter_entry.set_text("m")
        yield 600
        report.append(("'m' (too short: no filter)", len(titles()), repr(win.qfilter_count.get_text())))
        # grouped and collapsed: a match inside a collapsed album is still shown
        win.activate_action("win.queue-grouped", None)
        yield 500
        mass = win._queue_groups[2]
        win.toggle_group(mass)
        yield 300
        report.append(("grouped, 'Mass' collapsed", len(titles())))
        win.qfilter_entry.set_text("many 4")
        yield 600
        report.append(("filter 'many 4' with album collapsed", titles(), win.qfilter_count.get_text()))
        win.select_group(win._queue_groups[2])
        yield 200
        report.append(("click album header while filtering selects", [s.data.get("title") for s in win._queue_selected()]))
        shot(win, "qfilter")
        win.qfilter_entry.emit("stop-search")  # Escape
        yield 600
        report.append(("Escape", "bar open", win.qfilter_bar.get_search_mode(), "entry", repr(win.qfilter_entry.get_text()),
                       "rows", len(titles()), "collapsed again", mass.head_id in win.collapsed_groups))
        for line in report: print(*line, sep=" | ")
        print("screenshots in", OUT, flush=True)
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
