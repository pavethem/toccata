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
        from gi.repository import Gdk
        win = application.props.active_window
        hl = search_mock.Handler
        hl.playlists.clear()
        hl.playlists.update({"Evening": ["jazz/b1.flac"], "Mix": ["rock/a1.flac", "jazz/b1.flac"],
                             "Morning Jazz": ["jazz/b1.flac", "many/003.flac", "rock/a2.flac", "special/alphabet soup.flac"],
                             "Party": ["many/001.flac"]})
        yield 1500
        win.views.set_visible_child_name("playlists")
        yield 600
        def key(ch=None, keyval=None):
            kv = keyval if keyval is not None else Gdk.unicode_to_keyval(ord(ch))
            return win._on_library_key(None, kv, 0, Gdk.ModifierType(0))
        def current():
            item = win.pl_sel.get_selected_item()
            return item.get_string() if item else None
        win.pl_view.grab_focus()
        yield 200
        report.append(("playlists", [win.pl_names.get_string(i) for i in range(win.pl_names.get_n_items())], "shown", current()))
        key("m"); key("o")
        yield 400
        report.append(("typed 'mo'", current(), "tracks shown", win.pl_store.get_n_items(), "label", win.pl_typeahead_label.get_text(),
                       win.pl_typeahead_label.get_visible()))
        yield 1300
        key("m"); yield 100; a = current(); key("m"); yield 100; b = current()
        report.append(("m, m steps", a, b))
        yield 1300
        key("z")
        report.append(("'z'", current(), "error", win.pl_typeahead_label.has_css_class("error")))
        yield 1300
        win.pl_sel.set_selected([win.pl_names.get_string(i) for i in range(win.pl_names.get_n_items())].index("Morning Jazz"))
        yield 400
        win.pl_songs_view.grab_focus()
        yield 200
        report.append(("focus in track list", win.get_focus() is not None and win.get_focus().is_ancestor(win.pl_songs_scroll)))
        for ch in "soup":
            key(ch)
        yield 500
        bits = win.pl_songs_sel.get_selection()
        sel = [win.pl_store.get_item(bits.get_nth(i)).data.get("title") for i in range(bits.get_size())]
        report.append(("tracks typed 'soup'", sel, "label", win.plsong_typeahead_label.get_text(), "playlist still", current()))
        yield 1300
        for ch in "rocker":  # a later word: the artist
            key(ch)
        yield 500
        bits = win.pl_songs_sel.get_selection()
        report.append(("tracks typed 'rocker' (artist)", [win.pl_store.get_item(bits.get_nth(i)).data.get("title") for i in range(bits.get_size())]))
        win.views.set_visible_child_name("library")
        yield 300
        win.art_view.grab_focus()
        yield 200
        key("r"); key("o")
        yield 500
        report.append(("library still: typed 'ro'", win.art_sel.get_selected_item().get_string()))
        for line in report: print(*line, sep=" | ")
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
