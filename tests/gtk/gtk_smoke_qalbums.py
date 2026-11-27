"""GTK smoke test: behaviour of the grouped queue, written against songs and albums
(never row positions), so it holds whether album headers are GTK section headers or
rows of the list (2026-11-11 rebuild: headers as rows, so GTK's height estimate is
stable). Run like gtk_smoke.py; exits 1 on failure.
Queue: Rocker – First (2) | Jazzer – Night (1) | Mass – Lots (5) | Rocker – First (1)."""
import argparse, os, sys, tempfile, threading
PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="toccata-smoke-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "cfg")
os.environ["XDG_CACHE_HOME"] = os.path.join(_TMP, "cache")
sys.path.insert(0, PROJECT)
from tests import search_mock
from tests.lazy_mock import render

QUEUE = []  # [(songid, uri)]


class H(search_mock.Handler):
    def respond(self, a):
        cmd = a[0]
        if cmd == "idle":
            return ""
        if cmd in ("noidle", "currentsong", "replay_gain_status", "outputs"):
            return "OK\n"
        if cmd == "playlistinfo":
            by = {s["file"]: s for s in self.db}
            return "".join(render(dict(by.get(u, {"file": u}), Id=str(i), Pos=str(p)))
                           for p, (i, u) in enumerate(QUEUE)) + "OK\n"
        if cmd == "deleteid":
            QUEUE[:] = [(i, u) for i, u in QUEUE if str(i) != a[1]]
            return "OK\n"
        return super().respond(a)


