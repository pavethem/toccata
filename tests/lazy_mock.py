"""A mock MPD with a small in-memory database that really evaluates the queries
the lazy library uses, and imitates MPD's output buffer limit: if one answer
is bigger than `max_output` the server drops the connection, exactly like
MPD does when max_output_buffer_size is exceeded."""
import re
import socketserver
import threading

_TOKEN = re.compile(r'"((?:\\.|[^"\\])*)"|(\S+)')
_Q = r"'((?:\\.|[^'\\])*)'"
_NO_AA = re.compile(r"^\(AlbumArtist == ''\)$")
_UNTAGGED = re.compile(r"^\(\(AlbumArtist == ''\) AND \(Artist == " + _Q + r"\)\)$")
ACK_UNKNOWN = '[5@0] {} unknown command'


def _unescape(s):
    return re.sub(r"\\(.)", r"\1", s)


def tokenize(line):
    return [_unescape(m.group(1)) if m.group(1) is not None else m.group(2)
            for m in _TOKEN.finditer(line)]


def _values(song, tag):
    """All values of a tag (case-insensitive name); lists stand for repeated tags."""
    for k, v in song.items():
        if k.lower() == tag.lower():
            return v if isinstance(v, list) else [v]
    return []


_EXPR_TOKEN = re.compile(r"\s*(\(|\)|AND\b|==|!=|!|'(?:\\.|[^'\\])*'|[A-Za-z_-]+)")


def parse_filter(text, tag_fallback=False):
    """Parse an MPD filter expression into a predicate(song) -> bool.
    tag_fallback: like MPD 0.24, a song without AlbumArtist is matched by
    AlbumArtist against its Artist (so (AlbumArtist == '') misses it).
    Supports what the client sends: (TAG == 'v'), (TAG != 'v'), (A AND B ...),
    nested. Raises ValueError for anything else (the server then ACKs)."""
    tokens, pos = [], 0
    while pos < len(text):
        m = _EXPR_TOKEN.match(text, pos)
        if not m:
            raise ValueError(text)
        tokens.append(m.group(1))
        pos = m.end()
        while pos < len(text) and text[pos].isspace():
            pos += 1
    it = iter(tokens + [None])
    tok = [next(it)]

    def take(expected=None):
        t = tok[0]
        if expected is not None and t != expected:
            raise ValueError(f"expected {expected!r}, got {t!r}")
        tok[0] = next(it)
        return t

    def expr():
        take("(")
        if tok[0] == "!":  # (!(EXPR)): negation
            take("!")
            inner = expr()
            take(")")
            return lambda s: not inner(s)
        if tok[0] in ("added-since", "modified-since"):  # (added-since 'ISO time')
            field = "Added" if take() == "added-since" else "Last-Modified"
            since = _unescape(take()[1:-1])
            take(")")
            return lambda s: (s.get(field) or "") >= since
        if tok[0] == "(":
            parts = [expr()]
            while tok[0] == "AND":
                take("AND")
                parts.append(expr())
            take(")")
            return lambda s: all(p(s) for p in parts)
        tag = take()
        op = take()
        if op not in ("==", "!="):
            raise ValueError(op)
        value = _unescape(take()[1:-1])
        take(")")

        def vals(s):
            v = _values(s, tag)
            if not v and tag_fallback and tag.lower() == "albumartist":
                v = _values(s, "Artist")
            return v
        if op == "==":
            return lambda s: (value in vals(s)) if value else not any(vals(s))
        return lambda s: not ((value in vals(s)) if value else not any(vals(s)))
    result = expr()
    if tok[0] is not None:
        raise ValueError(text)
    return result


def make_db():
    db = []

    def add(file, **tags):
        db.append({"file": file, **tags})
    for i in (1, 2, 3):
        add(f"band/debut/{i}.flac", Title=f"Song {i}", Artist="Band", AlbumArtist="Band",
            Album="Debut", Track=str(i), Date="2019", duration="180.0")
    add("band/second/1.flac", Title="Later", Artist="Band", AlbumArtist="Band",
        Album="Second", Track="1", Date="2021")
    for i in (1, 2):
        add(f"gnr/{i}.flac", Title=f"G{i}", Artist="Guns N' Roses", AlbumArtist="Guns N' Roses",
            Album="Appetite", Track=str(i))
    for i, a in enumerate(["A", "B", "C"], 1):
        add(f"va/{i}.flac", Title=f"V{i}", Artist=a, AlbumArtist="Various Artists", Album="Hits", Track=str(i))
    for i in (1, 2):  # no AlbumArtist tag: appear under their artist
        add(f"solo/{i}.flac", Title=f"S{i}", Artist="Solo", Album="Solo Album", Track=str(i))
    add("motley/1.flac", Title="Kickstart", Artist="M\u00f6tley N' Cr\u00fce", Album="Dr", Track="1")
    add("loose/untagged.flac")  # no tags at all
    for i in range(600):
        add(f"big/{i:04d}.flac", Title=f"T{i}", Artist="Big Band", AlbumArtist="Big Band",
            Album=f"Album {i // 100}", Track=str(i % 100 + 1))
    return db


