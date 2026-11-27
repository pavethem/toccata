"""GTK smoke test: what is shown at the pointer while dragging (user report
2026-11-13: it read "toccata-artist" etc., the drag's internal tag). Real pointer
drags from an artist, an album, an album's track, a queue row, several selected
queue rows and a queue album header; the label GTK puts into the drag icon is
read back. Needs the headless mutter:
    TOCCATA_MONITOR=3840x2400 sh tests/gtk/run_headless_mutter.sh tests/gtk/gtk_smoke_draglabel.py
Exits 1 on failure."""
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
    from gi.repository import Gdk, GLib, Gtk

    # Record the drag icon's text of every drag the app starts: GTK fills the
    # default icon from the content after "drag-begin", so it is read in an idle.
    icons, hotspots, styled = [], [], []
    _set_hotspot = Gdk.Drag.set_hotspot

    def recording_set_hotspot(drag, x, y):  # (the app's call goes through Python)
        hotspots.append((x, y))
        return _set_hotspot(drag, x, y)
    Gdk.Drag.set_hotspot = recording_set_hotspot
    _DragSource = Gtk.DragSource

    class RecordingDragSource(_DragSource):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.connect("drag-begin", self._begun)

        def _begun(self, _src, drag):
            def read():
                child = Gtk.DragIcon.get_for_drag(drag).get_child()
                icons.append(child.get_label() if isinstance(child, Gtk.Label) else repr(child))
                styled.append(child.has_css_class("drag-label"))
                if len(styled) == 1:  # a picture of the first one
                    p = Gtk.WidgetPaintable.new(child); snap = Gtk.Snapshot()
                    p.snapshot(snap, child.get_width(), child.get_height())
                    node = snap.to_node()
                    if node is not None:
                        child.get_native().get_renderer().render_texture(node, None).save_to_png(f"{OUT}/label.png")
                return False
            GLib.idle_add(read)
    Gtk.DragSource = RecordingDragSource

    from toccata import app as appmod
    appmod.APP_ID = "io.github.toccata.smoke"
    args = argparse.Namespace(host="127.0.0.1", port=srv.server_address[1], password=None, music_dir=None,
                              no_covers=True, no_highlight=False)
    application = appmod.App(args)
    failures = []

    def check(name, got, want):
        ok = got == want
        print("ok  " if ok else "FAIL", name, repr(got), "" if ok else f"(wanted {want!r})", flush=True)
        if not ok:
            failures.append(name)

    def steps():
        from gi.repository import Graphene
        from tests.gtk.mutter_input import Input, start, KEY_ESC
        win = application.props.active_window
        win.fullscreen()
        yield 1500
        inp = Input()

        def at(widget):
            ok, p = widget.compute_point(win, Graphene.Point().init(widget.get_width() / 2, widget.get_height() / 2))
            k = win.get_surface().get_scale()
            return p.x * k, p.y * k

        def run(fn):
            t = start(fn)
            while t.is_alive():
                yield 50
            yield 300

        def drag_from(widget):
            """Press on the widget, move, read the icon, cancel with Escape."""
            del icons[:]
            x, y = at(widget)
            yield from run(lambda: inp.drag(x, y, x + 60, y + 120, release=False, hold=0.5))
            got = icons[-1] if icons else None
            yield from run(lambda: (inp.key(True, KEY_ESC), inp.key(False, KEY_ESC), inp.button(False)))
            return got

        def artist_row(name):
            child = win.art_view.get_first_child()
            while child is not None:
                label = child.get_first_child()
                if getattr(label, "artist", None) == name:
                    return label
                child = child.get_next_sibling()

        names = [win.artist_store.get_string(i) for i in range(win.artist_store.get_n_items())]
        win.art_sel.set_selected(names.index("Big Band"))
        yield 1000
        got = yield from drag_from(artist_row("Big Band"))
        check("artist", got, "Big Band")
        rows = win._album_rows()
        got = yield from drag_from(rows[0].header)
        check("album", got, rows[0].album.title)
        rows[0].set_expanded(True)
        yield 500
        track = rows[0].track_rows[1]
        got = yield from drag_from(track)
        check("album track", got, track.track.get("title"))
        songs = win._queue_songs
        got = yield from drag_from(songs[1].row)
        check("queue row", got, songs[1].data.get("title"))
        for i in (2, 3, 4):
            win.queue_sel.select_item(i, False)
        got = yield from drag_from(songs[3].row)
        check("three selected queue rows", got, "3 tracks")
        win.activate_action("win.queue-grouped", None)
        yield 800
        group = win._queue_groups[0]
        got = yield from drag_from(group.header)
        check("queue album header", got, group.title)
        check("no internal tag was ever shown", [t for t in icons if t and t.startswith("toccata-")], [])
        check("every icon is the app's label", styled and all(styled), True)
        # left of / above the icon's corner means the label sits right of / below the
        # pointer's tip, clear of the arrow (GTK's default is (-2, -2): under the cursor)
        size = Gtk.Settings.get_default().get_property("gtk-cursor-theme-size") or 24
        check("label beside the cursor, not under it",
              bool(hotspots) and all(x <= -0.75 * size and y < 0 for x, y in hotspots), True)
        print("hotspots", sorted(set(hotspots)), "cursor size", size, "picture", f"{OUT}/label.png")
        print("FAILED" if failures else "ALL OK", flush=True)
        application.quit()

    gen = None

    def tick():
        try:
            GLib.timeout_add(next(gen), tick)
        except StopIteration:
            pass
        except Exception:
            import traceback
            traceback.print_exc(); failures.append("exception"); application.quit()
        return False

    def act(_a):
        nonlocal gen
        if gen is None:
            gen = steps(); GLib.timeout_add(10, tick)
    application.connect("activate", act)
    application.run([sys.argv[0]])
    sys.stdout.flush()
    os._exit(1 if failures else 0)


main()
