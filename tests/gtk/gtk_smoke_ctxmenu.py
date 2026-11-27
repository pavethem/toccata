"""GTK smoke test: right-click menus in the Artists and Albums views, and Set Cover
(written into a temporary music folder, then shown). Run like gtk_smoke.py
(broadway + dbus-run-session). The file chooser itself is not driven."""
import argparse, os, sys, tempfile, threading
PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="toccata-smoke-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "cfg")
os.environ["XDG_CACHE_HOME"] = os.path.join(_TMP, "cache")
MUSIC = os.path.join(_TMP, "music")
sys.path.insert(0, PROJECT)
from tests import lazy_mock
from tests.lazy_mock import LazyHandler

QUEUE = []


class H(LazyHandler):
    def respond(self, a):
        cmd = a[0]
        if cmd == "idle":
            return ""
        if cmd in ("noidle", "currentsong", "replay_gain_status", "tagtypes", "outputs", "listplaylists",
                   "albumart", "readpicture"):
            return "OK\n" if cmd not in ("albumart", "readpicture") else "ACK [50@0] {albumart} No file exists\n"
        if cmd == "playlistinfo":
            return "".join(f"file: {u}\nId: {i + 1}\nPos: {i}\n" for i, u in enumerate(QUEUE)) + "OK\n"
        if cmd == "add":
            QUEUE.append(a[1])
            return "OK\n"
        return super().respond(a)


