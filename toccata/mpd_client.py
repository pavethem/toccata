"""Minimal, dependency-free MPD protocol client.

MPD speaks a simple line-based text protocol, so we implement just what the
UI needs instead of depending on python-mpd2. Not thread-safe: use one
instance per thread (the UI uses one for commands, one for `idle`).
"""
import re
import socket
from dataclasses import dataclass, field

DEFAULT_PORT = 6600
MAX_BINARY = 32 * 1024 * 1024  # refuse covers larger than 32 MiB
REPLAY_GAIN_MODES = ("off", "track", "album", "auto")
ACK_PASSWORD, ACK_PERMISSION = 3, 4  # MPD protocol error codes


class MPDError(Exception):
    pass


class ProtocolError(MPDError):
    pass


class CommandError(MPDError):
    """MPD answered with an ACK line."""


_ACK_RE = re.compile(r"ACK \[(\d+)@\d+\] \{[^}]*\} ?(.*)")


def ack_code(exc):
    """The numeric MPD error code of an ACK, or None."""
    m = _ACK_RE.search(str(exc))
    return int(m.group(1)) if m else None


def describe_error(exc, host=None, port=None):
    """A short, human-readable explanation of a connection/command failure."""
    where = f" at {host}:{port}" if host and port else ""
    if isinstance(exc, CommandError):
        code = ack_code(exc)
        if code == ACK_PASSWORD:
            return "Wrong password (or the server has no password set)."
        if code == ACK_PERMISSION:
            return "Permission denied: this server needs a password with more rights."
        m = _ACK_RE.search(str(exc))
        return m.group(2) if m and m.group(2) else str(exc)
    if isinstance(exc, ConnectionRefusedError):
        return f"Connection refused{where}. Is MPD running and listening there?"
    if isinstance(exc, socket.gaierror):
        return f"Cannot find host{where.replace(' at ', ' ') if where else ''}: unknown host name."
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return f"Timed out connecting{where}."
    if isinstance(exc, FileNotFoundError):
        return "Socket not found. Check the path."
    if isinstance(exc, PermissionError):
        return "No permission to use that socket."
    if isinstance(exc, ProtocolError) and "Unexpected greeting" in str(exc):
        return f"That does not look like an MPD server{where}."
    return str(exc)


@dataclass
class Output:
    """An MPD audio output. NB: `id` is only valid until the next change (moving
    an output between partitions renumbers them), so never keep it around."""
    id: int
    name: str = ""
    plugin: str = ""
    enabled: bool = False
    partition: str = ""  # which partition it belongs to; "" if unknown
    attributes: dict = field(default_factory=dict)


def parse_outputs(pairs, partition=""):
    outputs, cur = [], None
    for key, value in pairs:
        k = key.lower()
        if k == "outputid":
            try:
                cur = Output(int(value), partition=partition)
            except ValueError:
                raise ProtocolError(f"Bad output id: {value!r}") from None
            outputs.append(cur)
        elif cur is None:
            continue
        elif k == "outputname":
            cur.name = value
        elif k == "plugin":
            cur.plugin = value
        elif k == "outputenabled":
            cur.enabled = value == "1"
        elif k == "attribute":
            name, _, val = value.partition("=")
            cur.attributes[name] = val
    return outputs


def quote(arg) -> str:
    s = str(arg).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _lower_pairs(pairs):
    return [(k.lower(), v) for k, v in pairs]


def group_songs(pairs):
    """Split a flat (key, value) list into song dicts; 'file' starts a song."""
    songs, cur = [], None
    for k, v in _lower_pairs(pairs):
        if k in ("directory", "playlist"):
            cur = None  # their Last-Modified etc. belong to them, not the previous song
            continue
        if k == "file":
            cur = {}
            songs.append(cur)
        if cur is not None:
            cur[k] = v
    return songs


