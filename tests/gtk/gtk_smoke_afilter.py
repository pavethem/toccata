"""GTK smoke test: the artist filter (Ctrl+F), the artist-list twin of the queue
filter. Run like gtk_smoke.py (broadway + dbus-run-session). Exits 1 on failure."""
import argparse, os, sys, tempfile, threading
PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="toccata-smoke-")
OUT = os.path.join(_TMP, "shots"); os.makedirs(OUT)
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "cfg")
os.environ["XDG_CACHE_HOME"] = os.path.join(_TMP, "cache")
sys.path.insert(0, PROJECT)
from tests import lazy_mock
from tests.lazy_mock import LazyHandler

class H(LazyHandler):
    def respond(self, a):
        if a[0] == "idle":
            return ""
        if a[0] in ("noidle", "currentsong", "replay_gain_status", "tagtypes", "outputs", "playlistinfo"):
            return "OK\n"
        return super().respond(a)

def main():
    srv, _ = lazy_mock.start(db=lazy_mock.genre_db())
    srv.shutdown(); srv.server_close()
    srv = lazy_mock.LazyServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    import gi
    gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
    from gi.repository import GLib, Gtk
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
    def shot(win, name):
        p = Gtk.WidgetPaintable.new(win); snap = Gtk.Snapshot(); p.snapshot(snap, win.get_width(), win.get_height())
        node = snap.to_node()
        if node is None:
            print("screenshot", name, "empty (not drawn yet)", flush=True)
        else:
            win.get_native().get_renderer().render_texture(node, None).save_to_png(f"{OUT}/{name}.png")
    def artists(win):
        return [win.artist_store.get_string(i) for i in range(win.artist_store.get_n_items())]
    def selected(win):
        item = win.art_sel.get_selected_item()
        return item.get_string() if item is not None else None
    def steps():
        win = application.props.active_window
        yield 1500
        everyone = artists(win)
        print("artists:", everyone, "selected:", selected(win), flush=True)
        check("several artists", len(everyone) > 3, True)
        win.views.set_visible_child_name("playlists")
        win.activate_action("win.search", None)  # Ctrl+F
        yield 300
        check("Ctrl+F shows the Artists view", win.views.get_visible_child_name(), "library")
        check("bar open, toggle on", (win.afilter_bar.get_search_mode(), win.afilter_toggle.get_active()), (True, True))
        f = win.get_focus()
        check("focus in the entry", f is not None and f.is_ancestor(win.afilter_entry), True)
        target = "Hornist"
        others = [a for a in everyone if a != target]
        win.afilter_entry.set_text("HORN")
        yield 600
        check("'HORN' filters", artists(win), [target])
        check("count", win.afilter_count.get_text(), f"1 of {len(everyone)}")
        check("first match selected", selected(win), target)
        yield 500
        check("its albums shown", win._shown_artist is not None and win._selected_artist, target)
        check("remembered", win.settings.last_artist, target)
        win.afilter_entry.set_text("h")
        yield 600
        check("'h' too short: all", artists(win), everyone)
        check("count empty", win.afilter_count.get_text(), "")
        check("selection kept", selected(win), target)
        win.afilter_entry.set_text("zzz nothing")
        yield 600
        check("no match: empty list", artists(win), [])
        check("albums of the last artist stay", win._selected_artist, target)
        yield 300
        shot(win, "afilter")
        # Locate an artist the filter hides: the filter closes
        win.afilter_entry.set_text(target)
        yield 600
        other = others[0]
        win.show_song_in_artists({"file": "x", "albumartist": other, "artist": other, "album": "?"})
        yield 800
        check("Show in Artists of a hidden artist closes the filter", win.afilter_bar.get_search_mode(), False)
        check("all artists again", artists(win), everyone)
        check("that artist selected", selected(win), other)
        # Escape
        win.activate_action("win.search", None); yield 200
        win.afilter_entry.set_text("horn"); yield 600
        win.afilter_entry.emit("stop-search"); yield 400
        check("Escape: closed and cleared", (win.afilter_bar.get_search_mode(), win.afilter_entry.get_text(), artists(win)),
              (False, "", everyone))
        check("selection kept after Escape", selected(win), target)
        # in the Search view Ctrl+F stays with the search entry
        win.views.set_visible_child_name("search"); yield 200
        win.activate_action("win.search", None); yield 200
        f = win.get_focus()
        check("Ctrl+F in Search view focuses the search entry",
              (win.views.get_visible_child_name(), f is not None and f.is_ancestor(win.search_entry)), ("search", True))
        print("FAILED" if failures else "ALL OK", "screenshots in", OUT, flush=True)
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
    sys.exit(1 if failures else 0)

main()
