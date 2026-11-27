"""GTK smoke test: main menu > Server Information opens the server dialog on its\ninformation page, filled from stats / urlhandlers / tagtypes in the background.\nRun like gtk_smoke.py (or tests/gtk/run_headless_mutter.sh); exits 1 on failure."""
import argparse, os, sys, tempfile, threading
PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="toccata-smoke-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "cfg")
os.environ["XDG_CACHE_HOME"] = os.path.join(_TMP, "cache")
sys.path.insert(0, PROJECT)
from tests import search_mock



class H(search_mock.Handler):
    def respond(self, a):
        cmd = a[0]
        if cmd == "idle":
            return ""
        if cmd in ("noidle", "currentsong", "replay_gain_status", "outputs", "playlistinfo"):
            return "OK\n"
        if cmd == "status":
            return "state: stop\nOK\n"
        if cmd == "stats":
            return ("uptime: 638082\nplaytime: 88111\nartists: 22352\nalbums: 16046\nsongs: 186107\n"
                    "db_playtime: 58487571\ndb_update: 1794841097\nOK\n")
        if cmd == "urlhandlers":
            return "handler: http://\nhandler: nfs://\nOK\n"
        return super().respond(a)


def main():
    search_mock.start()
    srv = search_mock.Server(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    import gi
    gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
    from gi.repository import Adw, GLib
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

    def rows(dialog):
        out = {}
        for g in dialog.info_groups:
            stack = [g]
            while stack:
                w = stack.pop()
                if isinstance(w, Adw.ActionRow):
                    labels = []
                    def walk(x):
                        y = x.get_first_child()
                        while y is not None:
                            if type(y).__name__ == "Label" and "dim-label" in y.get_css_classes():
                                labels.append(y.get_label())
                            walk(y)
                            y = y.get_next_sibling()
                    walk(w)
                    out[w.get_title()] = labels[0] if labels else w.get_subtitle()
                    continue
                c = w.get_first_child()
                while c is not None:
                    stack.append(c)
                    c = c.get_next_sibling()
        return out

    def steps():
        win = application.props.active_window
        yield 1500
        win.activate_action("win.server-info", None)
        yield 800
        d = win._server_dialog
        check("dialog open on the information page", d is not None and d.get_visible_page_name(), "info")
        got = rows(d)
        check("group titles", [g.get_title() for g in d.info_groups], ["Server", "Database"])
        check("songs", got.get("Songs"), "186,107")
        check("uptime", got.get("Uptime"), "7 days 9:14:42")
        check("handlers", got.get("Handlers"), "http, nfs")
        check("tags listed", "Artist" in (got.get("Tags") or ""), True)
        check("address", got.get("Address"), f"127.0.0.1:{srv.server_address[1]}")
        d.refresh()
        yield 800
        check("refresh does not pile up groups", len(d.info_groups), 2)
        d.close()
        yield 300
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
