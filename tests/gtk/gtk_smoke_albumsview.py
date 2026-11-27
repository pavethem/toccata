"""GTK smoke test: the Albums view (recently added, sorting, actions, Show in
Artists, drag to the queue) against tests/albums_mock.py. Run like gtk_smoke.py
(broadway + dbus-run-session)."""
import argparse, os, sys, tempfile, threading, time
PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="toccata-smoke-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "cfg")
os.environ["XDG_CACHE_HOME"] = os.path.join(_TMP, "cache")
sys.path.insert(0, PROJECT)
from tests import albums_mock, lazy_mock

QUEUE = []


class H(albums_mock.Handler):
    def respond(self, a):
        cmd = a[0]
        if cmd == "idle":
            return ""
        if cmd in ("noidle", "currentsong", "replay_gain_status", "tagtypes", "outputs", "listplaylists"):
            return "OK\n"
        if cmd == "playlistinfo":
            return "".join(f"file: {u}\nId: {i + 1}\nPos: {i}\n" for i, u in enumerate(QUEUE)) + "OK\n"
        if cmd == "add":
            if len(a) == 3:
                QUEUE.insert(int(a[2]), a[1])
            else:
                QUEUE.append(a[1])
            return "OK\n"
        return super().respond(a)


def main():
    albums_mock.start()  # resets the shared mock state
    srv = lazy_mock.LazyServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    import gi
    gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
    from gi.repository import GLib
    from toccata import app as appmod
    appmod.APP_ID = "io.github.toccata.smoke"
    application = appmod.App(argparse.Namespace(host="127.0.0.1", port=srv.server_address[1], password=None,
                                                music_dir=None, no_covers=True, no_highlight=False))
    report = []

    def steps():
        win = application.props.active_window
        yield 1500
        win.views.set_visible_child_name("albums")
        yield 1500
        def titles(n=5):
            return [win.albums_store.get_item(i).entry.album for i in range(min(n, win.albums_store.get_n_items()))]
        report.append(("recently added", titles(), win.albums_count.get_text()))
        # walk to the end (the mock is small): everything, each album once
        for _ in range(20):
            if win._albums_recent.done:
                break
            win._albums_upper_at_batch = None
            win._albums_batch_time = 0
            win._albums_load_more()
            yield 200
        report.append(("all loaded", win.albums_count.get_text()))
        win.albums_sort.set_selected(1)  # Artist
        yield 800
        report.append(("by artist", titles(3), win.albums_count.get_text(), "setting", win.settings.albums_sort))
        win.albums_sort.set_selected(2)  # Title
        yield 500
        report.append(("by title", titles(3)))
        # select "Mix" and add it to the queue
        idx = next(i for i in range(win.albums_store.get_n_items()) if win.albums_store.get_item(i).entry.album == "Mix")
        win.albums_sel.select_item(idx, True)
        yield 200
        report.append(("buttons", win.a_add.get_label(), win.a_add.get_sensitive()))
        win.a_add.emit("clicked")
        yield 800
        report.append(("queue after Add", list(QUEUE)))
        # drag "Fresh" onto the start of the queue
        fresh = next(win.albums_store.get_item(i) for i in range(win.albums_store.get_n_items())
                     if win.albums_store.get_item(i).entry.album == "Fresh")
        win.albums_sel.unselect_all()
        win._drag_albums(fresh)
        win.insert_albums(win._drag[1], 0)
        yield 800
        report.append(("queue after dropping Fresh at 0", list(QUEUE)))
        # Show in Artists
        win.albums_sel.select_item(idx, True)
        win.activate_action("win.albums-locate", None)
        yield 1500
        report.append(("Show in Artists", win.views.get_visible_child_name(), win.art_sel.get_selected_item().get_string(),
                       "selected album", [r.album.title for r in win._sel_albums]))
        # right-click menu on a tile
        captured = []
        real_menu = appmod._context_menu
        appmod._context_menu = lambda w, x, y, entries: (captured.append(entries), real_menu(w, x, y, entries))[1]
        win.albums_sel.unselect_all()
        bound = next((win.albums_store.get_item(i) for i in range(win.albums_store.get_n_items())
                      if win.albums_store.get_item(i).tile is not None), None)
        if bound is not None:
            win._album_tile_menu(bound.tile, 5, 5)
            report.append(("tile menu", [e[0] if e else "---" for e in captured[-1]],
                           "selects it", [win.albums_store.get_item(win.albums_sel.get_selection().get_nth(0)).entry.album],
                           "was", bound.entry.album))
            before = len(QUEUE)
            next(e for e in captured[-1] if e and e[0] == "Add to Queue")[1]()
            yield 800
            report.append(("tile menu Add to Queue", len(QUEUE) - before, "tracks added"))
        else:
            report.append(("no tile bound",))
        appmod._context_menu = real_menu
        # type-ahead in the grid
        from gi.repository import Gdk
        win.views.set_visible_child_name("albums")
        yield 300
        win.albums_grid.grab_focus()
        yield 200
        for ch in "record 2":
            win._on_library_key(None, Gdk.unicode_to_keyval(ord(ch)), 0, Gdk.ModifierType(0))
        yield 400
        bits = win.albums_sel.get_selection()
        report.append(("type-ahead 'record 2'", [win.albums_store.get_item(bits.get_nth(i)).entry.album for i in range(bits.get_size())],
                       "label", win.albums_typeahead_label.get_text()))
        for line in report:
            print(*line, sep=" | ")
        application.quit()

    gen = None

    def tick():
        nonlocal gen
        try:
            GLib.timeout_add(next(gen), tick)
        except StopIteration:
            pass
        except Exception:
            import traceback
            traceback.print_exc()
            application.quit()
        return False

    def act(_a):
        nonlocal gen
        if gen is None:
            gen = steps()
            GLib.timeout_add(10, tick)
    application.connect("activate", act)
    application.run([sys.argv[0]])


main()
