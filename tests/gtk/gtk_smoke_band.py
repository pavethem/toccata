"""GTK smoke test: click-and-drag selection (rubber band) from empty space in the
queue and in the Artists view's album list, with real pointer input; a drag that
starts on a row is still drag-and-drop. Needs the headless mutter:
    TOCCATA_MONITOR=3840x2400 sh tests/gtk/run_headless_mutter.sh tests/gtk/gtk_smoke_band.py
Exits 1 on failure."""
import argparse, os, sys, tempfile, threading, time
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
        from gi.repository import Graphene
        from tests.gtk.mutter_input import Input, start, KEY_ESC
        win = application.props.active_window
        win.fullscreen()
        yield 1500
        inp = Input()
        def at(widget, x=None, y=None):
            """Screen point of (x, y) in widget (default: its centre)."""
            ok, p = widget.compute_point(win, Graphene.Point().init(
                widget.get_width() / 2 if x is None else x, widget.get_height() / 2 if y is None else y))
            k = win.get_surface().get_scale()  # the virtual monitor may be scaled: input is in device pixels
            return p.x * k, p.y * k
        def run(fn):
            t = start(fn)
            while t.is_alive():
                yield 50
            yield 300
        # -- queue: 6 entries, empty space below them
        songs = win._queue_songs
        check("queue has 6 entries", len(songs), 6)
        qx, _ = at(win.queue_view)
        end_y = at(win.queue_view, 0, win._queue_end_y())[1]
        row3 = songs[3].row
        _, r3y = at(row3)
        # a click on the empty space selects nothing (and starts no band)
        yield from run(lambda: (inp.move(qx, end_y + 100), inp.button(True), inp.button(False)))
        check("plain click on empty space: no band", (win._queue_band.active, win._queue_selected()), (False, []))
        yield from run(lambda: inp.drag(qx, end_y + 120, qx, r3y))
        check("queue band from below up to entry 3", [songs.index(s) for s in win._queue_selected()], [3, 4, 5])
        check("band hidden after release", win._queue_band.band.get_visible(), False)
        yield from run(lambda: inp.drag(qx, end_y + 60, qx, end_y + 200))
        check("band only over empty space selects nothing", win._queue_selected(), [])
        # Ctrl adds to the selection. Called directly: under the headless mutter an
        # injected Ctrl reaches GTK only after the next press (no keyboard focus before).
        win.queue_sel.select_item(next(i for i in range(win.queue_sel.get_n_items())
                                       if win.queue_sel.get_item(i) is songs[0]), True)
        off = win.queue_view.get_vadjustment().get_value()
        _, r4 = songs[4].row.compute_point(win.queue_view, Graphene.Point().init(0, songs[4].row.get_height() / 2))
        win._queue_band_start(True, 0, 0)
        win._queue_band_moved(win._queue_end_y() + 40 + off, r4.y + off, True)
        yield 100
        check("Ctrl+band adds", [songs.index(s) for s in win._queue_selected()], [0, 4, 5])
        # a press on a row and a move is drag-and-drop, not a band
        win._drag = None
        _, r1y = at(songs[1].row)
        def row_drag():
            inp.drag(qx, r1y, qx, r1y + 100, release=False, hold=0.5)
        yield from run(row_drag)
        check("drag from a row: no band", win._queue_band.active, False)
        check("drag from a row: drag-and-drop", (win._drag or ("",))[0], "queue")
        yield from run(lambda: (inp.key(True, KEY_ESC), inp.key(False, KEY_ESC), inp.button(False)))
        check("selection kept by the row drag", [songs.index(s) for s in win._queue_selected()], [0, 4, 5])
        # a plain click on the empty space clears the selection (user request 2026-11-12)
        yield from run(lambda: (inp.move(qx, end_y + 100), inp.button(True), inp.button(False)))
        check("click on empty queue space clears the selection", win._queue_selected(), [])
        yield from run(lambda: (inp.move(qx, r1y), inp.button(True), inp.button(False)))
        check("click on a queue row still selects it", [songs.index(s) for s in win._queue_selected()], [1])
        # -- album list of an artist with several albums
        names = artists(win)
        win.art_sel.set_selected(names.index("Big Band"))
        yield 1000
        rows = win._album_rows()
        check("artist has several albums", len(rows) > 3, True)
        ax, _ = at(win.album_list)
        bottom = at(win.album_list, 0, win.album_list.get_height())[1]
        _, h1 = at(rows[1].header)
        yield from run(lambda: inp.drag(ax, bottom + 80, ax - 250, h1, release=False))
        check("band shown while dragging", win._album_band.band.get_visible(), True)
        shot(win, "album-band-dragging")
        yield from run(lambda: inp.button(False))
        check("album band from below up to album 1", [rows.index(r) for r in win._sel_albums],
              list(range(1, len(rows))))
        # from the left margin, top down
        # the right margin: next to the left one is the pane divider, whose grab area reaches in
        mx, my = at(win.album_list, win.album_list.get_width() + 6, -6)
        _, h2 = at(rows[2].header)
        yield from run(lambda: inp.drag(mx, my, mx, h2))
        check("album band in the left margin", [rows.index(r) for r in win._sel_albums], [0, 1, 2])
        # a plain click on empty space clears the selection (user request 2026-11-12)
        click_empty = lambda: (inp.move(ax, bottom + 80), inp.button(True), inp.button(False))
        yield from run(click_empty)
        check("click on empty space clears the albums", win._sel_albums, [])
        yield from run(lambda: (inp.move(ax, h2), inp.button(True), inp.button(False)))
        check("click on an album still selects it", [rows.index(r) for r in win._sel_albums], [2])
        # a press on an album header and a move is drag-and-drop
        win._drag = None
        _, h0 = at(rows[0].header)
        yield from run(lambda: inp.drag(ax, h0, ax + 30, h0 + 150, release=False, hold=0.5))
        check("drag from an album: no band", win._album_band.active, False)
        check("drag from an album: drag-and-drop", (win._drag or ("",))[0], "tracks")
        yield from run(lambda: (inp.key(True, KEY_ESC), inp.key(False, KEY_ESC), inp.button(False)))
        # a real drop (album -> queue), then the very first press on empty space must
        # start a band (user report 2026-11-12: it took two clicks after a drag-and-drop;
        # the drag took the button release away from the band's gesture)
        qend = at(win.queue_view, 0, win._queue_end_y())[1]
        _, h3 = at(rows[3].header)
        def drop():
            inp.move(ax, h3); inp.button(True)
            for i in range(1, 31):
                inp.move(ax + (qx - ax) * i / 30, h3 + (qend + 60 - h3) * i / 30)
            time.sleep(0.3)
            inp.button(False)
        yield from run(drop)
        yield 500
        _, h1 = at(rows[1].header)
        yield from run(lambda: inp.drag(ax, bottom + 80, ax, h1))
        check("first band after a drop (album list)", [rows.index(r) for r in win._sel_albums],
              list(range(1, len(rows))))
        yield from run(drop)
        yield 500
        songs = win._queue_songs
        qend = at(win.queue_view, 0, win._queue_end_y())[1]
        _, rly = at(songs[-1].row)
        yield from run(lambda: inp.drag(qx, qend + 40, qx, rly))
        check("first band after a drop (queue)", [songs.index(s) for s in win._queue_selected()], [len(songs) - 1])
        # tracks of an opened album, along the margin
        rows[0].set_expanded(True); yield 500
        tr = rows[0].track_rows
        check("album 0 has 2+ tracks", len(tr) >= 2, True)
        _, t0 = at(tr[0]); _, t1 = at(tr[1])
        lx, _ = at(win.album_list, -4, 0)
        yield from run(lambda: inp.drag(lx, t0, lx, t1))
        check("band over tracks selects tracks", ([tr.index(r) for r in win._sel_tracks], win._sel_albums), ([0, 1], []))
        yield from run(lambda: (inp.move(lx, t0), inp.button(True), inp.button(False)))
        check("click on empty space clears the tracks", (win._sel_tracks, win._sel_albums), ([], []))
        # autoscroll: many opened albums, band from the top margin down past the bottom edge
        adj = win.album_scroll.get_vadjustment()
        for r in rows:  # open albums until the list is about 3 screens long
            r.set_expanded(True)
            yield 300
            if adj.get_upper() > 3 * adj.get_page_size():
                break
        if adj.get_upper() > adj.get_page_size() + 50:
            adj.set_value(0); yield 200
            sb = at(win.album_scroll, 0, win.album_scroll.get_height())[1]
            yield from run(lambda: inp.drag(lx, my, lx, sb + 100, hold=5.0))
            check("band scrolled the list", adj.get_value() > 0, True)
            check("band reached the last album", rows[-1] in win._sel_albums, True)
        else:
            print("skip autoscroll: albums fit on screen", flush=True)
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
    print("screenshots:", OUT, flush=True)
    sys.exit(1 if failures else 0)

main()
