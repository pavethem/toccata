"""A stateful mock MPD for partitions and outputs.

Mirrors the behaviours that matter:
  * the current partition belongs to the *connection*;
  * `outputs` lists only the current partition's outputs, and `outputid` is the
    position in that list (so ids change when outputs are moved);
  * `moveoutput NAME` pulls the output into the connection's partition;
  * `delpartition` merges the partition's outputs into `default`;
  * command_list_ok_begin answers each command with list_OK, and stops at the
    first ACK.
"""
import socketserver
import threading

from tests.lazy_mock import tokenize

NO_PARTITIONS = "ACK [5@0] {%s} unknown command \"%s\"\n"


def fresh_state():
    return {
        "partitions": {"default": ["Speakers", "Headphones"], "party": ["Kitchen"]},
        "enabled": {"Speakers": True, "Headphones": False, "Kitchen": True},
        "plugins": {"Speakers": "alsa", "Headphones": "pulse", "Kitchen": "httpd"},
        # a queue of (id, file, title) and the player; tests change these between updates
        "queue": [],
        "player": {"state": "stop", "songid": None},
    }


class PartHandler(socketserver.StreamRequestHandler):
    state = fresh_state()
    log = []
    idle_log = []  # the partition each `idle` was issued in, in order
    supports_partitions = True
    deny = set()  # partition names this server refuses to switch to

    def handle(self):
        self.cur = "default"
        self.list_mode, self.queue, self.n_idle = None, [], 0
        try:
            self.wfile.write(b"OK MPD 0.23.5\n")
            for raw in self.rfile:
                args = tokenize(raw.decode().strip())
                if not args:
                    continue
                PartHandler.log.append(args)
                cmd = args[0]
                if cmd in ("command_list_begin", "command_list_ok_begin"):
                    self.list_mode, self.queue = ("ok" if cmd == "command_list_ok_begin" else "plain"), []
                    continue
                if cmd == "command_list_end":
                    self.finish_list()
                    continue
                if self.list_mode:
                    self.queue.append(args)
                    continue
                if cmd == "idle":
                    PartHandler.idle_log.append(self.cur)
                    self.n_idle += 1
                    if self.n_idle > 1:
                        self.rfile.readline()  # block until the client goes away
                        return
                    self.wfile.write(b"changed: output\nOK\n")
                    continue
                body, err = self.run(args)
                self.wfile.write((err if err else body + "OK\n").encode())
        except (ConnectionResetError, BrokenPipeError):
            return

    def finish_list(self):
        out = []
        for args in self.queue:
            body, err = self.run(args)
            if err:
                self.wfile.write(err.encode())  # MPD stops at the first failure
                self.list_mode = None
                return
            out.append(body + ("list_OK\n" if self.list_mode == "ok" else ""))
        self.list_mode = None
        self.wfile.write(("".join(out) + "OK\n").encode())

    def ack(self, cmd, code=50, text="No such object"):
        return "", f"ACK [{code}@0] {{{cmd}}} {text}\n"

    def run(self, a):
        cmd, st = a[0], self.state
        parts = st["partitions"]
        if cmd == "status":
            pl = st["player"]
            body = f"state: {pl['state']}\n"
            if pl["songid"] is not None:
                body += f"songid: {pl['songid']}\n"
            return body + (f"partition: {self.cur}\n" if self.supports_partitions else ""), None
        if cmd in ("listpartitions", "partition", "newpartition", "delpartition", "moveoutput") \
                and not self.supports_partitions:
            return "", NO_PARTITIONS % (cmd, cmd)
        if cmd == "listpartitions":
            return "".join(f"partition: {n}\n" for n in parts), None
        if cmd == "partition":
            if a[1] not in parts or a[1] in self.deny:
                return self.ack(cmd, text="No such partition")
            self.cur = a[1]
            return "", None
        if cmd == "newpartition":
            if a[1] in parts:
                return self.ack(cmd, 56, "Partition already exists")
            parts[a[1]] = []
            return "", None
        if cmd == "delpartition":
            if a[1] == "default" or a[1] not in parts:
                return self.ack(cmd, text="Cannot delete this partition")
            parts["default"] += parts.pop(a[1])
            return "", None
        if cmd == "outputs":
            body = ""
            for i, name in enumerate(parts[self.cur]):
                body += (f"outputid: {i}\noutputname: {name}\nplugin: {st['plugins'][name]}\n"
                         f"outputenabled: {int(st['enabled'][name])}\n")
                if st["plugins"][name] == "alsa":
                    body += "attribute: dop=0\n"
            return body, None
        if cmd in ("enableoutput", "disableoutput"):
            try:
                name = parts[self.cur][int(a[1])]
            except (ValueError, IndexError):
                return self.ack(cmd, text="No such audio output")
            st["enabled"][name] = cmd == "enableoutput"
            return "", None
        if cmd == "moveoutput":
            for owner, names in parts.items():
                if a[1] in names:
                    names.remove(a[1])
                    parts[self.cur].append(a[1])
                    return "", None
            return self.ack(cmd, text="No such audio output")
        # what a window asks for while starting up: an empty library and queue
        if cmd == "playlistinfo":
            return "".join(f"file: {f}\nTitle: {t}\nPos: {i}\nId: {sid}\n"
                           for i, (sid, f, t) in enumerate(st["queue"])), None
        if cmd == "currentsong":
            cur = [(sid, f, t) for sid, f, t in st["queue"] if str(sid) == str(st["player"]["songid"])]
            return (f"file: {cur[0][1]}\nTitle: {cur[0][2]}\nId: {cur[0][0]}\n" if cur else ""), None
        if cmd in ("list", "find"):
            return "", None
        if cmd == "count":
            return "songs: 0\nplaytime: 0\n", None
        if cmd == "replay_gain_status":
            return "replay_gain_mode: off\n", None
        if cmd == "config":
            return self.ack(cmd, 4, "you are not on a local connection")
        return "", f"ACK [5@0] {{{cmd}}} unknown command\n"


class PartServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start(**settings):
    PartHandler.state = fresh_state()
    PartHandler.log = []
    PartHandler.idle_log = []
    PartHandler.supports_partitions = True
    PartHandler.deny = set()
    for k, v in settings.items():
        setattr(PartHandler, k, v)
    srv = PartServer(("127.0.0.1", 0), PartHandler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    return srv, srv.server_address[1]