def genre_db():
    """make_db() plus songs with genres (one with two genres, one without AlbumArtist)."""
    db = make_db()

    def add(file, **tags):
        db.append({"file": file, **tags})
    add("jazz/1.flac", Title="J1", Artist="Hornist", AlbumArtist="Hornist", Album="Blue", Track="1",
        Genre="Jazz")
    add("jazz/2.flac", Title="J2", Artist="Hornist", AlbumArtist="Hornist", Album="Blue", Track="2",
        Genre=["Jazz", "Funk"])
    add("jazz/3.flac", Title="J3", Artist="Hornist", AlbumArtist="Hornist", Album="Loud", Track="1",
        Genre="Rock")
    add("jazz/4.flac", Title="J4", Artist="Busker", Album="Street", Track="1", Genre="Jazz")
    add("jazz/5.flac", Title="J5", Album="Nameless", Track="1", Genre="Jazz")  # no artist at all
    add("rock/1.flac", Title="R1", Artist="Band", AlbumArtist="Band", Album="Debut", Track="9", Genre="Rock")
    add("quiet/1.flac", Title="Q", Artist="Quiet", AlbumArtist="Quiet", Album="Hush", Genre="")  # empty genre tag
    return db


def composer_db():
    """Classical music for the composer-genre tests."""
    db = []

    def add(file, **tags):
        db.append({"file": file, **tags})
    add("bach/1.flac", Title="Concerto 1", Artist="BP", AlbumArtist="Berliner Philharmoniker",
        Album="Brandenburg", Track="1", Genre="Classical", Composer="Johann Sebastian Bach")
    add("bach/2.flac", Title="Concerto 2", Artist="BP", AlbumArtist="Berliner Philharmoniker",
        Album="Brandenburg", Track="2", Genre="Classical", Composer="Johann Sebastian Bach")
    add("film/1.flac", Title="Theme", Artist="BP", AlbumArtist="Berliner Philharmoniker",
        Album="Film", Track="1", Genre="Soundtrack", Composer="Hans Zimmer")  # not classical: stays
    add("figaro/1.flac", Title="Overture", Artist="Wiener", AlbumArtist="Wiener Philharmoniker",
        Album="Figaro", Track="1", Genre=["Classical", "Opera"], Composer="Mozart")
    add("chant/1.flac", Title="Kyrie", Artist="Anon Ensemble", AlbumArtist="Anon Ensemble",
        Album="Chant", Track="1", Genre="Classical")  # classical without a composer: stays
    add("rock/1.flac", Title="R1", Artist="Band", AlbumArtist="Band", Album="Debut", Track="1", Genre="Rock",
        Composer="Band")  # a composer, but not in a composer genre
    add("nocturne/1.flac", Title="Nocturne", Artist="Solo Pianist", Album="Nocturnes", Track="1",
        Genre="Classical", Composer="Chopin")  # no album artist, a composer song
    return db


def render(song):
    out = [f"file: {song['file']}\n"]
    for k, v in song.items():
        if k != "file":
            out += [f"{k}: {x}\n" for x in (v if isinstance(v, list) else [v])]
    return "".join(out)


