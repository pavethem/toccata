"""A mock MPD that evaluates `search`/`find modified-since` (with `window`) on
an in-memory database and keeps stored playlists, like MPD does:
listplaylists, listplaylistinfo, load, save, rm, rename, playlistadd,
playlistdelete, playlistmove, and command lists."""
import socketserver
import threading

from tests.lazy_mock import render, tokenize

TAGS = ("Artist", "AlbumArtist", "Album", "Title", "Genre", "Date", "Composer",
        "Performer", "Comment", "OriginalDate", "Track", "Disc")


def make_db():
    def s(file, mtime, **tags):
        return {"file": file, "Last-Modified": mtime, "duration": "100.000", **tags}
    return [
        s("rock/a1.flac", "2024-01-10T10:00:00Z", Title="Alpha", Artist="Rocker", AlbumArtist="Rocker",
          Album="First", Track="1", Genre="Rock", Date="2001", Composer="Bach"),
        s("rock/a2.flac", "2024-01-10T10:00:00Z", Title="Beta", Artist="Rocker", AlbumArtist="Rocker",
          Album="First", Track="2", Genre="Rock", Date="2001"),
        s("jazz/b1.flac", "2025-06-01T00:00:00Z", Title="Blue", Artist="Jazzer", Album="Night",
          Track="1", Genre="Jazz", Date="1999", Comment="live take", Performer="Trio"),
        s("special/alphabet soup.flac", "2025-06-02T00:00:00Z", Title="Soup", Artist="Chef",
          Album="Kitchen", OriginalDate="1970"),
    ] + [s(f"many/{i:03d}.flac", "2020-01-01T00:00:00Z", Title=f"Many {i}", Artist="Mass",
           Album="Lots", Track=str(i + 1)) for i in range(25)]


def _epoch(iso):
    import calendar
    import time
    return calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))


class Handler(socketserver.StreamRequestHandler):
    db = []
    playlists = {}         # name -> [uri]
    queue = []             # uris
    tagtypes = TAGS
    version = "0.23.5"
    supports_window = True
    max_output = 10 ** 9   # bytes per answer; bigger -> connection dropped, like MPD
    log = []

    def handle(self):
        try:
            self.wfile.write(f"OK MPD {self.version}\n".encode())
            batch = None
            for raw in self.rfile:
                args = tokenize(raw.decode().strip())
                if not args:
                    continue
                Handler.log.append(args)
                if args[0] in ("command_list_begin", "command_list_ok_begin"):
                    batch = []
                    continue
                if args[0] == "command_list_end":
                    out = "OK\n"
                    for cmd in batch:
                        body = self.respond(cmd)
                        if body.startswith("ACK"):
                            out = body
                            break
                    batch = None
                    self.wfile.write(out.encode())
                    continue
                if batch is not None:
                    batch.append(args)
                    continue
                body = self.respond(args)
                if len(body) > self.max_output:
                    return  # MPD closes the connection when its output buffer overflows
                self.wfile.write(body.encode())
        except (ConnectionResetError, BrokenPipeError):
            return

    @staticmethod
    def ack(cmd, text="unknown command", code=5):
        return f"ACK [{code}@0] {{{cmd}}} {text}\n"

    def songs_out(self, songs, rest, cmd):
        if len(rest) >= 2 and rest[-2] == "window":
            if not self.supports_window:
                return None, self.ack(cmd, "too many arguments", 2)
            lo, hi = rest[-1].split(":")
            songs = songs[int(lo):int(hi)]
        return songs, None

    def respond(self, a):
        cmd, rest = a[0], a[1:]
        db = self.db
        if cmd == "status":
            return "state: stop\nOK\n"
        if cmd == "tagtypes":
            return "".join(f"tagtype: {t}\n" for t in self.tagtypes) + "OK\n"
        if cmd == "search":
            tag, text = rest[0].lower(), rest[1].casefold()
            if tag == "file":
                found = [s for s in db if text in s["file"].casefold()]
            elif tag == "any":  # like MPD: every tag, but not the file name
                found = [s for s in db if any(text in str(v).casefold() for k, v in s.items()
                                              if k in TAGS)]
            else:
                found = [s for s in db if text in next((v for k, v in s.items() if k.lower() == tag), "").casefold()]
            found, err = self.songs_out(found, rest[2:], cmd)
            return err or "".join(render(s) for s in found) + "OK\n"
        if cmd == "find" and rest and rest[0] == "modified-since":
            since = int(rest[1])
            found = [s for s in db if _epoch(s["Last-Modified"]) >= since]
            found, err = self.songs_out(found, rest[2:], cmd)
            return err or "".join(render(s) for s in found) + "OK\n"
        pl = self.playlists
        if cmd == "listplaylists":
            return "".join(f"playlist: {n}\nLast-Modified: 2025-01-01T00:00:00Z\n" for n in pl) + "OK\n"
        if cmd == "listplaylistinfo":
            if rest[0] not in pl:
                return self.ack(cmd, "No such playlist", 50)
            by_file = {s["file"]: s for s in db}
            return "".join(render(by_file.get(u, {"file": u})) for u in pl[rest[0]]) + "OK\n"
        if cmd == "load":
            if rest[0] not in pl:
                return self.ack(cmd, "No such playlist", 50)
            uris = pl[rest[0]]
            if len(rest) == 3:
                at = int(rest[2])
                self.queue[at:at] = uris
            else:
                self.queue.extend(uris)
            return "OK\n"
        if cmd == "save":
            if rest[0] in pl:
                return self.ack(cmd, "Playlist already exists", 56)
            pl[rest[0]] = list(self.queue)
            return "OK\n"
        if cmd == "rm":
            if rest[0] not in pl:
                return self.ack(cmd, "No such playlist", 50)
            del pl[rest[0]]
            return "OK\n"
        if cmd == "rename":
            if rest[0] not in pl:
                return self.ack(cmd, "No such playlist", 50)
            if rest[1] in pl:
                return self.ack(cmd, "Playlist already exists", 56)
            pl[rest[1]] = pl.pop(rest[0])
            return "OK\n"
        if cmd == "playlistadd":
            pl.setdefault(rest[0], []).append(rest[1])
            return "OK\n"
        if cmd == "playlistdelete":
            items = pl.get(rest[0])
            pos = int(rest[1])
            if items is None or pos >= len(items):
                return self.ack(cmd, "Bad song index", 2)
            del items[pos]
            return "OK\n"
        if cmd == "playlistmove":
            items = pl.get(rest[0])
            src, dst = int(rest[1]), int(rest[2])
            if items is None or src >= len(items) or dst >= len(items):
                return self.ack(cmd, "Bad song index", 2)
            items.insert(dst, items.pop(src))
            return "OK\n"
        return self.ack(cmd)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start(**settings):
    Handler.db = make_db()
    Handler.playlists = {}
    Handler.queue = []
    Handler.tagtypes = TAGS
    Handler.version = "0.23.5"
    Handler.supports_window = True
    Handler.max_output = 10 ** 9
    Handler.log = []
    for k, v in settings.items():
        setattr(Handler, k, v)
    srv = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    return srv, srv.server_address[1]
