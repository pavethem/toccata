"""GTK smoke test: selecting tracks inside an opened album (click, Ctrl, Shift, drag, double click).
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
        from gi.repository import Gtk as _Gtk
        _Gtk.Settings.get_default().set_property("gtk-enable-animations", False)  # albums open in one step
        win = application.props.active_window
        yield 1500
        names = artists(win)
        win.art_sel.set_selected(names.index("Band"))
        yield 1000
        rows = win._album_rows()
        debut = rows[0]
        win.album_click(debut, 1); win.album_click(debut, 2)  # double click opens
        yield 2500
        tr = debut.track_rows
        report.append(("opened", debut.album.title, "tracks", [r.track["title"] for r in tr]))
        win.track_click(tr[1], 1)
        report.append(("click track", [r.track["title"] for r in win._sel_tracks], "button", win.add_all.get_label(),
                       "css", tr[1].has_css_class("track-selected")))
        win.track_click(tr[3] if len(tr) > 3 else tr[-1], 1, shift=True)
        report.append(("shift-click", [r.track["title"] for r in win._sel_tracks], "button", win.add_all.get_label()))
        win.track_click(tr[2], 1, ctrl=True)
        report.append(("ctrl-click", [r.track["title"] for r in win._sel_tracks], "targets", [t["title"] for t in win._library_targets()]))
        win._drag_tracks(tr[1])
        report.append(("drag a selected track carries", [t["title"] for t in win._drag[1]]))
        win._drag_tracks(tr[0])
        report.append(("drag an unselected track carries", [t["title"] for t in win._drag[1]]))
        # the album's own drag source, started over a track row: must carry the track(s), not the album
        from gi.repository import Graphene
        ok, pt = tr[1].compute_point(debut, Graphene.Point().init(20, 10))
        win._drag_album(debut, pt.x, pt.y)
        report.append(("album drag started on selected track carries", [t["title"] for t in win._drag[1]]))
        ok, pt = tr[0].compute_point(debut, Graphene.Point().init(20, 10))
        win._drag_album(debut, pt.x, pt.y)
        report.append(("album drag started on unselected track carries", [t["title"] for t in win._drag[1]]))
        ok, pt = debut.header.compute_point(debut, Graphene.Point().init(20, 10))
        win._drag_album(debut, pt.x, pt.y)
        report.append(("album drag started on the header carries", len(win._drag[1]), "tracks (whole album)"))
        win.album_click(rows[1], 1)  # selecting an album clears the tracks
        report.append(("album clicked", [r.album.title for r in win._sel_albums], "tracks", len(win._sel_tracks),
                       "css cleared", not any(r.has_css_class("track-selected") for r in tr)))
        win.track_click(tr[0], 1)
        report.append(("track clicked again", len(win._sel_albums), [r.track["title"] for r in win._sel_tracks]))
        win.album_click(debut, 1); win.album_click(debut, 2)  # double click closes: hidden tracks are dropped
        yield 800
        report.append(("album closed", "expanded", debut.get_expanded(), "selected tracks", len(win._sel_tracks), "button", win.add_all.get_label()))
        win.album_click(debut, 1); win.album_click(debut, 2)
        yield 600
        before = []
        orig = win.add_tracks
        win.add_tracks = lambda tracks, name=None: before.append([t["title"] for t in tracks])
        win.track_click(debut.track_rows[0], 1)
        win.track_click(debut.track_rows[0], 2)  # double click on a track
        report.append(("double-click track adds", before))
        win.add_tracks = orig
        win.track_click(debut.track_rows[1], 1); win.track_click(debut.track_rows[2], 1, shift=True)
        yield 2500
        shot(win, "tracks")
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