def main():
    db = lazy_mock.genre_db()
    for s in db:  # the songs' folders exist in the music folder
        os.makedirs(os.path.join(MUSIC, os.path.dirname(s["file"])), exist_ok=True)
    srv, _ = lazy_mock.start(db=db)
    srv.shutdown(); srv.server_close()
    srv = lazy_mock.LazyServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    import gi
    gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GLib, GdkPixbuf
    from toccata import app as appmod
    appmod.APP_ID = "io.github.toccata.smoke"
    captured = []
    real_menu = appmod._context_menu
    appmod._context_menu = lambda widget, x, y, entries: (captured.append(entries), real_menu(widget, x, y, entries))[1]
    application = appmod.App(argparse.Namespace(host="127.0.0.1", port=srv.server_address[1], password=None,
                                                music_dir=MUSIC, no_covers=False, no_highlight=False))
    report = []
    pb = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 64, 64)
    pb.fill(0xcc3333ff)
    png = bytes(pb.save_to_bufferv("png", [], [])[1])

    def cover_win():  # the "Set Cover" search is its own (resizable) window
        from gi.repository import Gtk
        return next((w for w in Gtk.Window.list_toplevels()
                     if isinstance(w, appmod.CoverSearchDialog) and w.get_visible()), None)

    def labels():
        return [e[0] if e else "---" for e in captured[-1]]

    def run(label):
        next(e for e in captured[-1] if e and e[0] == label)[1]()

    def steps():
        win = application.props.active_window
        yield 1500
        names = [win.artist_store.get_string(i) for i in range(win.artist_store.get_n_items())]
        win.art_sel.set_selected(names.index("Band"))
        yield 1000
        rows = win._album_rows()
        debut = rows[0]
        win._album_menu(debut, 10, 10)
        report.append(("album menu", labels(), "selected", [r.album.title for r in win._sel_albums]))
        run("Add to Queue")
        yield 600
        report.append(("Add to Queue from the menu", list(QUEUE)))
        # Search Cover Online, with the network replaced by canned answers
        from toccata import coversearch
        good = coversearch.Candidate("Deezer", "Band", "Debut", "thumb://ok", "full://ok", "1000 px")
        bad = coversearch.Candidate("Cover Art Archive", "Band", "Debut", "thumb://missing", "full://missing")
        coversearch.search = lambda q, a="", b="", **k: ([bad, good], [])
        def fake_get(url, **k):
            if url == "thumb://missing":
                raise OSError("404")
            return png
        coversearch.http_get = fake_get
        win._album_menu(debut, 10, 10)
        report.append(("album menu with online search", labels()))
        run("Set Cover\u2026")
        yield 1500
        dlg = cover_win()
        tiles = []
        c = dlg.flow.get_first_child() if dlg else None
        while c is not None:
            tiles.append(c.get_child().candidate.provider)
            c = c.get_next_sibling()
        report.append(("search dialog", type(dlg).__name__ if dlg else None, "query", dlg.entry.get_text() if dlg else None,
                       "results left after thumbnails", tiles))
        report.append(("dialog title", dlg.get_title(), "has Choose File", dlg.on_file is not None))
        # double-click (child-activated) previews the full image; its button uses it
        fulls = []
        real_get = coversearch.http_get
        coversearch.http_get = lambda url, **k: (fulls.append(url), real_get(url, **k))[1]
        dlg.flow.emit("child-activated", dlg.flow.get_child_at_index(0))
        yield 800
        prev = dlg.get_visible_dialog()
        report.append(("search window", "resizable", dlg.get_resizable(), "modal", dlg.get_modal(),
                       "transient for main", dlg.get_transient_for() is win, "size", (dlg.get_width(), dlg.get_height())))
        report.append(("preview", type(prev).__name__, "selected", [c.get_index() for c in dlg.flow.get_selected_children()],
                       "shown", prev.stack.get_visible_child_name(), "subtitle", prev.title.get_subtitle(),
                       "use enabled", prev.use.get_sensitive(), "downloads", list(fulls)))
        from gi.repository import Gtk
        paint = Gtk.WidgetPaintable.new(dlg); snap = Gtk.Snapshot()  # the search window, preview on top
        paint.snapshot(snap, dlg.get_width(), dlg.get_height())
        shot = os.path.join(_TMP, "cover-preview.png")
        dlg.get_native().get_renderer().render_texture(snap.to_node(), None).save_to_png(shot)
        print("screenshot", shot, flush=True)
        prev._use()
        yield 1500
        report.append(("used from preview: full image downloaded once", list(fulls)))
        folders0 = appmod.album_folders(MUSIC, [t["file"] for t in debut.album.tracks])
        report.append(("after Use This Cover", "dialog open", cover_win() is not None,
                       "files", sorted(os.listdir(folders0[0])), "thumb", debut.thumb.get_visible_child_name()))
        for f in folders0:  # back to no cover for the Set Cover part below
            for n in os.listdir(f):
                os.remove(os.path.join(f, n))
        win.covers_changed({os.path.relpath(f, MUSIC) for f in folders0})
        yield 800
        # "Choose File..." closes the search and opens the file chooser (set_cover)
        opened = []
        real_set_cover = win.set_cover
        win.set_cover = lambda tracks, title: opened.append(title)
        win.search_cover_online(debut.album.tracks, debut.album.title)
        yield 800
        d2 = cover_win()
        d2._choose_file()
        yield 400
        report.append(("Choose File", "search closed", cover_win() is None, "file chooser for", opened))
        win.set_cover = real_set_cover
        # Last.fm: the key from Preferences reaches the search, and the dialog says so
        seen_keys = []
        coversearch.search = lambda q, a="", b="", lastfm_key="", **k: (seen_keys.append(lastfm_key), ([good], []))[1]
        prefs = appmod.SettingsDialog(win)
        report.append(("prefs has Last.fm field", hasattr(prefs, "lastfm_row"), "empty", prefs.lastfm_row.get_text() == ""))
        prefs.lastfm_row.set_text(" abc123DEF ")
        prefs._on_closed(prefs)
        yield 500
        report.append(("saved key", win.settings.lastfm_api_key))
        win.search_cover_online(debut.album.tracks, debut.album.title)
        yield 800
        dlg = cover_win()
        report.append(("dialog note", dlg.note.get_text() if dlg else None, "key used", seen_keys[-1:] ))
        if dlg:
            dlg.close()
        yield 300
        prefs = appmod.SettingsDialog(win)
        prefs.lastfm_row.set_text("not a key!")
        prefs._on_closed(prefs)
        yield 300
        report.append(("garbage key rejected", repr(win.settings.lastfm_api_key)))
        # no own key: Toccata's built-in key is used, and Last.fm is listed as a source
        appmod.lastfm.DEFAULT_API_KEY = "builtin0"
        win.search_cover_online(debut.album.tracks, debut.album.title)
        yield 800
        dlg = cover_win()
        report.append(("built-in key", seen_keys[-1:], "no 'add your key' hint",
                       bool(dlg) and "add your API key" not in dlg.note.get_text()))
        if dlg:
            dlg.close()
        yield 300
        # Set Cover, after the file chooser: write into the album folder and show it
        uris = [t["file"] for t in debut.album.tracks]
        folders = appmod.album_folders(MUSIC, uris)
        report.append(("folders", [os.path.relpath(f, MUSIC) for f in folders], "thumb before", debut.thumb.get_visible_child_name()))
        win._write_cover(uris, folders, png, debut.album.title)
        yield 2000
        report.append(("after Set Cover", sorted(os.listdir(folders[0])), "thumb", debut.thumb.get_visible_child_name()))
        win._write_cover(uris, folders, png, debut.album.title)  # again: the old one is kept
        yield 500
        report.append(("set again", sorted(os.listdir(folders[0]))))
        # track menu
        win.album_click(debut, 1); win.album_click(debut, 2)
        yield 1500
        win._track_row_menu(debut.track_rows[1], 5, 5)
        report.append(("track menu", labels(), "selected", [r.track["title"] for r in win._sel_tracks]))
        # artist menu (on a real artist row label)
        from gi.repository import Gtk
        stack, label = [win.art_view], None
        while stack:
            w = stack.pop()
            if isinstance(w, Gtk.Label) and getattr(w, "artist", None) == "Hornist":
                label = w
                break
            c = w.get_first_child()
            while c is not None:
                stack.append(c)
                c = c.get_next_sibling()
        if label is not None:
            win._artist_menu(label, 5, 5)
            report.append(("artist menu", labels()))
            run("Add to Queue")
            yield 800
            report.append(("artist Add to Queue", QUEUE[-3:]))
        # Albums view tile menu: its covers come from the same folder
        win.views.set_visible_child_name("albums")
        yield 2500
        tile_item = next((win.albums_store.get_item(i) for i in range(win.albums_store.get_n_items())
                          if win.albums_store.get_item(i).entry.album == "Debut"), None)
        if tile_item is not None and tile_item.tile is not None:
            win._album_tile_menu(tile_item.tile, 5, 5)
            report.append(("album tile menu", labels(), "tile cover", tile_item.tile.thumb.get_visible_child_name()))
        else:
            report.append(("album tile for Debut not on screen", win.albums_store.get_n_items()))
        # every menu gets its full natural size, also on a plain label (artist row)
        win.views.set_visible_child_name("library")
        yield 500
        from gi.repository import Gtk
        stack, label = [win.art_view], None
        while stack:
            w = stack.pop()
            if isinstance(w, Gtk.Label) and getattr(w, "artist", None):
                label = w
                break
            c = w.get_first_child()
            while c is not None:
                stack.append(c)
                c = c.get_next_sibling()
        if label is not None:
            pop = real_menu(label, 10, 5, win._track_menu_entries(lambda fn: None, label.artist))
            yield 800
            natural = pop.measure(Gtk.Orientation.VERTICAL, pop.get_width())[1]
            report.append(("artist menu size", pop.get_height(), "natural", natural, "fits", pop.get_height() >= natural))
            pop.popdown()
            yield 300
        # search results: right-click menu for the selection (or the clicked row)
        songs = [appmod.Song({"file": f"s/{n}.flac", "title": f"S{n}", "artist": "Band", "albumartist": "Band", "album": "Debut"}) for n in range(3)]
        win.views.set_visible_child_name("search")
        win.search_store.splice(0, win.search_store.get_n_items(), songs)
        win.search_stack.set_visible_child_name("results")
        yield 500
        appmod._select_like_click(win.search_view, 0)
        appmod._select_like_click(win.search_view, 1, extend=True)
        yield 200
        win._search_menu(songs[0], win.search_view, 10, 10)
        report.append(("search menu", labels()))
        QUEUE.clear()
        run("Add to Queue")
        yield 600
        report.append(("search menu Add on selection", list(QUEUE)))
        win._search_menu(songs[2], win.search_view, 10, 10)
        yield 200
        report.append(("search right-click outside selection selects", [s.data["file"] for s in win._search_selected()]))
        win._search_menu(songs[0], win.search_view, 10, 10)
        run("Show in Artists")
        yield 1200
        report.append(("search Show in Artists", win.views.get_visible_child_name(),
                       win.art_sel.get_selected_item().get_string(), [r.album.title for r in win._sel_albums]))
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
