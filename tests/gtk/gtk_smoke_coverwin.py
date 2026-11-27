"""GTK smoke test: the "Set Cover" search is a resizable window (user request
2026-11-17), with real pointer/keyboard input (mutter_input.py): dragging its corner
resizes it, the size is saved and used next time, Escape closes an open preview
first and then the window. Network replaced by canned answers. Run with
TOCCATA_MONITOR=3840x2400 sh tests/gtk/run_headless_mutter.sh tests/gtk/gtk_smoke_coverwin.py"""
import argparse, os, sys, tempfile, threading
PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="toccata-smoke-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "cfg")
os.environ["XDG_CACHE_HOME"] = os.path.join(_TMP, "cache")
MUSIC = os.path.join(_TMP, "music")
sys.path.insert(0, PROJECT)
from tests import lazy_mock
from tests.lazy_mock import LazyHandler


class H(LazyHandler):
    def respond(self, a):
        if a[0] == "idle":
            return ""
        if a[0] in ("noidle", "currentsong", "replay_gain_status", "tagtypes", "outputs", "listplaylists",
                    "playlistinfo"):
            return "OK\n"
        if a[0] in ("albumart", "readpicture"):
            return "ACK [50@0] {albumart} No file exists\n"
        return super().respond(a)


def main():
    os.makedirs(os.path.join(MUSIC, "band", "debut"))
    srv, _ = lazy_mock.start()
    srv.shutdown(); srv.server_close()
    srv = lazy_mock.LazyServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    import gi
    gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GLib, GdkPixbuf, Gtk
    from toccata import app as appmod, coversearch, settings as settingsmod
    from tests.gtk.mutter_input import Input, start, KEY_ESC
    appmod.APP_ID = "io.github.toccata.smoke"
    application = appmod.App(argparse.Namespace(host="127.0.0.1", port=srv.server_address[1], password=None,
                                                music_dir=MUSIC, no_covers=False, no_highlight=False))
    pb = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 64, 64)
    pb.fill(0x3366ccff)
    png = bytes(pb.save_to_bufferv("png", [], [])[1])
    coversearch.search = lambda q, a="", b="", **k: ([coversearch.Candidate("Deezer", "Band", "Debut", "t://1", "f://1")], [])
    coversearch.http_get = lambda url, **k: png
    failures = []

    def check(name, got, want):
        ok = got == want
        print("ok  " if ok else "FAIL", name, got, "" if ok else f"(wanted {want})", flush=True)
        if not ok:
            failures.append(name)

    def cover_win():
        return next((w for w in Gtk.Window.list_toplevels()
                     if isinstance(w, appmod.CoverSearchDialog) and w.get_visible()), None)

    tracks = [{"file": "band/debut/1.flac", "albumartist": "Band", "album": "Debut"}]
    inp = None

    def run(fn):
        t = start(fn)
        while t.is_alive():
            yield 50
        yield 300

    def steps():
        nonlocal inp
        win = application.props.active_window
        yield 1500
        inp = Input()
        win.search_cover_online(tracks, "Debut")
        yield 1500
        dlg = cover_win()
        check("Set Cover is its own window", type(dlg).__name__, "CoverSearchDialog")
        check("resizable, modal, on the main window",
              (dlg.get_resizable(), dlg.get_modal(), dlg.get_transient_for() is win), (True, True, True))
        w0, h0 = dlg.get_width(), dlg.get_height()
        check("default size", (w0, h0), (appmod.CoverSearchDialog.WIDTH, appmod.CoverSearchDialog.HEIGHT))
        # where is the window on the monitor? (Wayland does not say): a motion tells
        seen = []
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda _c, x, y: seen.append((x, y)))
        dlg.add_controller(motion)
        k = dlg.get_surface().get_scale()
        probe = (1920 * k / 2, 1200 * k / 2)  # the middle of the (logical 1920x1200) screen
        yield from run(lambda: (inp.move(probe[0] - 2, probe[1]), inp.move(*probe)))
        check("pointer seen by the window", bool(seen), True)
        if not seen:
            raise RuntimeError("no motion")
        # the surface may include the CSD shadow; widget coordinates are relative to the window
        ox, oy = probe[0] / k - seen[-1][0], probe[1] / k - seen[-1][1]  # window origin, logical
        cx, cy = (ox + w0 + 2) * k, (oy + h0 + 2) * k  # just outside the bottom-right corner: resize border
        yield from run(lambda: inp.drag(cx, cy, cx + 200 * k, cy + 120 * k))
        yield 500
        w1, h1 = dlg.get_width(), dlg.get_height()
        print("size after dragging the corner", (w1, h1), "from", (w0, h0), flush=True)
        check("dragging the corner made it bigger", (w1 > w0 + 100, h1 > h0 + 60), (True, True))
        # Escape: an open preview first, then the window
        dlg.flow.emit("child-activated", dlg.flow.get_child_at_index(0))
        yield 800
        check("preview open", type(dlg.get_visible_dialog()).__name__, "CoverPreviewDialog")
        mid = ((ox + w1 / 2) * k, (oy + h1 / 2) * k)  # on the preview's image: keyboard focus comes with a press
        yield from run(lambda: (inp.move(*mid), inp.button(True), inp.button(False)))
        check("the click kept the preview", type(dlg.get_visible_dialog()).__name__, "CoverPreviewDialog")
        yield from run(lambda: (inp.key(True, 42), inp.key(False, 42)))  # left Shift: without a key press the window never gets keyboard focus
        yield from run(lambda: (inp.key(True, KEY_ESC), inp.key(False, KEY_ESC)))
        check("first Escape closes only the preview", (dlg.get_visible_dialog(), cover_win() is dlg), (None, True))
        yield from run(lambda: (inp.key(True, KEY_ESC), inp.key(False, KEY_ESC)))
        check("second Escape closes the window", cover_win(), None)
        saved = settingsmod.load(settingsmod.config_path())
        check("size kept in the settings", (win.settings.cover_dialog_width, win.settings.cover_dialog_height), (w1, h1))
        check("and saved to disk", (saved.cover_dialog_width, saved.cover_dialog_height), (w1, h1))
        win.search_cover_online(tracks, "Debut")
        yield 1500
        dlg = cover_win()
        check("reopened with the saved size", (dlg.get_width(), dlg.get_height()), (w1, h1))
        dlg.close()
        yield 300
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
