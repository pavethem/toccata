"""GTK smoke test (user report 2026-11-11): add many albums to the queue, scroll
down, select and Delete them, add them again. The queue used to be rebuilt from
new entry objects, so GTK guessed its scroll anchor proportionally (row 3 of 4 ->
row ~355 of 474): ungrouped the view jumped deep into the list, grouped it went
empty, or (after the first fix) left the first earlier track blank until a scroll.
Also checks that MPD's piled-up change reports give one rebuild.
Run like gtk_smoke.py (broadway + dbus-run-session); exits 1 on failure.
Mock MPD: add / deleteid in command lists, idle wakes on each queue change."""
import argparse, os, sys, tempfile, threading, time
PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="toccata-smoke-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "cfg")
os.environ["XDG_CACHE_HOME"] = os.path.join(_TMP, "cache")
sys.path.insert(0, PROJECT)
from tests import lazy_mock
from tests.lazy_mock import LazyHandler, tokenize

QUEUE, BYFILE, NEXT = [], {}, [100]
LOCK = threading.Lock(); CHANGED = threading.Condition(LOCK); VERSION = [0]


def make_db():
    db = [{"file": f"start/{t}.flac", "Title": f"Start {t}", "Artist": "S", "AlbumArtist": "S", "Album": "Start",
           "Track": str(t + 1), "Time": "200", "duration": "200.0"} for t in range(4)]
    for a in range(130):  # ~1700 tracks: four 500-song command lists
        for t in range(1 + a % 25):
            db.append({"file": f"big/{a:02d}/{t:02d}.flac", "Title": f"T{t}", "Artist": "Big", "AlbumArtist": "Big",
                       "Album": f"Album {a:02d}", "Track": str(t + 1), "Time": "200", "duration": "200.0"})
    return db


class H(LazyHandler):
    def handle(self):
        listing, seen = None, VERSION[0]
        try:
            self.wfile.write(b"OK MPD 0.23.5\n")
            for raw in self.rfile:
                args = tokenize(raw.decode().strip())
                if not args:
                    continue
                cmd = args[0]
                if cmd in ("command_list_begin", "command_list_ok_begin"):
                    listing = []
                elif cmd == "command_list_end":
                    time.sleep(0.05)  # a real server takes a moment per list
                    with LOCK:
                        for a in listing:
                            if a[0] == "add":
                                QUEUE.append((NEXT[0], BYFILE[a[1]])); NEXT[0] += 1
                            elif a[0] == "deleteid":
                                QUEUE[:] = [q for q in QUEUE if str(q[0]) != a[1]]
                        VERSION[0] += 1; CHANGED.notify_all()
                    listing = None
                    self.wfile.write(b"OK\n")
                elif listing is not None:
                    listing.append(args)
                elif cmd == "idle":
                    with LOCK:
                        while VERSION[0] == seen:
                            CHANGED.wait(0.5)
                        seen = VERSION[0]
                    self.wfile.write(b"changed: playlist\nOK\n")
                elif cmd != "noidle":
                    self.wfile.write(self.respond(args).encode())
        except (ConnectionResetError, BrokenPipeError, OSError):
            return

    def respond(self, a):
        if a[0] == "playlistinfo":
            with LOCK:
                return "".join(lazy_mock.render(dict(s, Id=str(i), Pos=str(p))) for p, (i, s) in enumerate(QUEUE)) + "OK\n"
        if a[0] in ("currentsong", "replay_gain_status", "tagtypes", "outputs"):
            return "OK\n"
        return super().respond(a)


