"""GTK smoke test: with several albums (or tracks) selected, a plain press on one of
them keeps the selection (a drag may follow); the release selects only that one;
a drag in between keeps the selection. Run like gtk_smoke.py. Exits 1 on failure."""
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
    failures = []
    def check(name, got, want):
        ok = got == want
        print("ok  " if ok else "FAIL", name, got, "" if ok else f"(wanted {want})", flush=True)
        if not ok:
            failures.append(name)
    class FakeGesture:
        def __init__(self, widget, mods=0):
            self.w, self.mods = widget, mods
        def get_widget(self): return self.w
        def get_current_event_state(self):
            from gi.repository import Gdk
            return Gdk.ModifierType(self.mods)
    def steps():
        from gi.repository import Gdk
        win = application.props.active_window
        yield 1500
        names = artists(win)
        win.art_sel.set_selected(names.index("Big Band"))
        yield 1000
        rows = win._album_rows()
        titles = lambda: [r.album.title for r in win._sel_albums]
        win.album_click(rows[0], 1); win.album_click(rows[2], 1, shift=True)
        three = titles()
        check("shift selects three", len(three), 3)
        g = FakeGesture(rows[1].header)
        win._on_album_pressed(g, 1, -5, -5, rows[1])
        check("press on a selected album keeps the selection", titles(), three)
        win._on_album_released(g, 1, -5, -5, rows[1])
        check("release selects only it", titles(), [rows[1].album.title])
        win.album_click(rows[0], 1); win.album_click(rows[2], 1, shift=True)
        win._on_album_pressed(g, 1, -5, -5, rows[1])
        win._drag_album(rows[1])
        check("drag carries all three", len(win._drag[1]), sum(len(r.album.tracks) for r in rows[:3]))
        win._on_album_released(g, 1, -5, -5, rows[1])
        check("release after a drag keeps the selection", titles(), three)
        g4 = FakeGesture(rows[4].header)
        win._on_album_pressed(g4, 1, -5, -5, rows[4])
        check("press outside the selection selects at once", titles(), [rows[4].album.title])
        win._on_album_released(g4, 1, -5, -5, rows[4])
        win.album_click(rows[0], 1); win.album_click(rows[2], 1, shift=True)
        win._on_album_pressed(FakeGesture(rows[1].header, Gdk.ModifierType.CONTROL_MASK), 1, -5, -5, rows[1])
        check("ctrl press toggles at once", len(titles()), 2)
        # tracks inside an opened album
        win.album_click(rows[3], 1); win.album_click(rows[3], 2)
        yield 600
        tr = rows[3].track_rows
        check("album opened", len(tr) >= 3, True)
        win.track_click(tr[0], 1); win.track_click(tr[2], 1, shift=True)
        check("three tracks selected", len(win._sel_tracks), 3)
        gt = FakeGesture(tr[1])
        win._on_track_pressed(gt, 1, -5, -5, tr[1])
        check("press on a selected track keeps the selection", len(win._sel_tracks), 3)
        win._drag_tracks(tr[1])
        check("track drag carries three", len(win._drag[1]), 3)
        win._on_track_released(gt, 1, -5, -5, tr[1])
        check("release after the drag keeps them", len(win._sel_tracks), 3)
        win._on_track_pressed(gt, 1, -5, -5, tr[1])
        win._on_track_released(gt, 1, -5, -5, tr[1])
        check("press+release selects only that track", win._sel_tracks, [tr[1]])
        print("FAILED" if failures else "ALL OK", flush=True)
        application.exit_code = 1 if failures else 0
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
