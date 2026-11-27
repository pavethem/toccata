"""GTK smoke test: compact queue rows (queue menu "Compact Rows", setting queue_compact).

Checks, ungrouped and grouped, normal and compact: every queue row (track rows and
album header rows) has the same height - the condition for GTK's height estimate
to be exact, so the scrollbar does not jump (DECISIONS "album headers are list
rows") -, the list's height is the same at every scroll position, compact rows are
shorter, and switching modes keeps the view on the same rows (at the top: stays
at the top). Screenshots in the printed directory. Run with
tests/gtk/run_headless_mutter.sh; exits 1 on failure.

The other queue smoke tests run in compact mode with tests/gtk/compact.py."""
import argparse, os, sys, tempfile, threading
PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="toccata-smoke-")
OUT = os.path.join(_TMP, "shots")
os.makedirs(OUT, exist_ok=True)
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "cfg")
os.environ["XDG_CACHE_HOME"] = os.path.join(_TMP, "cache")
sys.path.insert(0, PROJECT)
from tests import search_mock
from tests.lazy_mock import render

QUEUE = [dict(file=f"q/{a:02d}/{t:02d}.flac", Title=f"Track {t} with a longer title",
              Artist=f"Artist {a}", AlbumArtist=f"Artist {a}", Album=f"Album {a}", Track=str(t), Time="200")
         for a in range(20) for t in range(1, 11)]
PLAYING = 3


class H(search_mock.Handler):
    def respond(self, a):
        cmd = a[0]
        if cmd == "idle":
            return ""
        if cmd in ("noidle", "replay_gain_status", "outputs"):
            return "OK\n"
        if cmd == "status":
            return f"state: play\nsong: {PLAYING - 1}\nsongid: {PLAYING}\nplaylistlength: {len(QUEUE)}\nOK\n"
        if cmd == "currentsong":
            return render(dict(QUEUE[PLAYING - 1], Id=str(PLAYING), Pos=str(PLAYING - 1))) + "OK\n"
        if cmd == "playlistinfo":
            return "".join(render(dict(s, Id=str(p + 1), Pos=str(p))) for p, s in enumerate(QUEUE)) + "OK\n"
        return super().respond(a)


def main():
    search_mock.start()
    srv = search_mock.Server(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    import gi
    gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
    from gi.repository import GLib, Gtk
    from toccata import app as appmod
    appmod.APP_ID = "io.github.toccata.smoke"
    application = appmod.App(argparse.Namespace(host="127.0.0.1", port=srv.server_address[1], password=None,
                                                music_dir=None, no_covers=False, no_highlight=False))
    failures = []

    def check(name, ok, detail=""):
        print("ok  " if ok else "FAIL", name, detail, flush=True)
        if not ok:
            failures.append(name)

    def shot(win, name):
        w, h = win.get_width(), win.get_height()
        paintable = Gtk.WidgetPaintable.new(win)
        snap = Gtk.Snapshot()
        paintable.snapshot(snap, w, h)
        node = snap.to_node()
        if node is not None:
            win.get_native().get_renderer().render_texture(node, None).save_to_png(f"{OUT}/{name}.png")

    def row_heights(win):
        """{height: count} of the queue rows laid out now, and whether headers were among them."""
        heights, headers = {}, 0
        child = win.queue_view.get_first_child()
        while child is not None:
            box = child.get_first_child()
            if child.get_mapped() and hasattr(box, "song_row"):
                h = child.get_height()
                heights[h] = heights.get(h, 0) + 1
                headers += box.header.get_visible()
            child = child.get_next_sibling()
        return heights, headers

    def steps():
        win = application.props.active_window
        win.set_size_request(1200, 800)
        yield 1500
        adj = win.queue_view.get_vadjustment()
        check("starts not compact", not win.queue_compact and not win.queue_view.has_css_class("queue-compact"))
        if win.lookup_action("queue-grouped").get_state().get_boolean():
            win.activate_action("win.queue-grouped", None)
            yield 600
        sizes = {}
        for grouped in (False, True):
            if grouped:
                win.activate_action("win.queue-grouped", None)
                yield 600
            for compact in (False, True, False, True):
                if compact != win.queue_compact:
                    win.activate_action("win.queue-compact", None)
                    yield 500
                mode = f"{'grouped' if grouped else 'ungrouped'} {'compact' if compact else 'normal'}"
                adj.set_value(0)
                yield 300
                n = win.queue_sel.get_n_items()
                uppers, all_heights, saw_headers = set(), {}, 0
                for frac in (0, 0.3, 0.6, 0.95, 0.5):
                    adj.set_value(frac * (adj.get_upper() - adj.get_page_size()))
                    yield 200
                    uppers.add(round(adj.get_upper()))
                    hs, headers = row_heights(win)
                    saw_headers += headers
                    for h, c in hs.items():
                        all_heights[h] = all_heights.get(h, 0) + c
                check(f"{mode}: every row (tracks{' and headers' if grouped else ''}) equally tall",
                      len(all_heights) == 1 and (saw_headers > 0) == grouped,
                      f"heights {all_heights}, header rows seen {saw_headers}")
                # the pitch (row plus the list's own row padding) from the list's height
                row_h = next(iter(uppers)) / n
                check(f"{mode}: list height the same at every scroll position, a whole number of px per row",
                      len(uppers) == 1 and row_h == int(row_h), f"upper {sorted(uppers)}, {n} rows -> {row_h} px")
                sizes[mode] = row_h
                # switching modes in the middle of the list keeps the same rows in view
                adj.set_value(round(80 * row_h))
                yield 300
                top = adj.get_value() / row_h
                win.activate_action("win.queue-compact", None)
                yield 600
                top_after = adj.get_value() / (adj.get_upper() / n)
                check(f"{mode}: switching keeps the view on the same rows", abs(top_after - top) <= 0.5,
                      f"top row {top:.1f} -> {top_after:.1f}")
                if not compact:
                    shot(win, f"{'grouped' if grouped else 'ungrouped'}-compact")
                win.activate_action("win.queue-compact", None)  # back
                yield 600
                # at the top: switching stays at the top (the first album header visible)
                adj.set_value(0)
                yield 300
                win.activate_action("win.queue-compact", None)
                yield 600
                check(f"{mode}: at the top, switching stays at the top", adj.get_value() == 0,
                      f"scroll {adj.get_value():.0f}")
                if not compact:
                    shot(win, f"{'grouped' if grouped else 'ungrouped'}-compact-top")
                win.activate_action("win.queue-compact", None)  # back
                yield 600
            check(f"{'grouped' if grouped else 'ungrouped'}: compact rows are shorter",
                  0 < sizes[f"{'grouped' if grouped else 'ungrouped'} compact"]
                  < sizes[f"{'grouped' if grouped else 'ungrouped'} normal"],
                  str(sizes))
        for _ in range(2):
            win.activate_action("win.queue-compact", None)
            yield 100
            check("the choice is saved", win.settings.queue_compact is win.queue_compact, str(win.queue_compact))
        print("screenshots in", OUT)
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
