"""GTK smoke test: the Play / Add to Queue buttons on an album cover in the Albums
view appear only while the pointer is over the tile, and act on that album only
(the selection stays). Real pointer input; needs the headless mutter:
    sh tests/gtk/run_headless_mutter.sh tests/gtk/gtk_smoke_tilehover.py
Exits 1 on failure."""
import argparse, os, sys, tempfile, threading
PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="toccata-smoke-")
OUT = os.path.join(_TMP, "shots"); os.makedirs(OUT)
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "cfg")
os.environ["XDG_CACHE_HOME"] = os.path.join(_TMP, "cache")
sys.path.insert(0, PROJECT)
from tests import albums_mock, lazy_mock

COMMANDS = []  # queue-changing commands the app sent


class H(albums_mock.Handler):
    def respond(self, a):
        if a[0] in ("add", "clear", "play", "addid"):
            COMMANDS.append(a)
            return "OK\n"
        if a[0] == "idle":
            return ""
        if a[0] in ("playlistinfo", "noidle", "currentsong", "replay_gain_status", "tagtypes", "outputs",
                    "listplaylists"):
            return "OK\n"
        return super().respond(a)


def main():
    albums_mock.start()
    srv = lazy_mock.LazyServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    import gi
    gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
    from gi.repository import GLib, Gtk, Gdk
    from toccata import app as appmod
    from toccata import albums as albumslib
    appmod.APP_ID = "io.github.toccata.smoke"
    args = argparse.Namespace(host="127.0.0.1", port=srv.server_address[1], password=None, music_dir=None,
                              no_covers=False, no_highlight=False)
    application = appmod.App(args)
    failures = []

    def check(name, got, want):
        ok = got == want
        print("ok  " if ok else "FAIL", name, got, "" if ok else f"(wanted {want})", flush=True)
        if not ok:
            failures.append(name)

    def pixels(win, widget, name):
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
        win.views.set_visible_child_name("albums")
        yield 2000
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

        items = [win.albums_store.get_item(i) for i in range(win.albums_store.get_n_items())]
        tiles = [it.tile for it in items[:2]]
        check("first tiles are on screen", all(t is not None for t in tiles), True)
        t0, t1 = tiles
        check("each tile has Play + Add", [len(t.hover_buttons) for t in tiles], [2, 2])
        play, add = t1.hover_buttons
        px = play.compute_point(t1, Graphene.Point().init(0, 0))[1]
        ax = add.compute_point(t1, Graphene.Point().init(0, 0))[1]
        check("Play is left of Add", px.x < ax.x, True)
        ok, cb = t1.cover.compute_bounds(t1)
        ok, bb = add.compute_bounds(t1)
        check("buttons lie on the cover", cb.contains_rect(bb), True)
        # select album 0, so we can see the buttons leave the selection alone
        win.albums_sel.select_item(0, True)
        yield 200
        yield from run(lambda: inp.move(*at(win.albums_count)))
        hidden = pixels(win, play, "away")
        hidden0 = pixels(win, t0.hover_buttons[0], "away-t0")
        yield from run(lambda: inp.move(*at(t1.title)))
        check("tile hovered", bool(t1.get_state_flags() & Gtk.StateFlags.PRELIGHT), True)
        # the OSD button is a dark circle on the grey placeholder cover; GridView's own
        # hover highlight changes the pixels too, so compare brightness, not equality
        dark = lambda px: sum(1 for i in range(0, len(px), 4) if max(px[i:i + 3]) < 40)
        shown = pixels(win, play, "hover")
        print("     dark pixels hidden/shown:", dark(hidden), dark(shown), flush=True)
        check("Play drawn while hovering", dark(shown) > dark(hidden) + len(shown) // 16, True)
        check("other tile's button stays hidden", pixels(win, t0.hover_buttons[0], "hover-t0") == hidden0, True)
        yield from run(lambda: inp.move(*at(win.albums_count)))
        check("Play hidden again", pixels(win, play, "away2") == hidden, True)
        uris = [t["file"] for t in albumslib.fetch_album_tracks(win.client, items[1].entry)]
        check("album 1 has tracks", len(uris) > 0, True)
        COMMANDS.clear()
        yield from run(lambda: (inp.move(*at(add)), inp.button(True), inp.button(False)))
        yield 700
        check("Add sends album 1's tracks", [c[1] for c in COMMANDS if c[0] == "add"], uris)
        check("Add does not clear the queue", any(c[0] == "clear" for c in COMMANDS), False)
        check("selection unchanged", win._albums_selected_positions(), [0])
        COMMANDS.clear()
        yield from run(lambda: (inp.move(*at(play)), inp.button(True), inp.button(False)))
        yield 700
        check("Play clears the queue first", COMMANDS[:1] and COMMANDS[0][0], "clear")
        check("Play sends album 1's tracks", [c[1] for c in COMMANDS if c[0] == "add"], uris)
        check("Play starts playback", any(c[0] == "play" for c in COMMANDS), True)
        check("selection still unchanged", win._albums_selected_positions(), [0])
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
