"""GTK smoke test: library, albums and playlists are reloaded once MPD's database
update is over, not on every `database` event while it runs (user request
2026-11-12); saving tags reloads when MPD reports the rescan finished, not after a
fixed delay. Idle events are fed to Window.on_idle directly (the mock never sends
any). Run like gtk_smoke.py (or tests/gtk/run_headless_mutter.sh); exits 1 on failure."""
import argparse, os, sys, tempfile, threading
PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="toccata-smoke-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "cfg")
os.environ["XDG_CACHE_HOME"] = os.path.join(_TMP, "cache")
sys.path.insert(0, PROJECT)
from tests import search_mock

UPDATING = []   # [job id] while the mock "rescans"
UPDATES = []    # uris of `update` commands


class H(search_mock.Handler):
    def respond(self, a):
        cmd = a[0]
        if cmd == "idle":
            return ""
        if cmd in ("noidle", "currentsong", "replay_gain_status", "outputs", "playlistinfo"):
            return "OK\n"
        if cmd == "status":
            return "state: stop\n" + "".join(f"updating_db: {j}\n" for j in UPDATING[:1]) + "OK\n"
        if cmd == "update":
            UPDATES.append(a[1] if len(a) > 1 else "")
            return "updating_db: 9\nOK\n"
        return super().respond(a)


def main():
    search_mock.start()
    srv = search_mock.Server(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    import gi
    gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
    from gi.repository import GLib
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
        yield 1500
        calls = []
        for name in ("reload_library", "invalidate_albums", "refresh_queue"):
            orig = getattr(win, name)
            setattr(win, name, lambda *a, _n=name, _o=orig, **k: (calls.append(_n), _o(*a, **k))[1])

        def count(name):
            return calls.count(name)

        # an update runs: `update` (start), then `database` twice (two jobs), then `update` (end)
        UPDATING[:] = ["5"]
        win.on_idle(["update"])
        check("update running: indicator shown", win.db_update.get_visible(), True)
        win.on_idle(["database"])
        win.on_idle(["database", "update"])
        yield 200
        check("while the update runs: library not reloaded", count("reload_library"), 0)
        check("while the update runs: albums not invalidated", count("invalidate_albums"), 0)
        UPDATING[:] = []
        win.on_idle(["update"])
        yield 200
        check("update over: library reloaded once", count("reload_library"), 1)
        check("update over: albums invalidated once", count("invalidate_albums"), 1)
        check("update over: indicator hidden", win.db_update.get_visible(), False)
        win.on_idle(["update"])
        check("a later `update` alone reloads nothing", count("reload_library"), 1)

        # `database` with no update running (e.g. another client's mount): at once
        win.on_idle(["database"])
        check("database change without a running update: reloaded at once", count("reload_library"), 2)

        # saving tags: rescan requested; reload when MPD says it is done, with the queue
        calls.clear()

        class S:
            def __init__(self, uri):
                self.uri = uri
        win.after_tag_write([S("rock/a1.flac"), S("rock/a2.flac")], True, [])
        check("tag save asked MPD to rescan both files", UPDATES[-2:], ["rock/a1.flac", "rock/a2.flac"])
        UPDATING[:] = ["9"]
        win.on_idle(["update"])
        yield 2500  # longer than the old fixed 2 s delay
        check("tag save: nothing reloaded while MPD rescans", count("reload_library"), 0)
        win.on_idle(["database"])
        check("tag save: still nothing after a `database` mid-rescan", count("reload_library"), 0)
        UPDATING[:] = []
        win.on_idle(["update"])
        yield 200
        check("tag save: library reloaded once the rescan is over", count("reload_library"), 1)
        check("tag save: queue refreshed for the new tags", count("refresh_queue"), 1)
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
