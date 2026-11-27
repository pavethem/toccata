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
        win = application.props.active_window
        yield 1500
        report.append(("genres", [win.genre_model.get_string(i) for i in range(win.genre_model.get_n_items())],
                       "enabled", win.genre_drop.get_sensitive()))
        report.append(("all: artists", len(artists(win))))
        win.genre_drop.set_selected(2)  # Jazz
        yield 1000
        report.append(("Jazz: artists", artists(win), "selected", win.art_sel.get_selected_item().get_string()))
        win.art_sel.set_selected(artists(win).index("Hornist"))
        yield 800
        report.append(("Jazz/Hornist albums", albums(win), "setting", win.settings.genre))
        shot(win, "genre-jazz")
        win.genre_drop.set_selected(0)
        yield 1000
        report.append(("All again: artists", len(artists(win)), "shown", win._shown_artist.name if win._shown_artist else None,
                       "albums", albums(win)))
        win.activate_action("win.genre", None)
        yield 500
        report.append(("Ctrl+G: focus in dropdown", win.genre_drop.has_focus() or (win.get_focus() is not None and win.get_focus().is_ancestor(win.genre_drop)),
                       "focus", type(win.get_focus()).__name__ if win.get_focus() else None))
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
