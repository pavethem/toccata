"""GTK smoke test: a Shift+click range in the queue starts at the last thing
selected by a header click or a right-click, not at an older track click
(user report 2026-11-11: Shift+click dropped the entries above). Uses GTK's own
"list.select-item" action, which is what a real click runs. Exits 1 on failure."""
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
                           for p, s in enumerate(self.db[:40])) + "OK\n"
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
    class G:
        def __init__(self, mods=0): self.mods = mods
        def get_current_event_state(self):
            from gi.repository import Gdk
            return Gdk.ModifierType(self.mods)
    def steps():
        from gi.repository import Gdk
        win = application.props.active_window
        yield 1500
        # Positions here are queue indices of songs; grouped, the list also has album
        # header rows (QueueAlbum), so rows are found by their song object.
        def sel():
            chosen = set(map(id, win._queue_selected()))
            return [i for i, s in enumerate(win._queue_songs) if id(s) in chosen]
        def row_of(i):
            song = win._queue_songs[i]
            return next(p for p in range(win.queue_sel.get_n_items()) if win.queue_sel.get_item(p) is song)
        def click(i, shift=False, ctrl=False):  # GTK's own click handler runs exactly this
            win.queue_view.activate_action("list.select-item", GLib.Variant("(ubb)", (row_of(i), ctrl, shift)))
        win.activate_action("win.queue-grouped", None)
        yield 500
        n = len(win._queue_songs)
        groups = {(g.start, g.end): g for g in win._queue_groups}
        check("mock queue has single and multi-track albums", ((11, 12) in groups, (6, 9) in groups, n), (True, True, 40))
        click(5); click(n - 1, True)
        check("plain click + Shift+click (baseline)", sel(), list(range(5, n)))
        # header click, then Shift+click below
        click(25)
        groups[(11, 12)].header._on_click(G(), 1, -5, -5)
        check("header click selects the album", sel(), [11])
        click(n - 1, True)
        check("Shift+click after a header click starts at the album", sel(), list(range(11, n)))
        # multi-track album header, Shift+click above it
        click(2)
        groups[(6, 9)].header._on_click(G(), 1, -5, -5)
        check("header click selects all its tracks", sel(), [6, 7, 8])
        click(0, True)
        check("Shift+click upwards from it", sel(), list(range(0, 7)))
        # Ctrl+header click adds and keeps the rest
        click(0)
        groups[(6, 9)].header._on_click(G(Gdk.ModifierType.CONTROL_MASK), 1, -5, -5)
        check("Ctrl+header click adds", sel(), [0, 6, 7, 8])
        # right-click outside the selection
        click(30)
        win._queue_menu(win._queue_songs[8], win.queue_view, 0, 0)
        yield 100
        check("right-click selects that track", sel(), [8])
        click(n - 1, True)
        check("Shift+click after a right-click starts there", sel(), list(range(8, n)))
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