class MPDClient:
    def __init__(self, host="localhost", port=DEFAULT_PORT, password=None, timeout=10, partition=None):
        self.host, self.port, self.password, self.timeout = host, port, password, timeout
        self.partition = partition        # entered right after connecting (MPD >= 0.22)
        self.partition_failed = False     # True if the server refused it; we then stay in `default`
        self._sock = None
        self._rfile = None
        self.protocol_version = None

    # -- connection ---------------------------------------------------------
    def connect(self):
        self.close()
        if self.host.startswith("/") or self.host.startswith("@"):
            host = "\0" + self.host[1:] if self.host.startswith("@") else self.host
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(self.timeout)
            s.connect(host)
        else:
            s = socket.create_connection((self.host, self.port), self.timeout)
        self._sock = s
        self._rfile = s.makefile("rb")
        greeting = self._readline()
        if not greeting.startswith("OK MPD "):
            self.close()
            raise ProtocolError(f"Unexpected greeting: {greeting!r}")
        self.protocol_version = greeting[len("OK MPD "):]
        if self.password:
            try:
                self.command("password", self.password)
            except MPDError:
                self.close()  # do not stay half-connected after a failed login
                raise
        self.partition_failed = False
        if self.partition:
            try:
                self.command("partition", self.partition)
            except CommandError:
                self.partition_failed = True  # unknown or unsupported: stay in `default`

    def close(self):
        for obj in (self._rfile, self._sock):
            try:
                if obj:
                    obj.close()
            except OSError:
                pass
        self._rfile = self._sock = None

    @property
    def connected(self):
        return self._sock is not None

    def abort(self):
        """Interrupt a blocking call (e.g. idle) running in another thread.
        The blocked thread gets an error and should close() the client."""
        s = self._sock
        if s is not None:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    # -- low level ----------------------------------------------------------
    def _readline(self) -> str:
        line = self._rfile.readline()
        if not line:
            self.close()
            raise ProtocolError("Connection closed by server")
        return line.decode("utf-8", "replace").rstrip("\n")

    def _send(self, name, *args):
        if not self._sock:
            raise ProtocolError("Not connected")
        parts = [name] + [quote(a) for a in args]
        try:
            self._sock.sendall((" ".join(parts) + "\n").encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError):
            # The server already closed this connection (e.g. MPD's connection_timeout,
            # 60 s without a command), so it never ran the command: drop the socket so
            # call_with_reconnect sees "not connected" and retries on a fresh one.
            self.close()
            raise

    def _read_response(self, want_binary=False):
        pairs, binary = [], None
        while True:
            line = self._readline()
            if line == "OK":
                return (pairs, binary) if want_binary else pairs
            if line.startswith("ACK "):
                raise CommandError(line)
            key, sep, value = line.partition(": ")
            if not sep:
                raise ProtocolError(f"Bad response line: {line!r}")
            if key == "binary":
                n = int(value)
                binary = self._rfile.read(n)
                self._rfile.readline()  # trailing newline after the blob
                continue
            pairs.append((key, value))

    def command(self, name, *args):
        self._send(name, *args)
        return self._read_response()

    def command_dict(self, name, *args):
        return dict(_lower_pairs(self.command(name, *args)))

    def command_songs(self, name, *args):
        return group_songs(self.command(name, *args))

    def iter_songs(self, name, *args, keep=None):
        """Stream songs from a listing command (e.g. listallinfo) one by one,
        without holding the whole response in memory.

        keep: optional set of lowercase keys to retain per song (must include
        'file'); everything else is dropped to save memory on big libraries.
        Directory and playlist entries are skipped. The generator MUST be
        consumed to the end, or the connection is left mid-response.
        """
        self._send(name, *args)
        cur = None
        while True:
            line = self._readline()
            if line == "OK":
                break
            if line.startswith("ACK "):
                raise CommandError(line)
            key, sep, value = line.partition(": ")
            if not sep:
                raise ProtocolError(f"Bad response line: {line!r}")
            k = key.lower()
            if k in ("directory", "playlist"):
                if cur is not None:
                    yield cur
                cur = None
                continue
            if k == "file":
                if cur is not None:
                    yield cur
                cur = {"file": value}
                continue
            if cur is not None and (keep is None or k in keep):
                cur[k] = value
        if cur is not None:
            yield cur

    def command_list(self, commands):
        """Send several commands as one command list, e.g.
        [("add", "a.flac"), ("add", "b.flac")]. One round trip; MPD stops at
        the first failing command and answers with a single ACK."""
        if not commands:
            return []
        if not self._sock:
            raise ProtocolError("Not connected")
        lines = ["command_list_begin"]
        for name, *args in commands:
            lines.append(" ".join([name] + [quote(a) for a in args]))
        lines.append("command_list_end")
        self._sock.sendall(("\n".join(lines) + "\n").encode("utf-8"))
        return self._read_response()

    def add_many(self, uris, chunk=500, position=None):
        """Add many songs to the queue, in command lists of `chunk` songs: at
        the end, or from queue index `position` on (needs MPD >= 0.23)."""
        uris = list(uris)
        for i in range(0, len(uris), chunk):
            part = uris[i:i + chunk]
            if position is None:
                self.command_list([("add", u) for u in part])
            else:
                self.command_list([("add", u, str(position + i + n)) for n, u in enumerate(part)])

    def move_ids(self, moves):
        """Apply [(songid, new_position)] as one command list."""
        self.command_list([("moveid", i, str(to)) for i, to in moves])

    def replace_queue(self, uris, play=True):
        """Clear the queue, add `uris`, and optionally start playing."""
        self.clear()
        self.add_many(uris)
        if play:
            self.play()

    def command_list_ok(self, commands):
        """Like command_list, but returns one list of (key, value) pairs per
        command (MPD separates their answers with 'list_OK')."""
        if not commands:
            return []
        if not self._sock:
            raise ProtocolError("Not connected")
        lines = ["command_list_ok_begin"]
        for name, *args in commands:
            lines.append(" ".join([name] + [quote(a) for a in args]))
        lines.append("command_list_end")
        self._sock.sendall(("\n".join(lines) + "\n").encode("utf-8"))
        results, cur = [], []
        while True:
            line = self._readline()
            if line == "OK":
                return results
            if line == "list_OK":
                results.append(cur)
                cur = []
                continue
            if line.startswith("ACK "):
                raise CommandError(line)
            key, sep, value = line.partition(": ")
            if not sep:
                raise ProtocolError(f"Bad response line: {line!r}")
            cur.append((key, value))

    # -- partitions (MPD >= 0.22) -------------------------------------------
    def listpartitions(self):
        """Names of all partitions, or None if the server has no partitions."""
        try:
            return [v for k, v in self.command("listpartitions") if k.lower() == "partition"]
        except CommandError:
            return None

    def current_partition(self):
        return self.status().get("partition") or "default"

    def switch_partition(self, name):
        """Make this connection use another partition (also on reconnect)."""
        self.command("partition", name)
        self.partition = name

    def new_partition(self, name):
        self.command("newpartition", name)

    def delete_partition(self, name):
        self.command("delpartition", name)

    # -- outputs -------------------------------------------------------------
    def outputs(self, partition=""):
        """Outputs of the partition this connection is in."""
        return parse_outputs(self.command("outputs"), partition)

    def set_output_enabled(self, output_id, enabled):
        self.command("enableoutput" if enabled else "disableoutput", output_id)

    def set_output_enabled_by_name(self, name, enabled):
        """Enable/disable an output of this partition by its name. Looks the id up
        right now, because ids change when outputs are moved between partitions."""
        for o in self.outputs():
            if o.name == name:
                self.set_output_enabled(o.id, enabled)
                return
        raise CommandError(f"ACK [50@0] {{{'enableoutput' if enabled else 'disableoutput'}}} No such audio output: {name}")

    def move_output(self, name):
        """Move the named output (from whichever partition) into this connection's partition."""
        self.command("moveoutput", name)

    def all_outputs(self):
        """Outputs of every partition, each tagged with its partition.

        One atomic round trip: the connection hops through the other
        partitions and back. If that fails halfway the connection is switched
        back before the error is raised; if even that fails it is closed, so
        it can never be left sitting in a partition the caller does not expect.
        """
        names = self.listpartitions()
        here = self.current_partition()
        if not names or len(names) < 2:
            return self.outputs(here)
        others = [n for n in names if n != here]
        commands = [("outputs",)]
        for n in others:
            commands += [("partition", n), ("outputs",)]
        commands.append(("partition", here))
        try:
            results = self.command_list_ok(commands)
        except (CommandError, ProtocolError, OSError):
            try:
                self.command("partition", here)
            except (MPDError, OSError):
                self.close()  # state unknown: force a clean reconnect
            raise
        found = parse_outputs(results[0], here)
        for i, n in enumerate(others):
            found += parse_outputs(results[2 + 2 * i], n)
        return found

    # -- idle ---------------------------------------------------------------
    def idle(self, *subsystems):
        """Block until MPD reports a change; returns list of subsystem names."""
        self._send("idle", *subsystems)
        saved = self._sock.gettimeout()
        self._sock.settimeout(None)
        try:
            pairs = self._read_response()
        finally:
            if self._sock:
                self._sock.settimeout(saved)
        return [v for k, v in pairs if k == "changed"]

    # -- high level ---------------------------------------------------------
    def status(self):
        return self.command_dict("status")

    def currentsong(self):
        return self.command_dict("currentsong")

    def queue(self):
        return self.command_songs("playlistinfo")

    def replay_gain_status(self):
        """Current replay gain mode ('off'/'track'/'album'/'auto'), or None if
        the server does not support it (e.g. non-MPD servers)."""
        try:
            return self.command_dict("replay_gain_status").get("replay_gain_mode")
        except CommandError:
            return None

    def set_replay_gain_mode(self, mode):
        if mode not in REPLAY_GAIN_MODES:
            raise ValueError(f"invalid replay gain mode: {mode!r}")
        self.command("replay_gain_mode", mode)

    def music_directory(self):
        """Only available over a local (unix socket) connection."""
        try:
            return self.command_dict("config").get("music_directory")
        except CommandError:
            return None

    def list_artists(self):
        vals = [v for k, v in self.command("list", "albumartist") if k.lower() == "albumartist"]
        return sorted({v for v in vals if v}, key=str.casefold)

    def tracks_for_artist(self, artist):
        songs = self.command_songs("find", "albumartist", artist)
        songs.sort(key=lambda s: (s.get("album", "").casefold(),
                                  _num(s.get("disc")), _num(s.get("track"))))
        return songs

    def search(self, text):
        return self.command_songs("search", "any", text)

    def tagtypes(self):
        """Tag names this server supports (and has enabled for this client)."""
        return [v for k, v in self.command("tagtypes") if k.lower() == "tagtype"]

    # -- stored playlists ---------------------------------------------------
    def listplaylists(self):
        """[(name, last_modified)] of every stored playlist."""
        out, name = [], None
        for k, v in _lower_pairs(self.command("listplaylists")):
            if k == "playlist":
                name = v
                out.append((v, ""))
            elif k == "last-modified" and name is not None:
                out[-1] = (name, v)
        return out

    def listplaylistinfo(self, name):
        return self.command_songs("listplaylistinfo", name)

    def load_playlist(self, name, position=None):
        """Append a stored playlist to the queue (at `position`, MPD >= 0.23)."""
        if position is None:
            self.command("load", name)
        else:
            self.command("load", name, "0:", str(position))

    def save_playlist(self, name, overwrite=False):
        """Save the queue as a playlist. With overwrite an existing one is
        replaced (rm + save; works on every MPD version)."""
        if overwrite:
            try:
                self.command("rm", name)
            except CommandError:
                pass  # it did not exist after all
        self.command("save", name)

    def rename_playlist(self, old, new):
        self.command("rename", old, new)

    def delete_playlist(self, name):
        self.command("rm", name)

    def playlist_add(self, name, uris, chunk=500):
        """Append songs to a stored playlist (created if missing)."""
        uris = list(uris)
        for i in range(0, len(uris), chunk):
            self.command_list([("playlistadd", name, u) for u in uris[i:i + chunk]])

    def playlist_delete(self, name, positions):
        """Remove entries by position. Highest first, so the others keep their
        positions; one command list (MPD stops at the first failure)."""
        self.command_list([("playlistdelete", name, str(p)) for p in sorted(set(positions), reverse=True)])

    def playlist_move(self, name, src, dst):
        self.command("playlistmove", name, str(src), str(dst))

    def _fetch_binary(self, cmd, uri):
        """Download a binary blob (albumart/readpicture) in chunks.

        Returns bytes, or None if there is nothing (ACK, empty answer, or a
        server that stops delivering data). Capped at MAX_BINARY so a broken
        or hostile server cannot make us loop or allocate forever.
        """
        data, offset = b"", 0
        while True:
            try:
                self._send(cmd, uri, offset)
                pairs, chunk = self._read_response(want_binary=True)
            except CommandError:
                return None
            if not chunk:  # no picture, or server delivered nothing more
                return None
            data += chunk
            try:
                size = int(dict(_lower_pairs(pairs)).get("size", len(data)))
            except ValueError:
                return None
            offset = len(data)
            if offset >= size:
                return data
            if offset > MAX_BINARY:
                return None

    def set_binary_limit(self, size):
        """Bigger chunks for albumart/readpicture (MPD >= 0.22.4). Returns False if
        the server does not know the command; covers then come in 8 KiB pieces."""
        try:
            self.command("binarylimit", str(int(size)))
            return True
        except CommandError:
            return False

    def albumart(self, uri):
        """Folder art (cover.jpg etc.) for a song; MPD >= 0.21. Bytes or None."""
        return self._fetch_binary("albumart", uri)

    def readpicture(self, uri):
        """Picture embedded in the file's tags; MPD >= 0.22. Bytes or None."""
        return self._fetch_binary("readpicture", uri)

    def cover(self, uri):
        """Best available cover: folder art first, then embedded picture."""
        data = self.albumart(uri)
        return data if data is not None else self.readpicture(uri)

    # playback
    def play(self, pos=None):
        self.command("play", *([] if pos is None else [pos]))

    def playid(self, songid):
        self.command("playid", songid)

    def toggle_pause(self):
        self.command("pause")

    def stop(self):
        self.command("stop")

    def next(self):
        self.command("next")

    def previous(self):
        self.command("previous")

    def setvol(self, v):
        self.command("setvol", int(v))

    def set_repeat(self, on):
        self.command("repeat", "1" if on else "0")

    def set_random(self, on):
        self.command("random", "1" if on else "0")

    def set_single(self, on):
        self.command("single", "1" if on else "0")

    def set_consume(self, on):
        self.command("consume", "1" if on else "0")

    def set_play_option(self, name, on):
        """repeat / single / random / consume on or off."""
        if name not in ("repeat", "single", "random", "consume"):
            raise ValueError(name)
        self.command(name, "1" if on else "0")

    def seekcur(self, seconds):
        self.command("seekcur", f"{seconds:.3f}")

    # queue editing
    def add(self, uri):
        self.command("add", uri)

    def addid(self, uri):
        """Add a song at the end of the queue; its new queue id."""
        return self.command_dict("addid", uri)["id"]

    def clear(self):
        self.command("clear")

    def deleteid(self, songid):
        self.command("deleteid", songid)

    def update(self, uri=None):
        self.command("update", *([] if uri is None else [uri]))


def _num(s):
    try:
        return int(str(s).split("/")[0])
    except (TypeError, ValueError):
        return 0


def probe(host, port, password=None, timeout=5):
    """Try a connection with the given details, for the settings dialog.

    Runs `status` after connecting, because MPD only checks a password when a
    command needs it. Returns {'version', 'music_directory', 'replaygain'};
    raises OSError / MPDError on failure (use describe_error for text).
    """
    c = MPDClient(host, port, password, timeout)
    try:
        c.connect()
        c.status()
        return {"version": c.protocol_version,
                "music_directory": c.music_directory(),
                "replaygain": c.replay_gain_status()}
    finally:
        c.close()
