"""GTK smoke test: the Play / Add to Queue buttons of an album in the Artists view
appear only while the pointer is over the album's header, and act on that album.
Real pointer input; needs the headless mutter:
    sh tests/gtk/run_headless_mutter.sh tests/gtk/gtk_smoke_albumhover.py
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

COMMANDS = []  # queue-changing commands the app sent


class H(LazyHandler):
    def respond(self, a):
        if a[0] in ("add", "clear", "play", "addid"):
            COMMANDS.append(a)
            return "OK\n"
        if a[0] == "idle":
            return ""
        if a[0] in ("playlistinfo", "noidle", "currentsong", "replay_gain_status", "tagtypes", "outputs"):
            return "OK\n"
        return super().respond(a)


def main():
    srv, _ = lazy_mock.start(db=lazy_mock.genre_db())  # sets the handler's db
    srv.shutdown(); srv.server_close()
    srv = lazy_mock.LazyServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    import gi
    gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
    from gi.repository import GLib, Gtk, Gdk
    from toccata import app as appmod
    appmod.APP_ID = "io.github.toccata.smoke"
    args = argparse.Namespace(host="127.0.0.1", port=srv.server_address[1], password=None, music_dir=None,
                              no_covers=True, no_highlight=False)
    application = appmod.App(args)
    failures = []

    def check(name, got, want):
        ok = got == want
        print("ok  " if ok else "FAIL", name, got, "" if ok else f"(wanted {want})", flush=True)
        if not ok:
            failures.append(name)

    def pixels(win, widget, name):
        """The rendered pixels of `widget`'s area in the window (bytes), saved as PNG."""
        from gi.repository import Graphene
        p = Gtk.WidgetPaintable.new(win); snap = Gtk.Snapshot()
        p.snapshot(snap, win.get_width(), win.get_height())
        tex = win.get_native().get_renderer().render_texture(snap.to_node(), None)
        tex.save_to_png(f"{OUT}/{name}.png")
        ok, b = widget.compute_bounds(win)
        x, y, w, h = int(b.get_x()), int(b.get_y()), int(b.get_width()), int(b.get_height())
        data, stride = Gdk.TextureDownloader.new(tex).download_bytes()
        buf = data.get_data()
        return b"".join(bytes(buf[(y + j) * stride + x * 4:(y + j) * stride + (x + w) * 4]) for j in range(h))

    def steps():
        from gi.repository import Graphene
        from tests.gtk.mutter_input import Input, start
        win = application.props.active_window
        win.fullscreen()
        yield 1500
        inp = Input()

        def at(widget, x=None, y=None):
            ok, p = widget.compute_point(win, Graphene.Point().init(
                widget.get_width() / 2 if x is None else x, widget.get_height() / 2 if y is None else y))
            k = win.get_surface().get_scale()
            return p.x * k, p.y * k

        def run(fn):
            t = start(fn)
            while t.is_alive():
                yield 50
            yield 300

        names = [win.artist_store.get_string(i) for i in range(win.artist_store.get_n_items())]
        win.art_sel.set_selected(names.index("Big Band"))
        yield 1000
        rows = win._album_rows()
        check("artist has several albums", len(rows) > 2, True)
        r0, r1 = rows[0], rows[1]
        check("each album has Play + Add buttons", [len(r.hover_buttons) for r in rows], [2] * len(rows))
        play_x = r1.hover_buttons[0].compute_point(r1, Graphene.Point().init(0, 0))[1].x
        add_x = r1.hover_buttons[1].compute_point(r1, Graphene.Point().init(0, 0))[1].x
        check("Play is left of Add", play_x < add_x, True)
        play, add = r1.hover_buttons
        # pointer away from the albums
        yield from run(lambda: inp.move(*at(win.queue_view)))
        hidden = pixels(win, play, "away")
        hidden0 = pixels(win, r0.hover_buttons[0], "away-r0")
        check("header not hovered", bool(r1.header.get_state_flags() & Gtk.StateFlags.PRELIGHT), False)
        # pointer on album 1's title
        yield from run(lambda: inp.move(*at(r1.header, 80)))
        check("header hovered", bool(r1.header.get_state_flags() & Gtk.StateFlags.PRELIGHT), True)
        shown = pixels(win, play, "hover")
        check("Play button drawn while hovering", shown != hidden, True)
        check("other album's button stays hidden", pixels(win, r0.hover_buttons[0], "hover-r0") == hidden0, True)
        # the title keeps its place: the buttons only change opacity
        check("buttons keep their size when hidden", play.get_width() > 0, True)
        # away again: hidden
        yield from run(lambda: inp.move(*at(win.queue_view)))
        check("Play button hidden again", pixels(win, play, "away2") == hidden, True)
        # click Add to Queue: adds album 1's tracks, does not select or open the album
        COMMANDS.clear()
        yield from run(lambda: (inp.move(*at(add)), inp.button(True), inp.button(False)))
        yield 500
        uris = [t["file"] for t in r1.album.tracks]
        check("Add sends the album's tracks", [c[1] for c in COMMANDS if c[0] == "add"], uris)
        check("Add does not clear the queue", any(c[0] == "clear" for c in COMMANDS), False)
        check("Add does not open the album", r1.get_expanded(), False)
        # click Play: replaces the queue with album 1 and plays
        COMMANDS.clear()
        yield from run(lambda: (inp.move(*at(play)), inp.button(True), inp.button(False)))
        yield 500
        check("Play clears the queue first", COMMANDS[:1] and COMMANDS[0][0], "clear")
        check("Play sends the album's tracks", [c[1] for c in COMMANDS if c[0] == "add"], uris)
        check("Play starts playback", any(c[0] == "play" for c in COMMANDS), True)
        check("Play does not open the album", r1.get_expanded(), False)
        # click the empty space below the last album: the pane takes the keyboard focus
        # (type-ahead), which lands on the first album's header; its buttons must stay
        # hidden (user report 2026-11-27: "the mouse hover controls pop up over the first album")
        win.art_sel.set_selected(names.index("Band"))  # 2 albums: room below them
        yield 1000
        rows = win._album_rows()
        a0, a1 = rows[0], rows[-1]
        yield from run(lambda: inp.move(*at(win.queue_view)))
        win.queue_view.grab_focus()
        yield 300
        hid_play, hid_add = pixels(win, a0.hover_buttons[0], "b-away"), pixels(win, a0.hover_buttons[1], "b-away-add")
        ok, lb = a1.compute_bounds(win.album_scroll)
        empty_y = lb.get_y() + lb.get_height() + 30
        check("empty space below the last album", empty_y < win.album_scroll.get_height() - 5, True)
        yield from run(lambda: (inp.move(*at(win.album_scroll, 200, empty_y)), inp.button(True), inp.button(False)))
        yield 300
        f = win.get_focus()
        check("click on empty space: focus in the album pane", f is not None and f.is_ancestor(win.album_scroll), True)
        check("click on empty space: first album's buttons stay hidden",
              pixels(win, a0.hover_buttons[0], "b-empty-click") == hid_play, True)
        # keyboard focus on one of the buttons shows both buttons of that album
        a0.hover_buttons[0].grab_focus()
        yield 300
        check("focused button shown", pixels(win, a0.hover_buttons[0], "b-btn-focus") != hid_play, True)
        check("its album's other button shown", pixels(win, a0.hover_buttons[1], "b-btn-focus-add") != hid_add, True)
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
