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
        shot(win, "1-library")
        report.append(("categories", [c.label for c in win._search_cats]))
        # --- search
        report.append(("category before first search", win._current_search_category().key, win.search_cat.get_selected(), len(win._search_cats)))
        win.focus_search()
        win.search_entry.set_text("alpha")
        win.search_entry.emit("activate")
        yield 800
        report.append(("search any 'alpha'", [s.data["file"] for s in win.search_store], win.search_stats.get_text()))
        shot(win, "2-search")
        keys = [c.key for c in win._search_cats]
        win.search_cat.set_selected(keys.index("composer"))  # re-runs the search
        yield 800
        report.append(("composer 'alpha'", [s.data["file"] for s in win.search_store], win.search_stats.get_text()))
        win.search_entry.set_text("bach")
        win.search_entry.emit("activate")
        yield 800
        report.append(("composer 'bach'", [s.data["file"] for s in win.search_store]))
        win.search_cat.set_selected(keys.index("modified-since"))
        win.search_entry.set_text("2025/05/01")
        win.search_entry.emit("activate")
        yield 800
        report.append(("modified since 2025/05/01", sorted(s.data["file"] for s in win.search_store)))
        report.append(("help", win.search_help.get_text()))
        win.search_cat.set_selected(keys.index("artist"))
        win.search_entry.set_text("mass")
        win.search_entry.emit("activate")
        yield 800
        win.search_sel.select_range(0, 3, True)
        yield 200
        report.append(("buttons with 3 selected", win.s_add.get_label(), win.s_play.get_label()))
        before = len(hl.queue)
        win.s_add.emit("clicked")
        yield 600
        report.append(("queue grew by", len(hl.queue) - before, "queue store", win.queue_store.get_n_items()))
        shot(win, "3-search-selected")
        first = win._search_targets()[0]
        win.activate_action("win.search-show-artists", None)
        yield 1000
        report.append(("show in artists -> view", win.views.get_visible_child_name(),
                       "artist", win.art_sel.get_selected_item().get_string(),
                       "album selected", [r.album.title for r in win._sel_albums], "result's album", first.get("album")))
        win.views.set_visible_child_name("search")
        yield 300
        win.activate_action("win.search-show-albums", None)  # (this mock has no Albums view; see gtk_smoke_qmenu)
        yield 500
        report.append(("show in albums -> view", win.views.get_visible_child_name()))
        # --- playlists
        win.views.set_visible_child_name("playlists")
        yield 600
        report.append(("playlists", [win.pl_names.get_string(i) for i in range(win.pl_names.get_n_items())],
                       "shown", win._pl_name, win.pl_info.get_text()))
        win.pl_sel.set_selected(1)  # "Mix"
        yield 400
        report.append(("Mix rows", win.pl_store.get_n_items(), win.pl_info.get_text()))
        shot(win, "4-playlists")
        win.activate_action("win.pl-invalid", None)
        yield 500
        report.append(("after remove invalid", list(hl.playlists["Mix"])))
        win.activate_action("win.pl-dedupe", None)
        yield 500
        report.append(("after dedupe", list(hl.playlists["Mix"]), "rows", win.pl_store.get_n_items()))
        first_track = next(i for i in range(win.pl_songs_sel.get_n_items())  # skip album header rows
                           if not isinstance(win.pl_songs_sel.get_item(i), appmod.QueueAlbum))
        win.pl_songs_sel.select_item(first_track, True)
        yield 200
        win.p_remove.emit("clicked")
        yield 500
        report.append(("after remove row 0", list(hl.playlists["Mix"])))
        q0 = list(hl.queue)
        win.p_add.emit("clicked")
        yield 500
        report.append(("load Mix appended", hl.queue[len(q0):]))
        # dialogs: drive them like a user would
        def find(widget, cls):
            if isinstance(widget, cls):
                return widget
            child = widget.get_first_child()
            while child is not None:
                hit = find(child, cls)
                if hit is not None:
                    return hit
                child = child.get_next_sibling()
            return None
        win.save_queue_as_playlist()
        yield 400
        dlg = win.get_visible_dialog()
        entry = find(dlg, Gtk.Entry)
        listing = find(dlg, Gtk.ListBox)
        def offered():
            out, row = [], listing.get_first_child()
            while row is not None:
                out.append(row.name)
                row = row.get_next_sibling()
            return out
        report.append(("save: suggestions", offered()))
        entry.set_text("mi")
        report.append(("save: typed 'mi'", offered()))
        listing.emit("row-activated", listing.get_first_child())
        hint = [w for w in (listing.get_parent().get_parent().get_prev_sibling(),) if w is not None]
        report.append(("save: picked", entry.get_text(), "allowed", dlg.get_response_enabled("ok"),
                       "hint", find(dlg, Gtk.Label) is not None and [l.get_text() for l in [dlg.get_extra_child().get_first_child().get_next_sibling()]]))
        dlg.emit("response", "ok")
        yield 500
        confirm = win.get_visible_dialog()
        report.append(("save: existing name asks first", confirm.get_heading() if confirm else None))
        if confirm is not None:
            confirm.emit("response", "cancel")
            confirm.close()
        dlg.close()
        yield 400
        win.save_queue_as_playlist()
        yield 400
        dlg = win.get_visible_dialog()
        entry = find(dlg, Gtk.Entry)
        entry.set_text("bad/name")
        report.append(("save: 'bad/name' allowed?", dlg.get_response_enabled("ok")))
        entry.set_text("Night Out")
        report.append(("save: 'Night Out' allowed?", dlg.get_response_enabled("ok")))
        yield 300
        shot(dlg, "5-save-dialog")
        dlg.emit("response", "ok")
        dlg.close()
        yield 600
        report.append(("saved playlists", sorted(hl.playlists), "Night Out =", hl.playlists.get("Night Out")))
        win.add_to_playlist([{"file": "rock/a2.flac"}, {"file": "jazz/b1.flac"}])
        yield 400
        dlg = win.get_visible_dialog()
        drop = find(dlg, Gtk.DropDown)
        report.append(("add: preselected", drop.get_selected_item().get_string()))
        shot(dlg, "6-add-dialog")
        dlg.emit("response", "ok")
        dlg.close()
        yield 600
        report.append(("Night Out after add", hl.playlists.get("Night Out")))
        # rename "Night Out" onto the existing "Evening": must ask before overwriting
        win._pl_name = "Night Out"
        win.activate_action("win.pl-rename", None)
        yield 400
        dlg = win.get_visible_dialog()
        find(dlg, Gtk.Entry).set_text("Evening")
        dlg.emit("response", "ok")
        dlg.close()
        yield 500
        confirm = win.get_visible_dialog()
        report.append(("overwrite question", confirm.get_heading() if confirm else None))
        if confirm:
            shot(confirm, "7-overwrite")
            confirm.emit("response", "confirm")
            confirm.close()
        yield 600
        report.append(("after rename", sorted(hl.playlists), "Evening =", hl.playlists.get("Evening"),
                       "list", [win.pl_names.get_string(i) for i in range(win.pl_names.get_n_items())],
                       "shown", win._pl_name))
        # category switch with unfitting text: no toast, just the hint
        win.views.set_visible_child_name("search")
        win.search_entry.set_text("bach")
        keys = [c.key for c in win._search_cats]
        win.search_cat.set_selected(keys.index("modified-since"))
        yield 300
        report.append(("switch to Modified with 'bach'", win.search_stack.get_visible_child_name(),
                       win.search_status.get_title()))
        yield 300
        shot(win, "8-final")
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
