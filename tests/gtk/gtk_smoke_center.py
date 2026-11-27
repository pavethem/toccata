"""GTK smoke test: finding an artist (locate_album) scrolls it into the middle of the
artist list. Run like gtk_smoke.py (broadway + dbus-run-session)."""
import argparse, os, sys, tempfile, threading
PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="toccata-smoke-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "cfg"); os.environ["XDG_CACHE_HOME"] = os.path.join(_TMP, "cache")
sys.path.insert(0, PROJECT)
from tests import lazy_mock
db = lazy_mock.genre_db()
for i in range(300):
    db.append({"file": f"x/{i}.flac", "Title": "t", "Artist": f"Filler {i:03d}", "AlbumArtist": f"Filler {i:03d}", "Album": "A"})
db.append({"file": "z/1.flac", "Title": "t", "Artist": "Zed", "AlbumArtist": "Zed", "Album": "Last"})
srv, port = lazy_mock.start(db=db)
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk
from toccata import app as appmod
appmod.APP_ID = "io.github.toccata.smoke"
application = appmod.App(argparse.Namespace(host="127.0.0.1", port=port, password=None, music_dir=None, no_covers=True, no_highlight=False))
def where(win, name):
    names = [win.artist_store.get_string(i) for i in range(win.artist_store.get_n_items())]
    i = names.index(name); adj = win.art_view.get_vadjustment(); row = adj.get_upper() / len(names)
    mid = (i + 0.5) * row - adj.get_value()
    return f"{name}: row centre at {mid:.0f}px of {adj.get_page_size():.0f}px visible"
def steps():
    win = application.props.active_window
    win.set_default_size(1000, 700)
    yield 2000
    for name in ("Filler 150", "Band", "Zed"):
        win.locate_album(appmod.albumslib.AlbumEntry(name, "A"))
        yield 1200
        print(where(win, name), flush=True)
    application.quit()
gen = None
def tick():
    try: GLib.timeout_add(next(gen), tick)
    except StopIteration: pass
    except Exception:
        import traceback; traceback.print_exc(); application.quit()
    return False
def act(_a):
    global gen
    if gen is None:
        gen = steps(); GLib.timeout_add(10, tick)
application.connect("activate", act)
application.run([sys.argv[0]])
