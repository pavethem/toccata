"""GTK smoke test: maximized and windowed keep their own divider positions (user request
2026-11-13, "the maximized layout should not be affected when I window the app").
Needs a window manager that maximizes: run with tests/gtk/run_headless_mutter.sh.
Exits 1 on failure."""
import argparse, os, sys, tempfile
PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="toccata-smoke-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "cfg"); os.environ["XDG_CACHE_HOME"] = os.path.join(_TMP, "cache")
sys.path.insert(0, PROJECT)
from tests import lazy_mock
srv, port = lazy_mock.start(db=lazy_mock.genre_db())
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import GLib
from toccata import app as appmod
appmod.APP_ID = "io.github.toccata.smoke"
application = appmod.App(argparse.Namespace(host="127.0.0.1", port=port, password=None, music_dir=None,
                                            no_covers=True, no_highlight=False))
failures = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""), flush=True)
    if not ok:
        failures.append(name)


def steps():
    win = application.props.active_window
    s = win.settings
    yield 1500
    check("starts windowed", not win.is_maximized(), f"{win.get_width()}x{win.get_height()}")
    # "User drags" while windowed. A drag is several moves: UserDragFilter never counts a
    # divider's first notification. One divider per frame: GTK clamps the inner divider
    # against the last allocation.
    win.main_split.set_position(649); win.main_split.set_position(650)
    yield 300
    win.library_split.set_position(199); win.library_split.set_position(200)
    yield 300
    check("windowed drag saved as windowed", (s.main_split, s.artist_split) == (650, 200),
          f"{s.main_split}, {s.artist_split}")
    win.maximize()
    yield 1500
    check("maximized", win.is_maximized(), f"{win.get_width()}x{win.get_height()}")
    check("maximized falls back to the windowed layout at first", win.main_split.get_position() == 650,
          str(win.main_split.get_position()))
    win.main_split.set_position(1099); win.main_split.set_position(1100)  # drags while maximized
    yield 300
    win.library_split.set_position(299); win.library_split.set_position(300)
    yield 300
    check("maximized drag saved as maximized", (s.main_split_max, s.artist_split_max) == (1100, 300),
          f"{s.main_split_max}, {s.artist_split_max}")
    check("windowed values untouched", (s.main_split, s.artist_split) == (650, 200), f"{s.main_split}, {s.artist_split}")
    win.unmaximize()
    yield 1500
    check("windowed again", not win.is_maximized(), f"{win.get_width()}x{win.get_height()}")
    check("windowed layout restored", (win.main_split.get_position(), win.library_split.get_position()) == (650, 200),
          f"{win.main_split.get_position()}, {win.library_split.get_position()}")
    win.main_split.set_position(599); win.main_split.set_position(600)  # drag while windowed
    yield 300
    check("maximized values untouched by windowed drag", (s.main_split_max, s.artist_split_max) == (1100, 300),
          f"{s.main_split_max}, {s.artist_split_max}")
    win.maximize()
    yield 1500
    check("maximized layout restored", (win.main_split.get_position(), win.library_split.get_position()) == (1100, 300),
          f"{win.main_split.get_position()}, {win.library_split.get_position()}")
    check("windowed value kept", s.main_split == 600, str(s.main_split))
    application.quit()


gen = None


def tick():
    try:
        GLib.timeout_add(next(gen), tick)
    except StopIteration:
        pass
    except Exception:
        import traceback; traceback.print_exc(); failures.append("exception"); application.quit()
    return False


def act(_a):
    global gen
    if gen is None:
        gen = steps(); GLib.timeout_add(10, tick)


application.connect("activate", act)
application.run([sys.argv[0]])
print("FAILED: " + ", ".join(failures) if failures else "all passed", flush=True)
sys.exit(1 if failures else 0)
