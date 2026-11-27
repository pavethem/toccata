"""GTK smoke test: the Play / Add to Queue buttons of a grouped album in the
Playlists view appear only while the pointer is over the album's header, and act
on that album. Real pointer input; needs the headless mutter:
    sh tests/gtk/run_headless_mutter.sh tests/gtk/gtk_smoke_plhover.py
Exits 1 on failure."""
import argparse, os, sys, tempfile, threading
PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="toccata-smoke-")
OUT = os.path.join(_TMP, "shots"); os.makedirs(OUT)
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "cfg")
os.environ["XDG_CACHE_HOME"] = os.path.join(_TMP, "cache")
sys.path.insert(0, PROJECT)
from tests import search_mock
from tests.gtk.gtk_smoke_plgroups import H


def main():
    search_mock.start()
    search_mock.Handler.queue[:] = ["rock/a2.flac"]
    srv = search_mock.Server(("127.0.0.1", 0), H)
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
        hl = search_mock.Handler
        hl.playlists.clear()
        hl.playlists["Mixed"] = ["rock/a1.flac", "rock/a2.flac", "jazz/b1.flac"] + \
            [f"many/{i:03d}.flac" for i in range(5)]
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

        win.views.set_visible_child_name("playlists")
        yield 800
        names = [win.pl_names.get_string(i) for i in range(win.pl_names.get_n_items())]
        win.pl_sel.set_selected(names.index("Mixed"))
        yield 1000
        g = win._plg.groups
        h0, h2 = g[0].header, g[2].header  # Rocker - First, Mass - Lots
        check("headers have Play + Add", [len(x.header.hover_buttons) for x in g], [2] * len(g))
        play, add = h2.hover_buttons
        check("Play is left of Add, both left of the collapse button",
              play.compute_point(h2, Graphene.Point())[1].x < add.compute_point(h2, Graphene.Point())[1].x
              < h2.toggle.compute_point(h2, Graphene.Point())[1].x, True)
        yield from run(lambda: inp.move(*at(win.pl_names_view if hasattr(win, "pl_names_view") else win.queue_view)))
        hidden = pixels(win, play, "away")
        hidden0 = pixels(win, h0.hover_buttons[0], "away-h0")
        yield from run(lambda: inp.move(*at(h2.title)))
        check("header hovered", bool(h2.get_state_flags() & Gtk.StateFlags.PRELIGHT), True)
        check("Play button drawn while hovering", pixels(win, play, "hover") != hidden, True)
        check("other album's button stays hidden", pixels(win, h0.hover_buttons[0], "hover-h0") == hidden0, True)
        yield from run(lambda: inp.move(*at(win.queue_view)))
        check("Play button hidden again", pixels(win, play, "away2") == hidden, True)
        want = [f"many/{i:03d}.flac" for i in range(5)]
        yield from run(lambda: (inp.move(*at(add)), inp.button(True), inp.button(False)))
        yield 500
        check("Add appends the album", list(hl.queue), ["rock/a2.flac"] + want)
        check("Add does not select the album", win._pl_selected_positions(), [])
        check("Add does not collapse the album", g[2].head_id in win._plg.collapsed_groups, False)
        yield from run(lambda: (inp.move(*at(play)), inp.button(True), inp.button(False)))
        yield 500
        check("Play replaces the queue with the album", list(hl.queue), want)
        check("Play does not select the album", win._pl_selected_positions(), [])
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
