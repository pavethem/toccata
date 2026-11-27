"""GTK smoke test: the queue's right-click menu (rows and album headers):
Edit Tags on the selection, Show in Artists / Albums, Remove. Run like gtk_smoke.py
(broadway + dbus-run-session)."""
import argparse, os, sys, tempfile, threading
PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="toccata-smoke-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "cfg")
os.environ["XDG_CACHE_HOME"] = os.path.join(_TMP, "cache")
sys.path.insert(0, PROJECT)
from tests import albums_mock, lazy_mock
from tests.lazy_mock import render

DB = lazy_mock.genre_db()
QUEUE = [s for s in DB if s.get("Album") in ("Blue", "Debut")]  # Hornist - Blue (2), Band - Debut ...
SENT = []


class H(albums_mock.Handler):  # knows `list album group ...` (the Albums view)
    def respond(self, a):
        cmd = a[0]
        if cmd == "idle":
            return ""
        if cmd in ("noidle", "currentsong", "replay_gain_status", "tagtypes", "outputs", "listplaylists"):
            return "OK\n"
        if cmd in ("albumart", "readpicture"):
            return "ACK [50@0] {albumart} No file exists\n"
        if cmd == "playlistinfo":
            return "".join(render(s) + f"Id: {i + 1}\nPos: {i}\n" for i, s in enumerate(QUEUE)) + "OK\n"
        if cmd == "deleteid":
            SENT.append(("deleteid", a[1]))
            return "OK\n"
        return super().respond(a)


def main():
    srv, _ = albums_mock.start()  # sets up the mock's class state ...
    srv.shutdown(); srv.server_close()
    H.db = DB  # ... with this test's library
    srv = lazy_mock.LazyServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    import gi
    gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
    from gi.repository import GLib
    from toccata import app as appmod
    appmod.APP_ID = "io.github.toccata.smoke"
    captured = []
    real_menu = appmod._context_menu
    appmod._context_menu = lambda widget, x, y, entries: (captured.append(entries), real_menu(widget, x, y, entries))[1]
    application = appmod.App(argparse.Namespace(host="127.0.0.1", port=srv.server_address[1], password=None,
                                                music_dir=None, no_covers=True, no_highlight=False))
    report = []

    def labels():
        return [e[0] if e else "---" for e in captured[-1]]

    def run(label):
        next(e for e in captured[-1] if e and e[0] == label)[1]()

    def steps():
        win = application.props.active_window
        edited = []
        win.edit_tags = lambda songs: edited.append([s.data.get("title") for s in songs])
        yield 1500
        songs = list(win._queue_songs)
        report.append(("queue", [s.data.get("title") for s in songs]))
        # right-click a track outside the selection: it gets selected, the menu acts on it
        win._queue_menu(songs[2], win.queue_view, 10, 10)
        report.append(("track menu", labels(), "selected", [s.data.get("title") for s in win._queue_selected()]))
        run("Edit Tags…")
        report.append(("Edit Tags got", edited[-1]))
        # two selected, right-click one of them: both
        win.queue_sel.select_item(0, True)
        win.queue_sel.select_item(1, False)
        win._queue_menu(songs[1], win.queue_view, 10, 10)
        report.append(("2 selected menu", labels()))
        run("Edit Tags…")
        report.append(("Edit Tags got", edited[-1]))
        # Show in Artists
        run("Show in Artists")
        yield 1500
        sel = win.art_sel.get_selected()
        report.append(("Show in Artists: view", win.views.get_visible_child_name(),
                       "artist", win.artist_store.get_string(sel),
                       "album selected", [r.album.title for r in getattr(win, "_sel_albums", [])]))
        # Show in Albums (Hornist - Blue)
        win._queue_menu(songs[0], win.queue_view, 10, 10)
        run("Show in Albums")
        yield 2500
        item = win.albums_sel.get_selected_item() if hasattr(win.albums_sel, "get_selected_item") else None
        if item is None:
            bits = win.albums_sel.get_selection()
            item = win.albums_store.get_item(bits.get_nth(0)) if bits.get_size() else None
        report.append(("Show in Albums: view", win.views.get_visible_child_name(), "sort", win._albums_sort_key(),
                       "selected", item.entry.key if item else None))
        # the same from a search result's menu (Hornist - Blue: songs[0])
        win.albums_sel.unselect_all()
        win.views.set_visible_child_name("search")
        win.search_store.splice(0, win.search_store.get_n_items(), [appmod.Song(dict(songs[0].data))])
        win._search_menu(win.search_store.get_item(0), win.search_view, 10, 10)
        report.append(("search menu", labels()))
        run("Show in Albums")
        yield 1500
        bits = win.albums_sel.get_selection()
        report.append(("search Show in Albums: view", win.views.get_visible_child_name(), "selected",
                       [win.albums_store.get_item(bits.get_nth(i)).entry.key for i in range(bits.get_size())]))
        win.views.set_visible_child_name("search")
        win._search_menu(win.search_store.get_item(0), win.search_view, 10, 10)
        run("Show in Artists")
        yield 1500
        report.append(("search Show in Artists: view", win.views.get_visible_child_name(),
                       "artist", win.artist_store.get_string(win.art_sel.get_selected()),
                       "album selected", [r.album.title for r in getattr(win, "_sel_albums", [])]))
        # "Recently added" in batches of one song: an album outside the loaded part
        # switches to artist sorting; one inside keeps the sort
        base = appmod.albumslib.RecentAlbums
        appmod.albumslib.RecentAlbums = lambda field, now=None, chunk=1: base(field, now, chunk=1)
        win.albums_sort.set_selected(0)
        yield 1500
        loaded = [e.key for e in win._albums_entries]
        def selected():
            bits = win.albums_sel.get_selection()
            return win.albums_store.get_item(bits.get_nth(0)).entry.key if bits.get_size() else None
        win.views.set_visible_child_name("library")
        win.show_song_in_albums({"albumartist": loaded[0][0], "album": loaded[0][1]})
        yield 1500
        report.append(("loaded first", loaded[0], "-> sort", win._albums_sort_key(), "selected", selected()))
        toasts = []
        real_toast = win.show_toast
        win.show_toast = lambda t, *a, **k: (toasts.append(t), real_toast(t, *a, **k))
        win.views.set_visible_child_name("library")
        win.show_song_in_albums({"albumartist": "Nobody", "album": "Nowhere"})  # in no list
        yield 2500
        report.append(("unknown album: sort", win._albums_sort_key(), "selected", selected(),
                       "pending", win._albums_pending, "toasts", toasts))
        win.show_toast = real_toast
        appmod.albumslib.RecentAlbums = base
        # Remove two selected
        win.views.set_visible_child_name("library")
        win.queue_sel.select_item(0, True)
        win.queue_sel.select_item(1, False)
        win._queue_menu(songs[0], win.queue_view, 10, 10)
        report.append(("remove label", labels()[-1]))
        run(labels()[-1])
        yield 500
        report.append(("Remove sent", list(SENT)))
        # grouped queue: right-click an album header
        win.activate_action("win.queue-grouped", None)
        yield 800
        g = win._queue_groups[0]
        win.header_menu(g, win.queue_view, 10, 10)
        run("Edit Tags…")
        report.append(("header", g.title, "menu", labels(), "Edit Tags got", edited[-1]))
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
