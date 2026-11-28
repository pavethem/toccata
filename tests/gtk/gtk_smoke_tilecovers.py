"""GTK smoke test: the Albums view grid (sorted by artist, so each tile first needs
one `find ... window 0:1` for a song of its album) asks MPD only for the tiles on
screen and near it, on-screen ones first - not for the ~1300 tiles GridView keeps
bound (max-columns x ~30 rows). Before the fix a jump to the middle of a big
library waited behind a thousand lookups (user report 2026-11-28).
Run: sh tests/gtk/run_headless_mutter.sh tests/gtk/gtk_smoke_tilecovers.py"""
import argparse, os, sys, tempfile, threading
PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="toccata-smoke-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "cfg")
os.environ["XDG_CACHE_HOME"] = os.path.join(_TMP, "cache")
sys.path.insert(0, PROJECT)
from tests import albums_mock, lazy_mock

N_EXTRA = 3000


class H(albums_mock.Handler):
    def respond(self, a):
        if a[0] == "idle":
            return ""
        if a[0] in ("noidle", "currentsong", "replay_gain_status", "tagtypes", "outputs", "listplaylists",
                    "playlistinfo"):
            return "OK\n"
        return super().respond(a)


def lookups():
    """Albums looked up so far (for their cover): [album name] in request order."""
    out = []
    for f in H.finds:
        if f[-2:-1] == ["window"] and f[-1] == "0:1":
            out.append(f[1].split("(Album == ")[1].rsplit(")", 2)[0].strip("'\""))
    return out


def main():
    albums_mock.start()
    for i in range(N_EXTRA):
        H.db.append({"file": f"x/{i:04d}/1.flac", "Added": "2020-01-01T00:00:00Z", "Title": "t",
                     "Artist": f"Art {i:04d}", "AlbumArtist": f"Art {i:04d}", "Album": f"LP {i:04d}",
                     "Date": "2000"})
    srv = lazy_mock.LazyServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    import gi
    gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); gi.require_version("Graphene", "1.0")
    from gi.repository import GLib, Graphene
    from toccata import app as appmod
    appmod.APP_ID = "io.github.toccata.smoke"
    application = appmod.App(argparse.Namespace(host="127.0.0.1", port=srv.server_address[1], password=None,
                                                music_dir=None, no_covers=False, no_highlight=False))
    fails = []

    def check(name, cond, *info):
        print("ok  " if cond else "FAIL", name, *info, flush=True)
        if not cond:
            fails.append(name)

    def on_screen(win):
        h, out = win.albums_grid.get_height(), []
        child = win.albums_grid.get_first_child()
        while child is not None:
            tile = child.get_first_child()
            if getattr(tile, "item", None) is not None and child.get_mapped() and tile.get_height() > 0:
                ok, p = tile.compute_point(win.albums_grid, Graphene.Point().init(0, 0))
                if ok and p.y + tile.get_height() > 0 and p.y < h:
                    out.append(tile.item.entry.album)
            child = child.get_next_sibling()
        return out

    def bound(win):
        return sum(1 for i in range(win.albums_store.get_n_items()) if win.albums_store.get_item(i).tile is not None)

    def steps():
        win = application.props.active_window
        win.set_default_size(1000, 800)
        yield 1500
        win.views.set_visible_child_name("albums")
        yield 300
        win.albums_sort.set_selected(1)  # Artist
        yield 2500
        n_bound, vis, done = bound(win), on_screen(win), lookups()
        check("grid filled", win.albums_store.get_n_items() > N_EXTRA, win.albums_store.get_n_items())
        check("GridView keeps many more tiles bound than shown", n_bound > 5 * len(vis), n_bound, len(vis))
        check("every album on screen looked up", vis and set(vis) <= set(done), len(vis), len(done))
        check("only albums near the view looked up", len(done) <= 4 * len(vis), len(done), "for", len(vis))
        check("on-screen albums first", set(done[:len(vis)]) == set(vis), done[:len(vis)], vis)
        # jump to the middle, through a few positions on the way (like dragging the scrollbar)
        adj = win.albums_scroll.get_vadjustment()
        start, target = adj.get_value(), (adj.get_upper() - adj.get_page_size()) / 2
        before = len(lookups())
        for k in range(1, 9):
            adj.set_value(start + (target - start) * k / 8)
            yield 30
        yield 2000
        vis, done = on_screen(win), lookups()
        new = done[before:]
        check("after the jump: every album on screen looked up", vis and set(vis) <= set(done), vis[:3], len(vis))
        check("after the jump: few lookups", len(new) <= 12 * len(vis), len(new), "for", len(vis))
        jumped = new[next(i for i, a in enumerate(new) if a in vis):]  # from the first one on screen
        check("after the jump: on-screen albums before the ones around them",
              set(jumped[:len(vis)]) == set(vis), jumped[:len(vis)], vis)
        print("ALL OK" if not fails else f"FAILED: {fails}", flush=True)
        application.quit()

    gen = steps()

    def tick():
        try:
            GLib.timeout_add(next(gen), tick)
        except StopIteration:
            pass
        return False
    GLib.timeout_add(100, tick)
    application.run([])
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
