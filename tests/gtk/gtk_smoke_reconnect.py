"""GTK smoke test: MPD closes the UI connection after connection_timeout (60 s
without a command, normal while a track plays). The next track change arrives from
the idle watcher (own connection); the player bar must then show the new song, not
stay on the old one with the progress bar at 100% (user report 2026-11-12).
The mock closes the UI connection on request; idle events are fed to Window.on_idle.
Run like gtk_smoke.py (or tests/gtk/run_headless_mutter.sh); exits 1 on failure."""
import argparse, os, socket, sys, tempfile, threading
PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="toccata-smoke-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "cfg")
os.environ["XDG_CACHE_HOME"] = os.path.join(_TMP, "cache")
sys.path.insert(0, PROJECT)
from tests import search_mock

SONGS = [("a/1.flac", "First Song", 30), ("a/2.flac", "Second Song", 200)]
CUR = [0]
CONNS = {}   # client port -> handler


class H(search_mock.Handler):
    def handle(self):
        CONNS[self.client_address[1]] = self
        super().handle()

    def respond(self, a):
        cmd = a[0]
        if cmd == "idle":
            return ""
        if cmd in ("noidle", "replay_gain_status", "outputs"):
            return "OK\n"
        f, t, d = SONGS[CUR[0]]
        if cmd == "status":
            return (f"state: play\nsong: {CUR[0]}\nsongid: {CUR[0] + 1}\nelapsed: 1.0\n"
                    f"duration: {d}.0\nvolume: 50\nOK\n")
        if cmd == "currentsong":
            return f"file: {f}\nTitle: {t}\nArtist: Band\nPos: {CUR[0]}\nId: {CUR[0] + 1}\nOK\n"
        if cmd == "playlistinfo":
            return "".join(f"file: {f}\nTitle: {t}\nArtist: Band\nTime: {d}\nPos: {i}\nId: {i + 1}\n"
                           for i, (f, t, d) in enumerate(SONGS)) + "OK\n"
        return super().respond(a)


def drop(port):
    """Close the server side of one connection, like MPD's connection_timeout."""
    h = CONNS[port]
    try:
        h.request.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    h.request.close()


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
        print("ok  " if ok else "FAIL", name, repr(got), "" if ok else f"(wanted {want!r})", flush=True)
        if not ok:
            failures.append(name)

    def steps():
        win = application.props.active_window
        yield 1500
        check("first song shown", win.now_title.get_text(), "First Song")
        check("first song marked playing in the queue", [s.playing for s in win._queue_songs], ['play', ''])

        # 60 s pass: MPD closes the UI connection; then the track changes
        drop(win.client._sock.getsockname()[1])
        yield 200
        CUR[0] = 1
        win.on_idle(["player"])
        yield 200
        check("after the drop: new song shown", win.now_title.get_text(), "Second Song")
        check("after the drop: status follows", win.status.get("songid"), "2")
        check("after the drop: seek range is the new duration", win.seek.get_adjustment().get_upper(), 200.0)
        check("after the drop: queue marks the new song", [s.playing for s in win._queue_songs], ['', 'play'])
        check("UI connection is live again", win.client.connected, True)

        # again, now the second command after the drop hits a reset socket in sendall
        port = win.client._sock.getsockname()[1]
        drop(port)
        yield 200
        try:
            win.client._sock.sendall(b"ping\n")  # the peer answers with a reset
        except OSError:
            pass
        yield 200
        CUR[0] = 0
        win.on_idle(["player"])
        yield 200
        check("after a reset: new song shown", win.now_title.get_text(), "First Song")
        check("after a reset: queue marks it", [s.playing for s in win._queue_songs], ['play', ''])
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
