"""GTK smoke test: album covers in the Search view's result rows (like Cantata).
The cover loader is replaced by a fake that answers per album folder, so the test
checks the wiring: one thumbnail per row, the right album's cover, the placeholder
when there is none, nothing for a row rebound meanwhile. Run headless with
tests/gtk/run_headless_mutter.sh; exits 1 on failure."""
import argparse, os, sys, tempfile, threading
PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="toccata-smoke-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "cfg")
os.environ["XDG_CACHE_HOME"] = os.path.join(_TMP, "cache")
sys.path.insert(0, PROJECT)
from tests import search_mock


class H(search_mock.Handler):
    def respond(self, a):
        if a[0] == "idle":
            return ""
        if a[0] in ("noidle", "currentsong", "replay_gain_status", "outputs", "playlistinfo"):
            return "OK\n"
        return super().respond(a)


def main():
    search_mock.start()
    srv = search_mock.Server(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    import gi
    gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
    from gi.repository import Gdk, GLib
    from toccata import app as appmod
    appmod.APP_ID = "io.github.toccata.smoke"
    application = appmod.App(argparse.Namespace(host="127.0.0.1", port=srv.server_address[1], password=None,
                                                music_dir=None, no_covers=False, no_highlight=False))
    failures = []

    def check(name, got, want):
        ok = got == want
        print("ok  " if ok else "FAIL", name, got, "" if ok else f"(wanted {want})", flush=True)
        if not ok:
            failures.append(name)

    def texture(rgb):
        return Gdk.MemoryTexture.new(2, 2, Gdk.MemoryFormat.R8G8B8, GLib.Bytes.new(bytes(rgb) * 4), 6)

    covers = {"rock": texture((200, 0, 0))}  # jazz/ has none
    requests = []
    later = []

    class FakeLoader:
        def request(self, uri, callback, wanted=None, song=None, size=None):
            requests.append((uri, (song or {}).get("file"), size))
            later.append((uri, callback, wanted))

    def answer_all(reverse=False):
        for uri, cb, wanted in (reversed(later) if reverse else later):
            if wanted is None or wanted():
                cb(covers.get(os.path.dirname(uri)))
        later.clear()

    def steps():
        win = application.props.active_window
        win.set_size_request(1200, 900)
        yield 1500
        win.cover_loader = FakeLoader()
        win.focus_search()
        win.search_entry.set_text("a")  # matches rock and jazz songs
        win.search_entry.emit("activate")
        yield 800
        songs = list(win.search_store)
        uris = [s.uri for s in songs]
        check("results from rock and other albums", {"rock", "jazz"} <= {os.path.dirname(u) for u in uris}, True)
        check("one cover request per shown row, as thumbnail with the song's tags",
              sorted(requests), sorted((u, u, appmod.THUMB_SIZE) for u in uris))
        answer_all()
        yield 300
        from gi.repository import Gtk
        paint = Gtk.WidgetPaintable.new(win); snap = Gtk.Snapshot()
        paint.snapshot(snap, win.get_width(), win.get_height())
        shot = os.path.join(_TMP, "search-covers.png")
        win.get_native().get_renderer().render_texture(snap.to_node(), None).save_to_png(shot)
        print("screenshot", shot, flush=True)
        rows = {s.uri: s.row for s in songs}
        check("every row has a visible thumbnail", all(r.thumb.get_visible() for r in rows.values()), True)
        shown = {u: r.thumb.get_visible_child_name() for u, r in rows.items()}
        check("rock rows show the cover, jazz rows the placeholder",
              shown, {u: ("pic" if u.startswith("rock/") else "none") for u in uris})
        check("rock row's cover is rock's texture",
              all(r.thumb._art._texture is covers["rock"] for u, r in rows.items() if u.startswith("rock/")), True)
        # a late answer for a row that shows another song meanwhile is dropped
        rock = next(s for s in songs if s.uri.startswith("rock/"))
        jazz = next(s for s in songs if s.uri.startswith("jazz/"))
        r = rock.row
        requests.clear()
        r.unbind_song(); r.bind_song(rock, lambda s: None)   # request for rock pending...
        r.unbind_song(); r.bind_song(jazz, lambda s: None)   # ...row now shows jazz
        check("each rebinding asks for its song's cover", [q[0] for q in requests], [rock.uri, jazz.uri])
        answer_all(reverse=True)  # the stale rock answer arrives last
        check("the stale rock answer does not land on the jazz row", r.thumb.get_visible_child_name(), "none")
        # the queue's song rows have no thumbnail (their album header has one)
        check("queue song rows without a thumbnail", appmod.SongRow([], None, False).thumb, None)
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