def main():
    db = make_db()
    H.db = db
    BYFILE.update({s["file"]: s for s in db})
    QUEUE[:] = [(i, s) for i, s in enumerate(db[:4])]
    srv = lazy_mock.LazyServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    import gi
    gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
    from gi.repository import Gdk, GLib
    from toccata import app as appmod
    appmod.APP_ID = "io.github.toccata.smoke"
    application = appmod.App(argparse.Namespace(host="127.0.0.1", port=srv.server_address[1], password=None,
                                                music_dir=None, no_covers=True, no_highlight=False))
    failures = []

    def check(name, ok, detail):
        print("ok  " if ok else "FAIL", name, detail, flush=True)
        if not ok:
            failures.append(name)

    def steps():
        win = application.props.active_window
        yield 1500
        adj = win.queue_view.get_vadjustment()
        big = [s for s in db if s["AlbumArtist"] == "Big"]
        refreshes = [0]
        real_refresh = win.refresh_queue

        def counted():
            refreshes[0] += 1
            return real_refresh()
        win.refresh_queue = counted

        def add():
            win.add_tracks(big, "Big")
        for grouped in (False, True):
            if grouped != win._grouped:
                win.activate_action("win.queue-grouped", None)
                yield 500
            refreshes[0] = 0
            add()
            yield 1500
            n = win.queue_sel.get_n_items()
            check(f"grouped={grouped}: first add", n >= 4 + len(big), f"{n} rows")
            # one per command list before ChangeBatcher; a report that arrives just after the
            # merged refresh ran may add one more (it then changes nothing)
            check(f"grouped={grouped}: reports merged (4 command lists)", refreshes[0] <= 2, f"{refreshes[0]} rebuilds")
            # GTK's estimate of the list's height must not depend on where you are: else a
            # held scrollbar jumps back and forth (grouped, user report 2026-11-11)
            uppers = []
            for frac in (0.3, 0.55, 0.8, 0.45, 0.65, 0.0):
                adj.set_value(frac * (adj.get_upper() - adj.get_page_size()))
                yield 400
                uppers.append(round(adj.get_upper()))
            check(f"grouped={grouped}: the list's height is the same at every scroll position",
                  len(set(uppers)) == 1, f"heights {uppers}")
            # removing in the middle of the queue must leave the view where it was (it
            # jumped to the top when the removed row had the keyboard focus, 2026-11-11)
            class Click:
                def get_current_event_state(self):
                    return Gdk.ModifierType(0)
            scroller = win.queue_view.get_parent()

            def visible_rows(kind):
                out = []
                for i in range(win.queue_sel.get_n_items()):
                    item = win.queue_sel.get_item(i)
                    w = item.widget if isinstance(item, appmod.QueueAlbum) else getattr(item, "row", None)
                    if isinstance(item, kind) and w is not None and w.get_mapped():
                        y = w.compute_bounds(scroller)[1].get_y()
                        if 40 < y < scroller.get_height() - 120:
                            out.append(item)
                return out
            paths = ["Delete key", "album menu Remove", "row - button"] if grouped else ["Delete key", "row - button"]
            for how in paths:
                kind = appmod.QueueAlbum if (grouped and how != "row - button") else appmod.Song
                adj.set_value(20000)
                yield 400
                while not visible_rows(kind):
                    adj.set_value(adj.get_value() + 50)
                    yield 50
                before = adj.get_value()
                item = visible_rows(kind)[0]

                def screen_y():  # {row object: y} of the rows laid out now
                    out = {}
                    for i in range(win.queue_sel.get_n_items()):
                        it = win.queue_sel.get_item(i)
                        w = it.widget if isinstance(it, appmod.QueueAlbum) else getattr(it, "row", None)
                        if w is not None and w.get_mapped():
                            out[it] = w.compute_bounds(scroller)[1].get_y()
                    return out
                y_before = screen_y()
                gap_y = y_before[item]
                win.queue_view.grab_focus()
                if kind is appmod.QueueAlbum:
                    item.widget._on_click(Click(), 1, -5, -5)  # (focuses the header, as a click does)
                    songs = win._queue_selected()
                else:
                    item.row.grab_focus()
                    songs = [item]
                n_before = len(win._queue_songs)
                if how == "Delete key":
                    if kind is appmod.Song:
                        win.queue_view.activate_action("list.select-item", GLib.Variant(
                            "(ubb)", (next(i for i in range(win.queue_sel.get_n_items()) if win.queue_sel.get_item(i) is item), False, False)))
                    win._on_queue_key(None, Gdk.KEY_Delete, 0, Gdk.ModifierType(0))
                elif how == "album menu Remove":
                    win.remove_songs(songs)
                else:
                    win.remove_from_queue(item)
                yield 1500
                gone = n_before - len(win._queue_songs)
                y_after = screen_y()
                # (2026-11-13: the rows above moved down instead, up to 112 px with compact rows)
                above = [(round(y), round(y_after[it])) for it, y in y_before.items()
                         if -40 < y < gap_y and it in y_after]  # (also a row partly above the view)
                check(f"grouped={grouped}: {how}: the rows above the gap stay where they were",
                      above and all(abs(a - b) < 1 for a, b in above), f"y before -> after {above}")
                check(f"grouped={grouped}: {how} in the middle keeps the view where it was",
                      gone > 0 and abs(adj.get_value() - before) <= 60,
                      f"removed {gone}, scroll {before:.0f} -> {adj.get_value():.0f}")
            # scroll to the bottom, select the added tracks, Delete
            win.queue_view.grab_focus()
            for _ in range(80):
                adj.set_value(min(adj.get_upper() - adj.get_page_size(), adj.get_value() + 600))
                yield 16
            yield 300
            n = win.queue_sel.get_n_items()  # rows, with album header rows when grouped
            first_big = next(i for i in range(n) if isinstance(win.queue_sel.get_item(i), appmod.Song)
                             and win.queue_sel.get_item(i).data.get("albumartist") == "Big")
            win.queue_view.activate_action("list.select-item", GLib.Variant("(ubb)", (first_big, False, False)))
            win.queue_view.activate_action("list.select-item", GLib.Variant("(ubb)", (n - 1, False, True)))
            win._on_queue_key(None, Gdk.KEY_Delete, 0, Gdk.ModifierType(0))
            yield 1500
            check(f"grouped={grouped}: removed", len(win._queue_songs) == 4, f"{len(win._queue_songs)} songs left")
            kept = list(win._queue_songs)
            seen = []
            hid = adj.connect("value-changed", lambda a: seen.append(a.get_value()))
            scroller = win.queue_view.get_parent()
            first = kept[0]
            painted = []  # y of the first earlier track in every painted frame (None: not laid out)

            def after_paint(_clock):
                row = first.row
                if row is None or not row.get_mapped():
                    painted.append(None)
                else:
                    painted.append(round(row.compute_bounds(scroller)[1].get_y()))
            clock = win.get_frame_clock()
            pid = clock.connect("after-paint", after_paint)
            win.queue_view.add_tick_callback(lambda w, c: len(painted) < 40)  # keep frames coming
            add()
            yield 1500
            clock.disconnect(pid)
            adj.disconnect(hid)
            steady = painted[0] if painted else None
            # At the top the view must not move at all (no hiccup): GTK is anchored on the
            # first row before the change (Window._queue_rows_changing).
            bad = sum(1 for y in painted if y != steady)
            check(f"grouped={grouped}: no painted frame shows the list moved (no hiccup)",
                  bool(painted) and bad == 0, f"{bad} of {len(painted)} frames moved")
            page = adj.get_page_size()
            check(f"grouped={grouped}: second add keeps the old entries' objects",
                  all(a is b for a, b in zip(kept, win._queue_songs)), "")
            check(f"grouped={grouped}: second add does not scroll at all (we were at the top)",
                  max(seen + [0]) == 0 and adj.get_value() == 0,
                  f"largest scroll value {max(seen + [0]):.0f}, now {adj.get_value():.0f}, page {page:.0f}")
            # the tracks that were in the queue before must all be laid out on screen
            # (the sync pin fix left the first one blank until the user scrolled)
            scroller = win.queue_view.get_parent()
            shown = []
            for song in kept:
                ok = song.row is not None and song.row.get_mapped()
                if ok:
                    got, r = song.row.compute_bounds(scroller)
                    ok = got and r.get_y() >= 0 and r.get_y() + r.get_height() <= scroller.get_height()
                shown.append(ok)
            check(f"grouped={grouped}: the {len(kept)} earlier tracks are all on screen after the re-add",
                  all(shown), str(shown))
            # scrolled down somewhere: adding more at the end must not move the view either
            adj.set_value(3000)
            yield 300
            where = adj.get_value()
            seen.clear()
            hid = adj.connect("value-changed", lambda a: seen.append(a.get_value()))
            win.add_tracks(big[:200], "more")
            yield 1500
            adj.disconnect(hid)
            check(f"grouped={grouped}: scrolled down, adding at the end keeps the view where it was",
                  all(abs(v - where) < 1 for v in seen) and abs(adj.get_value() - where) < 1,
                  f"was {where:.0f}, values during the add {sorted(set(round(v) for v in seen))}, now {adj.get_value():.0f}")
            # clean up for the next round: remove the added tracks again
            ids = [s.data["id"] for s in win._queue_songs[4:]]
            win.safe(lambda: win.client.command_list([("deleteid", i) for i in ids]))
            yield 1500
            adj.set_value(0)
            yield 200
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
