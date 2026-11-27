"""GTK smoke test: after a database update the artist list selects (and centres) the
artist of the newest song the update added, with that album selected, and leaves
the keyboard focus alone (user request 2026-11-20: "after adding a new album the
artist view always jumps to the bottom"). An update that added nothing keeps the
artist on screen. The update is simulated: the mock's database grows, then the
DbReloadGate is opened as the `database` idle event would. Run like gtk_smoke.py
(or tests/gtk/run_headless_mutter.sh); exits 1 on failure."""
import argparse, os, sys, tempfile
PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="toccata-smoke-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "cfg"); os.environ["XDG_CACHE_HOME"] = os.path.join(_TMP, "cache")
sys.path.insert(0, PROJECT)
from tests import lazy_mock
from tests.lazy_mock import LazyHandler

OLD = "2020-01-01T00:00:00Z"
db = lazy_mock.genre_db()
for s in db:
    s["Last-Modified"] = OLD
for i in range(2000):
    db.append({"file": f"x/{i}.flac", "Title": "t", "Artist": f"Filler {i:04d}", "AlbumArtist": f"Filler {i:04d}",
               "Album": "A", "Last-Modified": OLD})
srv, port = lazy_mock.start(db=db, db_update=1577923200)  # 2020-01-02: the last update before the test
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import GLib
from toccata import app as appmod
appmod.APP_ID = "io.github.toccata.smoke"
application = appmod.App(argparse.Namespace(host="127.0.0.1", port=port, password=None, music_dir=None,
                                            no_covers=True, no_highlight=False))
failures = []


def check(name, got, want):
    ok = got == want
    print("ok  " if ok else "FAIL", name, got, "" if ok else f"(wanted {want})", flush=True)
    if not ok:
        failures.append(name)


def centred(win, name):
    """Is the artist's row in the middle of the visible list (within a row)?"""
    n = win.art_sel.get_n_items()
    names = [win.artist_store.get_string(i) for i in range(n)]
    adj = win.art_view.get_vadjustment()
    row = adj.get_upper() / n
    mid = (names.index(name) + 0.5) * row - adj.get_value()
    return abs(mid - adj.get_page_size() / 2) <= row


def selected_albums(win):
    return [r.album.title for r in win._sel_albums]


def update(win, songs, stamp):
    LazyHandler.db = LazyHandler.db + songs
    LazyHandler.db_update = stamp  # MPD's new db_update once the update is over
    win._db_gate.changed()
    win._reload_if_db_settled()


def steps():
    win = application.props.active_window
    win.set_default_size(1000, 700)
    yield 2000
    win.locate_album(appmod.albumslib.AlbumEntry("Filler 0300", "A"))
    yield 1200
    check("start: Filler 0300 selected", win._selected_artist, "Filler 0300")
    win.queue_view.grab_focus()  # somewhere that survives the reload (album rows do not)
    yield 200
    focus = win.get_focus()
    check("start: focus in the queue", focus is not None and focus.is_ancestor(win.queue_view) or focus is win.queue_view, True)
    # an update adds two albums; "Filler 1700"'s is the newest
    update(win, [
        {"file": "n/1.flac", "Title": "t", "AlbumArtist": "Newcomer", "Album": "Older New",
         "Last-Modified": "2026-11-20T10:00:00Z"},
        {"file": "n/2.flac", "Title": "t", "AlbumArtist": "Filler 1700", "Album": "Fresh",
         "Last-Modified": "2026-11-20T10:05:00Z"}], 1795169200)
    yield 1500
    check("after update: newest album's artist selected", win._selected_artist, "Filler 1700")
    check("after update: list selection", win.artist_store.get_string(win.art_sel.get_selected()), "Filler 1700")
    check("after update: artist centred", centred(win, "Filler 1700"), True)
    check("after update: new album selected", selected_albums(win), ["Fresh"])
    check("after update: remembered", win.settings.last_artist, "Filler 1700")
    check("after update: focus unchanged", win.get_focus() is focus, True)
    # the next update is compared with the new stamp: nothing added since -> stay
    win.locate_album(appmod.albumslib.AlbumEntry("Filler 0050", "A"))
    yield 1200
    update(win, [], 1795169300)
    yield 1500
    check("update without new songs: artist kept", win._selected_artist, "Filler 0050")
    check("update without new songs: centred", centred(win, "Filler 0050"), True)
    # a brand-new artist further down
    update(win, [{"file": "n/3.flac", "Title": "t", "AlbumArtist": "Zz Brandnew", "Album": "Debut",
                  "Last-Modified": "2026-11-20T10:10:00Z"}], 1795169400)
    yield 1500
    check("new artist selected", win._selected_artist, "Zz Brandnew")
    check("new artist centred or at the end", centred(win, "Zz Brandnew") or
          win.art_view.get_vadjustment().get_value() + win.art_view.get_vadjustment().get_page_size()
          >= win.art_view.get_vadjustment().get_upper() - 1, True)
    check("new artist's album selected", selected_albums(win), ["Debut"])
    # an update while another view is shown: the list cannot scroll then; back in the
    # library the new artist must be centred, not left off-screen (2026-11-27)
    win.locate_album(appmod.albumslib.AlbumEntry("Filler 0300", "A"))
    yield 1200
    win.views.set_visible_child_name("albums")
    yield 300
    update(win, [{"file": "n/4.flac", "Title": "t", "AlbumArtist": "Filler 1500", "Album": "Away",
                  "Last-Modified": "2026-11-20T10:15:00Z"}], 1795169500)
    yield 1500
    check("update in another view: newest artist selected", win._selected_artist, "Filler 1500")
    win.views.set_visible_child_name("library")
    yield 800
    check("back in the library: artist centred", centred(win, "Filler 1500"), True)
    # a plain reload (e.g. a genre switch) keeps the artist
    win.locate_album(appmod.albumslib.AlbumEntry("Filler 1000", "A"))
    yield 1200
    win.reload_library(fresh=False)
    yield 1200
    check("plain reload: artist kept", win._selected_artist, "Filler 1000")
    application.quit()


gen = None


def tick():
    try:
        GLib.timeout_add(next(gen), tick)
    except StopIteration:
        pass
    except Exception:
        import traceback
        traceback.print_exc()
        failures.append("exception")
        application.quit()
    return False


def act(_a):
    global gen
    if gen is None:
        gen = steps()
        GLib.timeout_add(10, tick)


application.connect("activate", act)
application.run([sys.argv[0]])
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
