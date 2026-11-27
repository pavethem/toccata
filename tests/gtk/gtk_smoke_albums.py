"""GTK smoke test: album clicks (select / double click opens) and album type-ahead.
Run like gtk_smoke.py (broadway + dbus-run-session)."""
"""GTK smoke test of the library genre filter (run like gtk_smoke.py, under broadway)."""
import argparse, os, sys, tempfile, threading
PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="toccata-smoke-")
OUT = os.path.join(_TMP, "shots"); os.makedirs(OUT)
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "cfg")
os.environ["XDG_CACHE_HOME"] = os.path.join(_TMP, "cache")
sys.path.insert(0, PROJECT)
from tests import lazy_mock
from tests.lazy_mock import LazyHandler

class H(LazyHandler):
    def respond(self, a):
        if a[0] == "idle":
            return ""
        if a[0] in ("noidle", "currentsong", "playlistinfo", "replay_gain_status", "tagtypes", "outputs"):
            return "OK\n"
        return super().respond(a)

def main():
    srv, _ = lazy_mock.start(db=lazy_mock.genre_db())
    srv.shutdown(); srv.server_close()
    srv = lazy_mock.LazyServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    import gi
    gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
    from gi.repository import GLib, Gtk
    from toccata import app as appmod
    appmod.APP_ID = "io.github.toccata.smoke"
    args = argparse.Namespace(host="127.0.0.1", port=srv.server_address[1], password=None, music_dir=None,
                              no_covers=True, no_highlight=False)
    application = appmod.App(args)
    report = []
    def shot(win, name):
        p = Gtk.WidgetPaintable.new(win); snap = Gtk.Snapshot(); p.snapshot(snap, win.get_width(), win.get_height())
        node = snap.to_node()
        if node is None:
            report.append(("screenshot", name, "empty")); return
        win.get_native().get_renderer().render_texture(node, None).save_to_png(f"{OUT}/{name}.png")
    def artists(win):
        return [win.artist_store.get_string(i) for i in range(win.artist_store.get_n_items())]
    def albums(win):
        return [(r.album.title, len(r.album.tracks)) for r in win._album_rows()]
    def steps():
        from gi.repository import Gdk
        win = application.props.active_window
        yield 1500
        names = artists(win)
        win.art_sel.set_selected(names.index("Big Band"))
        yield 1000
        rows = win._album_rows()
        r0, r3 = rows[0], rows[3]
        report.append(("albums", [r.album.title for r in rows], "header found", all(r.header is not None for r in rows),
                       "header activatable", r0.header.get_activatable()))
        win.album_click(r3, 1)
        yield 200
        report.append(("single click", "selected", [r.album.title for r in win._sel_albums], "expanded", r3.get_expanded()))
        win.album_click(r3, 2)
        yield 600
        report.append(("double click", "expanded", r3.get_expanded(), "tracks built", len(r3.album.tracks)))
        win.album_click(r3, 1); win.album_click(r3, 2)
        yield 600
        report.append(("double click again", "expanded", r3.get_expanded()))
        win.album_click(r0, 1, ctrl=True)
        report.append(("ctrl click", [r.album.title for r in win._sel_albums]))
        win.album_click(rows[5], 1, shift=True)
        report.append(("shift click", [r.album.title for r in win._sel_albums]))
        # keyboard: Enter on a focused header opens/closes
        r0.header.grab_focus()
        yield 200
        win._on_album_key(None, Gdk.KEY_Return, 0, Gdk.ModifierType(0), r0)
        yield 400
        report.append(("Enter on header", "expanded", r0.get_expanded()))
        win._on_album_key(None, Gdk.KEY_Return, 0, Gdk.ModifierType(0), r0)
        yield 400
        # type-ahead in the album list (focus is on an album header)
        def key(ch=None, keyval=None):
            kv = keyval if keyval is not None else Gdk.unicode_to_keyval(ord(ch))
            return win._on_library_key(None, kv, 0, Gdk.ModifierType(0))
        r0.header.grab_focus()
        yield 200
        report.append(("focus in album list", win.get_focus().is_ancestor(win.album_scroll)))
        for ch in "album 4":
            key(ch)
        yield 300
        report.append(("typed 'album 4'", [r.album.title for r in win._sel_albums], "label", win.album_typeahead_label.get_text(),
                       win.album_typeahead_label.get_visible(), "artist label shown", win.typeahead_label.get_visible(),
                       "focus on that album", win.get_focus().is_ancestor(rows[4])))
        yield 1300
        key("z")
        report.append(("'z' no match", [r.album.title for r in win._sel_albums], "error", win.album_typeahead_label.has_css_class("error")))
        yield 1300
        # artist type-ahead still works when the artist list has the focus
        win.art_view.grab_focus()
        yield 200
        for ch in "hor":
            key(ch)
        yield 800
        report.append(("artist list focus, typed 'hor'", win.art_sel.get_selected_item().get_string()))
        shot(win, "albums")
        for line in report: print(*line, sep=" | ")
        print("screenshots in", OUT, flush=True)
        application.quit()

    gen = None
    def tick():
        try: GLib.timeout_add(next(gen), tick)
        except StopIteration: pass
        except Exception:
            import traceback; traceback.print_exc(); application.quit()
        return False
    def act(_a):
        nonlocal gen
        if gen is None:
            gen = steps(); GLib.timeout_add(10, tick)
    application.connect("activate", act)
    application.run([sys.argv[0]])

main()
