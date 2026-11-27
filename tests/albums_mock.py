"""A mock MPD for the Albums view: `list album [FILTER] group T1 [group T2]`
(MPD prints a group value only when it changes) and `find FILTER [sort -Added]
[window a:b]`, on a small library with Added times."""
import socketserver
import threading

from tests import lazy_mock
from tests.lazy_mock import LazyHandler, parse_filter, render


def make_db():
    db = []

    def add(file, added, **tags):
        db.append({"file": file, "Added": added, "Last-Modified": added, **tags})
    # an old album, a newer one, a compilation added track by track over time
    for i in (1, 2):
        add(f"old/{i}.flac", "2015-03-01T10:00:00Z", Title=f"O{i}", Artist="Oldie", AlbumArtist="Oldie",
            Album="Ancient", Date="1971", Track=str(i))
    for i in (1, 2, 3):
        add(f"new/{i}.flac", "2026-11-01T10:00:00Z", Title=f"N{i}", Artist="Newbie", AlbumArtist="Newbie",
            Album="Fresh", Date="2026", Track=str(i))
    add("va/1.flac", "2026-06-01T00:00:00Z", Title="V1", Artist="A", AlbumArtist="Various Artists",
        Album="Mix", Date="2000", Track="1")
    add("va/2.flac", "2026-10-01T00:00:00Z", Title="V2", Artist="B", AlbumArtist="Various Artists",
        Album="Mix", Date="2000", Track="2")
    add("solo/1.flac", "2024-01-01T00:00:00Z", Title="S1", Artist="Solo", Album="Alone", Date="1999", Track="1")
    # Solo also appears on a compilation of the same name: must not join Solo's own "Alone"
    add("va2/1.flac", "2023-01-01T00:00:00Z", Title="S on VA", Artist="Solo", AlbumArtist="Various Artists",
        Album="Alone", Date="2003", Track="1")
    add("loose/x.flac", "2026-11-05T00:00:00Z", Title="No album", Artist="Nobody")  # no album: not an album
    for i in range(30):  # many albums by one artist, added over the years
        add(f"many/{i:02d}.flac", f"20{10 + i % 16:02d}-05-01T00:00:00Z", Title=f"M{i}", Artist="Prolific",
            AlbumArtist="Prolific", Album=f"Record {i:02d}", Date=str(1990 + i))
    return db


class Handler(LazyHandler):
    def respond(self, a):
        cmd, rest = a[0], a[1:]
        if cmd == "list" and rest and rest[0].lower() == "album":
            rest = rest[1:]
            songs = self.db
            if rest and rest[0].startswith("("):
                songs = self.filtered(rest[0])
                if songs is None:
                    return self.ack("list", "Unknown filter type")
                rest = rest[1:]
            groups = [rest[i + 1] for i in range(0, len(rest), 2) if rest[i] == "group"]
            tag = {"albumartist": "AlbumArtist", "artist": "Artist", "date": "Date"}
            rows = sorted({tuple(s.get(tag[g.lower()], "") for g in reversed(groups)) + (s.get("Album", ""),)
                           for s in songs})
            out, last = [], [None] * len(groups)
            for row in rows:
                for i, g in enumerate(reversed(groups)):
                    if row[i] != last[i]:
                        out.append(f"{tag[g.lower()]}: {row[i]}\n")
                        last[i] = row[i]
                        last[i + 1:] = [None] * (len(groups) - i - 1)
                out.append(f"Album: {row[-1]}\n")
            return "".join(out) + "OK\n"
        if cmd == "find" and rest and rest[0].startswith("("):
            Handler.finds.append(list(a))
            songs = self.filtered(rest[0])
            if songs is None:
                return self.ack("find", "Unknown filter type")
            rest = rest[1:]
            if len(rest) >= 2 and rest[0] == "sort":
                field = rest[1].lstrip("-")
                songs = sorted(songs, key=lambda s: s.get(field, ""), reverse=rest[1].startswith("-"))
                rest = rest[2:]
            if len(rest) >= 2 and rest[0] == "window":
                lo, hi = rest[1].split(":")
                songs = songs[int(lo):int(hi)]
            return "".join(render(s) for s in songs) + "OK\n"
        return super().respond(a)


def start(tag_fallback=False):
    Handler.db = make_db()
    Handler.tag_fallback = tag_fallback  # MPD 0.24: missing AlbumArtist matches as Artist
    LazyHandler.log = []  # handle() appends to the base class's log
    Handler.finds = []
    Handler.supports_filters = True
    Handler.max_output = 10 ** 9
    srv = lazy_mock.LazyServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    return srv, srv.server_address[1]
