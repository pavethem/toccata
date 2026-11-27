"""GTK smoke test: right after tracks are added to a queue that is at its top, the
user can scroll at once (user report 2026-11-11: "a noticeable delay between
dragging some tracks into the queue and before I can scroll again"). The top pin
undoes GTK's own moves for a second after a change; a scroll by the user in that
second must not be undone. Both ungrouped and grouped. Run with
tests/gtk/run_headless_mutter.sh; exits 1 on failure."""
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
        return super().respond(a)


def main():
    search_mock.start()
    QUEUE[:] = [(1, "rock/a1.flac"), (2, "rock/a2.flac")]
    srv = search_mock.Server(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    import gi
    gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
    from gi.repository import GLib, Gtk
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

    def steps():
        win = application.props.active_window
        win.set_size_request(1200, 800)
        yield 1500
        adj = win.queue_view.get_vadjustment()
        next_id = [100]
        for mode in ("ungrouped", "grouped"):
            if mode == "grouped":
                win.activate_action("win.queue-grouped", None)
                yield 600
            QUEUE[:] = QUEUE[:2]
            win.refresh_queue()
            yield 600
            adj.set_value(0)
            yield 300
            # add 60 tracks (what a drop does), then the user scrolls 200 ms later
            for i in range(60):  # (60: room to scroll to 300 px also with compact rows)
                QUEUE.append((next_id[0], f"many/{i:03d}.flac")); next_id[0] += 1
            win.refresh_queue()
            yield 200
            scroller = win.queue_view.get_parent()
            ctl = next(c for c in scroller.observe_controllers() if isinstance(c, Gtk.EventControllerScroll))
            ctl.emit("scroll", 0.0, 3.0)  # what GTK emits for a wheel / touchpad scroll
            adj.set_value(300)
            yield 400
            check(f"{mode}: the user's scroll right after an add stays", round(adj.get_value()), 300)
            # dragging the scrollbar: held for longer than a wheel scroll's hold time
            adj.set_value(0)
            yield 300
            QUEUE.append((next_id[0], "rock/a1.flac")); next_id[0] += 1
            win.refresh_queue()
            bar = scroller.get_vscrollbar()
            press = next(c for c in bar.observe_controllers() if isinstance(c, Gtk.GestureClick)
                         and c.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE)
            press.emit("pressed", 1, 5.0, 5.0)
            yield 800
            adj.set_value(250)
            yield 300
            press.emit("released", 1, 5.0, 5.0)
            yield 100
            check(f"{mode}: a scrollbar drag right after an add stays", round(adj.get_value()), 250)
            # without user input, GTK's own move right after a change is still undone
            adj.set_value(0)
            yield 700  # past the scroll input's hold time
            for i in range(60, 65):
                QUEUE.append((next_id[0], f"many/{i:03d}.flac")); next_id[0] += 1
            win.refresh_queue()
            yield 50
            adj.set_value(120)  # a move that is not the user's
            yield 300
            check(f"{mode}: a move that is not the user's is still undone", round(adj.get_value()), 0)
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