def main():
    search_mock.start()
    uris = ["rock/a1.flac", "rock/a2.flac", "jazz/b1.flac"] + [f"many/{i:03d}.flac" for i in range(5)] + ["rock/a1.flac"]
    QUEUE[:] = [(10 + n, u) for n, u in enumerate(uris)]
    srv = search_mock.Server(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    import gi
    gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
    from gi.repository import Gdk, GLib
    from toccata import app as appmod
    appmod.APP_ID = "io.github.toccata.smoke"
    application = appmod.App(argparse.Namespace(host="127.0.0.1", port=srv.server_address[1], password=None,
                                                music_dir=None, no_covers=True, no_highlight=False))
    failures = []

    def check(name, got, want):
        ok = got == want
        print("ok  " if ok else "FAIL", name, got, "" if ok else f"(wanted {want})", flush=True)
        if not ok:
            failures.append(name)

    class G:
        def __init__(self, mods=0):
            self.mods = mods

        def get_current_event_state(self):
            return Gdk.ModifierType(self.mods)

    def steps():
        win = application.props.active_window
        win.set_size_request(1400, 1100)  # every row and header of this queue fits on screen
        yield 1500
        Song = appmod.Song
        sel = win.queue_sel

        def songs_shown():  # the song rows of the list, in order (album header rows skipped)
            return [sel.get_item(i) for i in range(sel.get_n_items()) if isinstance(sel.get_item(i), Song)]

        def titles(songs):
            return [s.data.get("title") for s in songs]

        def ids(songs):
            return [s.data["id"] for s in songs]

        def pos_of(song):  # list position of a song's row
            return next(i for i in range(sel.get_n_items()) if sel.get_item(i) is song)

        def click(song, shift=False, ctrl=False):  # what GTK's own click on that row runs
            win.queue_view.activate_action("list.select-item", GLib.Variant("(ubb)", (pos_of(song), ctrl, shift)))

        def selected():
            return ids(win._queue_selected())

        def header_widgets():  # album headers on screen, top to bottom
            found = []

            def walk(w):
                c = w.get_first_child()
                while c is not None:
                    if isinstance(c, appmod.QueueHeader):
                        if c.get_mapped() and c.group is not None:
                            found.append((c.compute_bounds(win.queue_view)[1].get_y(), c))
                    else:
                        walk(c)
                    c = c.get_next_sibling()
            walk(win.queue_view)
            return [w for _, w in sorted(found, key=lambda t: t[0])]

        def shown_headers():
            return [w.group.title for w in header_widgets()]

        def header(start):  # the header widget of the album starting at queue index `start`
            return next(w for w in header_widgets() if w.group.start == start)

        all_songs = list(win._queue_songs)
        check("ungrouped: every song shown, in queue order", ids(songs_shown()), ids(all_songs))
        check("ungrouped: no album headers", shown_headers(), [])
        win.activate_action("win.queue-grouped", None)
        yield 600
        groups = win._queue_groups
        check("groups", [(g.title, g.end - g.start) for g in groups],
              [("Rocker – First (2001)", 2), ("Jazzer – Night (1999)", 1), ("Mass – Lots", 5), ("Rocker – First (2001)", 1)])
        check("grouped: every song shown, in queue order", ids(songs_shown()), ids(all_songs))
        check("grouped: a header for every album", shown_headers(), [g.title for g in groups])
        check("header texts", [w.sub.get_text() for w in header_widgets()],
              ["2 Tracks (3:20)", "1 Track (1:40)", "5 Tracks (8:20)", "1 Track (1:40)"])
        mass = groups[2]
        mass_ids = ids(win._queue_songs[mass.start:mass.end])

        # header click selects the album, Ctrl+header click adds
        click(all_songs[0])
        header(mass.start)._on_click(G(), 1, -5, -5)
        yield 100
        check("header click selects the album's tracks", selected(), mass_ids)
        header(groups[1].start)._on_click(G(Gdk.ModifierType.CONTROL_MASK), 1, -5, -5)
        check("Ctrl+header click adds an album", sorted(selected()), sorted(mass_ids + ids(win._queue_songs[2:3])))
        # Shift+click range starts at the clicked album
        click(all_songs[0])
        header(mass.start)._on_click(G(), 1, -5, -5)
        click(all_songs[-1], shift=True)
        check("Shift+click after a header click: from the album to the clicked track",
              selected(), ids(all_songs[mass.start:]))
        # right-click on a header selects the album (its menu is not opened here)
        win.select_group(groups[0])
        check("select_group (header right-click) selects that album", selected(), ids(all_songs[0:2]))
        # dragging a header carries the whole album
        win.drag_group(mass)
        check("dragging a header carries the album", win._drag, ("queue", mass_ids))
        win._drag = None
        # dropping on a header means "before that album"
        ok, b = header(mass.start).compute_bounds(win.queue_view)
        check("drop on a header: before that album",
              win._queue_insert_index(b.get_x() + b.get_width() / 2, b.get_y() + b.get_height() / 2), mass.start)
        # Ctrl+A: every track once
        win.queue_view.grab_focus()
        win._on_library_key(None, Gdk.KEY_a, 0, Gdk.ModifierType.CONTROL_MASK)
        check("Ctrl+A selects every track once", sorted(selected()), sorted(ids(all_songs)))
        bits = sel.get_selection()
        check("no album header row is ever selected",
              [type(sel.get_item(bits.get_nth(i))).__name__ for i in range(bits.get_size())
               if not isinstance(sel.get_item(bits.get_nth(i)), Song)], [])
        # header rows exactly as tall as track rows: GTK's height estimate stays exact
        scroller = win.queue_view.get_parent()
        song_h = {round(s.row.compute_bounds(scroller)[1].get_height()) for s in all_songs if s.row is not None}
        head_h = {round(w.compute_bounds(scroller)[1].get_height()) for w in header_widgets()}
        check("album header rows are exactly as tall as track rows", (head_h, len(song_h)), (song_h, 1))

        # collapse an album: one row stands for it, and counts as all its tracks
        win.toggle_group(mass)
        yield 400
        mass = win._queue_groups[2]
        shown = songs_shown()
        check("collapsed: its tracks hidden but one", len(shown), len(all_songs) - 4)
        summary = [s for s in shown if s.summary]
        check("collapsed: one summary row for it", [s.group.title for s in summary], ["Mass – Lots"])
        check("collapsed: header still shown", shown_headers(), [g.title for g in win._queue_groups])
        click(summary[0])
        check("selecting the collapsed row selects the whole album", selected(), mass_ids)
        header(mass.start)._on_click(G(), 1, -5, -5)
        check("header click on a collapsed album selects all its tracks", selected(), mass_ids)
        win.toggle_group(mass)
        yield 400
        check("expanded again: every song shown", ids(songs_shown()), ids(all_songs))

        # filter: only matches, and only the headers of albums with a match
        win.open_queue_filter()
        win.qfilter_entry.set_text("many")
        yield 600
        check("filter 'many': only the Mass tracks", titles(songs_shown()), [f"Many {i}" for i in range(5)])
        check("filter 'many': only its album header", shown_headers(), ["Mass – Lots"])
        win.qfilter_entry.set_text("")
        win.qfilter_bar.set_search_mode(False)
        yield 600
        check("filter closed: every song again", ids(songs_shown()), ids(all_songs))
        check("filter closed: every header again", shown_headers(), [g.title for g in win._queue_groups])

        # Delete with a collapsed album selected removes all its tracks; the next track is selected
        win.toggle_group(win._queue_groups[2])
        yield 400
        mass = win._queue_groups[2]
        win.queue_view.grab_focus()
        header(mass.start)._on_click(G(), 1, -5, -5)
        win._on_queue_key(None, Gdk.KEY_Delete, 0, Gdk.ModifierType(0))
        win.refresh_queue()  # (the mock sends no idle events)
        yield 600
        left = [u for _, u in QUEUE]
        check("Delete on a collapsed album removed its 5 tracks", left, ["rock/a1.flac", "rock/a2.flac", "jazz/b1.flac", "rock/a1.flac"])
        check("after Delete the track that moved up is selected", titles(win._queue_selected()), ["Alpha"])
        check("headers after the Delete", shown_headers(), [g.title for g in win._queue_groups])

        # back to ungrouped
        win.activate_action("win.queue-grouped", None)
        yield 600
        check("ungrouped again: no headers", shown_headers(), [])
        check("ungrouped again: every song", len(songs_shown()), 4)
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
