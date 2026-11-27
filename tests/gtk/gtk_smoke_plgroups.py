"""GTK smoke test: album groups in the Playlists view. Run like gtk_smoke.py (broadway + dbus-run-session)."""
"""GTK smoke test: type-ahead in the Playlists view. Run like gtk_smoke.py (broadway + dbus-run-session)."""
"""GTK smoke test: drives the real window against an in-process mock MPD and
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
    search_mock.Handler.queue[:] = ["rock/a2.flac"]
    # a long playlist: 120 albums of 3 tracks (the list's height must not depend on
    # the scroll position; with GTK section headers it did, and the list jumped)
    long_pl = []
    for a in range(120):
        for t in range(3):
            uri = f"long/{a:03d}/{t}.flac"
            search_mock.Handler.db.append({"file": uri, "duration": "100.000", "Title": f"Song {a}.{t}",
                                           "Artist": f"Band {a}", "Album": f"Record {a}", "Track": str(t + 1)})
            long_pl.append(uri)
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
    failed = [False]

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
        from gi.repository import Gdk
        win = application.props.active_window
        hl = search_mock.Handler
        hl.playlists.clear()
        hl.playlists["Mixed"] = ["rock/a1.flac", "rock/a2.flac", "jazz/b1.flac"] + \
            [f"many/{i:03d}.flac" for i in range(5)] + ["rock/a1.flac"]
        hl.playlists["Long"] = long_pl
        yield 1500
        win.views.set_visible_child_name("playlists")
        yield 800
        def rows():  # track rows (album header rows skipped)
            items = [win.pl_songs_sel.get_item(i) for i in range(win.pl_songs_sel.get_n_items())]
            return [(x.texts or (x.title,))[0] for x in items if not isinstance(x, appmod.QueueAlbum)]
        names = [win.pl_names.get_string(i) for i in range(win.pl_names.get_n_items())]
        win.pl_sel.set_selected(names.index("Mixed"))
        yield 500
        headers = [win.pl_songs_sel.get_item(i).group.title for i in range(win.pl_songs_sel.get_n_items())
                   if isinstance(win.pl_songs_sel.get_item(i), appmod.QueueAlbum)]
        report.append(("header rows", headers))
        g = win._plg.groups
        report.append(("grouped by default", win._pl_grouped, "groups", [(x.title, x.n) for x in g]))
        report.append(("rows", rows()))
        yield 2500
        shot(win, "pl-grouped")
        # hover Play / Add on a header: that album only (Mass - Lots, 5 tracks);
        # visibility on hover and real clicks: gtk_smoke_plhover.py
        hdr = g[2].header
        ok = hdr is not None and len(hdr.hover_buttons) == 2
        before = list(hl.queue)
        hdr.hover_buttons[1].emit("clicked")  # Add
        yield 500
        added = hl.queue[len(before):]
        hdr.hover_buttons[0].emit("clicked")  # Play
        yield 500
        played = list(hl.queue)
        want = [f"many/{i:03d}.flac" for i in range(5)]
        ok = ok and added == want and played == want
        report.append(("header hover buttons", len(hdr.hover_buttons),
                       "Add ->", added, "Play -> queue", played, "ok" if ok else "WRONG"))
        failed[0] |= not ok
        nq = len(appmod.QueueHeader(win, as_row=True).hover_buttons)  # the queue's headers: none
        report.append(("queue header buttons", nq))
        failed[0] |= nq != 0
        win._plg.toggle_group(g[2])  # collapse "Mass - Lots"
        yield 400
        report.append(("collapsed Mass", len(rows()), "rows", rows()[3:5]))
        win._plg.select_group(win._plg.groups[2])
        yield 200
        report.append(("select collapsed album -> positions", win._pl_selected_positions(), "button", win.p_remove.get_label()))
        # type-ahead into the collapsed album opens it
        win.pl_songs_view.grab_focus()
        yield 200
        for ch in "many 3":
            win._on_library_key(None, Gdk.unicode_to_keyval(ord(ch)), 0, Gdk.ModifierType(0))
        yield 500
        bits = win.pl_songs_sel.get_selection()
        sel = [win.pl_songs_sel.get_item(bits.get_nth(i)).data.get("title") for i in range(bits.get_size())]
        report.append(("type-ahead 'many 3' into collapsed album", sel, "album open again", win._plg.groups[2].head_id not in win._plg.collapsed_groups))
        # remove a collapsed album = all its tracks
        win._plg.toggle_group(win._plg.groups[2])
        yield 300
        win._plg.select_group(win._plg.groups[2])
        yield 200
        win.p_remove.emit("clicked")
        yield 600
        report.append(("removed the collapsed album", list(hl.playlists["Mixed"])))
        win.activate_action("win.pl-grouped", None)  # off
        yield 500
        report.append(("grouping off", win._pl_grouped, "rows", rows(), "setting", win.settings.playlist_grouped))
        win.activate_action("win.pl-grouped", None)  # on again
        win.pl_sel.set_selected(names.index("Long"))
        yield 800
        adj = win.pl_songs_scroll.get_vadjustment()
        uppers = []
        for frac in (0.0, 0.13, 0.31, 0.5, 0.77, 1.0, 0.42, 0.05):
            adj.set_value(frac * (adj.get_upper() - adj.get_page_size()))
            yield 250
            uppers.append(round(adj.get_upper()))
        ok = len(set(uppers)) == 1
        report.append(("long grouped playlist: list height at 8 scroll positions", uppers,
                       "constant" if ok else "CHANGES (list jumps)"))
        failed[0] |= not ok
        for line in report: print(*line, sep=" | ")
        print("screenshots in", OUT, flush=True)
        if failed[0]:
            print("FAILED", flush=True)
            os._exit(1)
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
