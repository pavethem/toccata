"""GTK smoke test: "Show the playing track" (win.queue-center, Ctrl+L) scrolls the
queue so the playing entry is in the middle - ungrouped, grouped, and inside a
collapsed album (which is expanded first; the other albums stay collapsed). Selection is left alone. Run with
tests/gtk/run_headless_mutter.sh (or broadway + dbus-run-session); exits 1 on failure."""
import argparse, os, sys, tempfile, threading
PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="toccata-smoke-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "cfg")
os.environ["XDG_CACHE_HOME"] = os.path.join(_TMP, "cache")
sys.path.insert(0, PROJECT)
from tests import search_mock
from tests.lazy_mock import render

# 20 albums of 10 tracks; entry id 150 (album 14, track 10) is playing
QUEUE = [dict(file=f"q/{a:02d}/{t:02d}.flac", Title=f"Track {t}", Artist=f"Artist {a}",
              AlbumArtist=f"Artist {a}", Album=f"Album {a}", Track=str(t), Time="200")
         for a in range(20) for t in range(1, 11)]
PLAYING = 150


class H(search_mock.Handler):
    def respond(self, a):
        cmd = a[0]
        if cmd == "idle":
            return ""
        if cmd in ("noidle", "replay_gain_status", "outputs"):
            return "OK\n"
        if cmd == "status":
            return (f"state: play\nsong: {PLAYING - 1}\nsongid: {PLAYING}\nplaylistlength: {len(QUEUE)}\n"
                    "volume: 50\nOK\n")
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
    from gi.repository import GLib
    from toccata import app as appmod
    appmod.APP_ID = "io.github.toccata.smoke"
    application = appmod.App(argparse.Namespace(host="127.0.0.1", port=srv.server_address[1], password=None,
                                                music_dir=None, no_covers=True, no_highlight=False))
    failures = []

    def check(name, ok, detail):
        print("ok  " if ok else "FAIL", name, detail, flush=True)
        if not ok:
            failures.append(name)

    def centre_of_playing(win):
        """y of the middle of the row showing the playing entry, and the page size."""
        song = win._queue_by_id[str(PLAYING)]
        widget = song.row
        if widget is None or not widget.get_mapped():
            return None, None
        ok, b = widget.compute_bounds(win.queue_view)
        return b.get_y() + b.get_height() / 2, win.queue_view.get_vadjustment().get_page_size()

    def steps():
        win = application.props.active_window
        win.set_size_request(1200, 800)
        yield 1500
        adj = win.queue_view.get_vadjustment()
        if win.lookup_action("queue-grouped").get_state().get_boolean():
            win.activate_action("win.queue-grouped", None)  # start ungrouped
            yield 600
        for mode in ("ungrouped", "grouped", "collapsed"):
            if mode == "grouped":
                win.activate_action("win.queue-grouped", None)
                yield 600
            if mode == "collapsed":
                win.activate_action("win.queue-collapse-all", None)
                yield 600
            adj.set_value(0)
            yield 400
            win.queue_sel.select_item(1, True)  # (row 0 is an album header when grouped)
            before = win.queue_sel.get_selection().get_size()
            win.activate_action("win.queue-center", None)
            yield 600
            mid, page = centre_of_playing(win)
            check(f"{mode}: playing row in the middle", mid is not None and abs(mid - page / 2) < 30,
                  f"row centre {mid if mid is None else round(mid)} of {page} (scroll {adj.get_value():.0f})")
            if mode == "collapsed":
                song = win._queue_by_id[str(PLAYING)]
                others = [g for g in win._queue_groups if g is not song.group]
                check("collapsed: the playing album is expanded", song.group.head_id not in win.collapsed_groups
                      and not song.hidden and not song.summary, "")
                check("collapsed: the other albums stay collapsed",
                      all(g.head_id in win.collapsed_groups for g in others), "")
            check(f"{mode}: selection untouched", win.queue_sel.is_selected(1)
                  and win.queue_sel.get_selection().get_size() == before, "")
            yield 700  # the pin must not take it back to the top
            check(f"{mode}: stays there", adj.get_value() > 0, f"scroll {adj.get_value():.0f}")
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
