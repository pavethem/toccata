"""GTK smoke test: Ctrl+A selects every album of the shown artist (Artists view)
or every queue entry, depending on the focus; text fields keep their Ctrl+A.
Run like gtk_smoke.py. Exits 1 on failure."""
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
        if a[0] == "playlistinfo":
            return "".join(lazy_mock.render(dict(s, Id=str(100 + p), Pos=str(p)))
                           for p, s in enumerate(self.db[:6])) + "OK\n"
        if a[0] in ("noidle", "currentsong", "replay_gain_status", "tagtypes", "outputs"):
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
    failures = []
    def check(name, got, want):
        ok = got == want
        print("ok  " if ok else "FAIL", name, got, "" if ok else f"(wanted {want})", flush=True)
        if not ok:
            failures.append(name)
    def steps():
        from gi.repository import Gdk
        win = application.props.active_window
        CTRL = Gdk.ModifierType.CONTROL_MASK
        ctrl_a = lambda: win._on_library_key(None, Gdk.KEY_a, 0, CTRL)
        yield 1500
        names = artists(win)
        win.art_sel.set_selected(names.index("Big Band"))
        yield 1000
        rows = win._album_rows()
        check("artist has several albums", len(rows) > 3, True)
        win.art_view.grab_focus(); yield 100
        check("Ctrl+A in the artist list handled", ctrl_a(), True)
        check("all albums selected", len(win._sel_albums), len(rows))
        win.album_click(rows[1], 1); rows[1].header.grab_focus(); yield 100
        ctrl_a()
        check("Ctrl+A in the album pane: all albums", len(win._sel_albums), len(rows))
        # with tracks selected inside an opened album, Ctrl+A switches to all albums
        win.album_click(rows[0], 1); win.album_click(rows[0], 2); yield 500
        tr = rows[0].track_rows
        win.track_click(tr[0], 1); tr[0].grab_focus(); yield 100
        ctrl_a()
        check("from a selected track: all albums, no tracks", (len(win._sel_albums), len(win._sel_tracks)), (len(rows), 0))
        # queue
        n_queue = win.queue_sel.get_n_items()
        check("queue has entries", n_queue, 6)
        win.queue_sel.select_item(2, True)
        win.queue_view.grab_focus(); yield 100
        check("Ctrl+A in the queue handled", ctrl_a(), True)
        check("whole queue selected", win.queue_sel.get_selection().get_size(), n_queue)
        check("albums left alone by queue Ctrl+A", len(win._sel_albums), len(rows))
        # text field: not taken
        win.activate_action("win.search", None); yield 300
        f = win.get_focus()
        check("focus in a text field", isinstance(f, Gtk.Editable) or (f is not None and f.get_ancestor(Gtk.Text) is not None), True)
        check("Ctrl+A in a text field not taken", ctrl_a(), False)
        print("FAILED" if failures else "ALL OK", flush=True)
        application.quit()

    gen = None
    def tick():
        try: GLib.timeout_add(next(gen), tick)
        except StopIteration: pass
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