class LazyHandler(socketserver.StreamRequestHandler):
    db = []
    max_output = 10 ** 9           # bytes per answer before the "buffer" overflows
    supports_filters = True        # MPD >= 0.21
    supports_window = True         # MPD >= 0.20
    falls_back_itself = False      # pretend find/albumartist also returns untagged songs
    fail_find = False              # answer every `find` with an ACK
    db_update = None               # `stats` db_update (Unix time); None: no `stats`
    log = []                       # every command, tokenised

    def handle(self):
        try:
            self.wfile.write(b"OK MPD 0.23.5\n")
            for raw in self.rfile:
                args = tokenize(raw.decode().strip())
                if not args:
                    continue
                LazyHandler.log.append(args)
                body = self.respond(args)
                if len(body) > self.max_output:
                    return  # MPD closes the connection when its output buffer overflows
                self.wfile.write(body.encode())
        except (ConnectionResetError, BrokenPipeError):
            return  # the client hung up abruptly (e.g. abort()): not an error for a server

    def ack(self, cmd, text="unknown command"):
        return f"ACK [5@0] {{{cmd}}} {text}\n"

    def filtered(self, expr):
        if not self.supports_filters:
            return None
        try:
            match = parse_filter(expr, getattr(self, "tag_fallback", False))
        except ValueError:
            return None
        return [s for s in self.db if match(s)]

    def old_pairs(self, args):
        """Old-style `TAG VALUE TAG VALUE ...` filter (ANDed), or None."""
        if len(args) % 2:
            return None
        pairs = list(zip(args[::2], args[1::2]))
        return [s for s in self.db if all(v in _values(s, t) for t, v in pairs)]

    def respond(self, a):
        cmd = a[0]
        if cmd == "status":
            return "state: stop\nOK\n"
        if cmd == "stats" and self.db_update is not None:
            return f"artists: 1\ndb_update: {self.db_update}\nOK\n"
        if cmd == "list" and len(a) == 2 and a[1].lower() == "albumartist":
            vals = sorted({s["AlbumArtist"] for s in self.db if s.get("AlbumArtist")})
            return "".join(f"AlbumArtist: {v}\n" for v in vals) + "OK\n"
        if cmd == "list" and a[1].lower() == "composer":
            songs = self.db if len(a) == 2 else self.filtered(a[2])
            if songs is None:
                return self.ack("list")
            vals = sorted({c for s in songs for c in _values(s, "Composer") if c})
            return "".join(f"Composer: {v}\n" for v in vals) + "OK\n"
        if cmd == "list" and len(a) == 2 and a[1].lower() == "genre":
            vals = sorted({g for s in self.db for g in _values(s, "Genre")})
            return "".join(f"Genre: {v}\n" for v in vals) + "OK\n"
        if cmd == "list" and a[1].lower() == "albumartist" and len(a) >= 3:
            songs = self.filtered(a[2]) if len(a) == 3 else self.old_pairs(a[2:])
            if songs is None:
                return self.ack("list")
            vals = sorted({s["AlbumArtist"] for s in songs if s.get("AlbumArtist")})
            return "".join(f"AlbumArtist: {v}\n" for v in vals) + "OK\n"
        if cmd == "list" and len(a) == 3 and a[1].lower() == "artist":
            songs = self.filtered(a[2])
            if songs is None:
                return self.ack("list")
            vals = sorted({s["Artist"] for s in songs if s.get("Artist")})
            return "".join(f"Artist: {v}\n" for v in vals) + "OK\n"
        if cmd == "count" and len(a) == 2:
            songs = self.filtered(a[1])
            return self.ack("count") if songs is None else f"songs: {len(songs)}\nplaytime: 0\nOK\n"
        if cmd == "find":
            if self.fail_find:
                return self.ack("find", "No such directory")
            rest, window = a[1:], None
            if len(rest) >= 2 and rest[-2] == "window":
                if not self.supports_window:
                    return self.ack("find", "Unknown filter type")
                lo, hi = rest[-1].split(":")
                window, rest = (int(lo), int(hi)), rest[:-2]
            order = None
            if len(rest) >= 3 and rest[-2] == "sort":  # sort -Tag: descending
                order, rest = rest[-1], rest[:-2]
            if len(rest) == 2 and rest[0].lower() == "albumartist":
                songs = [s for s in self.db if s.get("AlbumArtist") == rest[1]]
                if self.falls_back_itself:
                    songs += [s for s in self.db if not s.get("AlbumArtist") and s.get("Artist") == rest[1]]
            elif len(rest) >= 4 and rest[0].lower() == "albumartist":
                songs = self.old_pairs(rest)
            elif len(rest) == 1 and rest[0].startswith("("):
                songs = self.filtered(rest[0])
                if songs is None:
                    return self.ack("find", "Unknown filter type")
            else:
                return self.ack("find")
            if order:
                tag = order.lstrip("-")
                songs = sorted(songs, key=lambda s: (_values(s, tag) or [""])[0], reverse=order.startswith("-"))
            if window:
                songs = songs[window[0]:window[1]]
            return "".join(render(s) for s in songs) + "OK\n"
        return self.ack(cmd)


class LazyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start(db=None, **settings):
    """Start a server; returns (server, port). Settings are LazyHandler attributes."""
    LazyHandler.db = db if db is not None else make_db()
    LazyHandler.log = []
    LazyHandler.max_output = 10 ** 9
    LazyHandler.supports_filters = True
    LazyHandler.supports_window = True
    LazyHandler.falls_back_itself = False
    LazyHandler.fail_find = False
    LazyHandler.db_update = None
    for k, v in settings.items():
        setattr(LazyHandler, k, v)
    srv = LazyServer(("127.0.0.1", 0), LazyHandler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    return srv, srv.server_address[1]
