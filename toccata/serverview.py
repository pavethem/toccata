"""What the Outputs / Partitions / Collections pages show, as plain data, so the
rules (what can be switched, deleted, moved) are testable without GTK."""
from dataclasses import dataclass

DEFAULT_PARTITION = "default"


@dataclass
class PartitionRow:
    name: str
    current: bool
    can_switch: bool
    can_delete: bool


def partition_rows(names, current):
    """One row per partition. `default` can never be deleted, and neither can the
    partition this connection is in (MPD would pull it from under us)."""
    return [PartitionRow(n, n == current, n != current, n != current and n != DEFAULT_PARTITION)
            for n in names]


def split_outputs(outputs, current):
    """(here, elsewhere): outputs of the current partition (can be switched on and
    off) and outputs that belong to other partitions (can be moved here)."""
    here = [o for o in outputs if not o.partition or o.partition == current]
    elsewhere = [o for o in outputs if o.partition and o.partition != current]
    return here, elsewhere


def collection_subtitle(c, active_partition_name=None):
    """'host:port' plus the partition if one is set, e.g. 'nas:6600 \u00b7 party'."""
    text = f"{c.host}:{c.port}"
    if c.partition and c.partition != DEFAULT_PARTITION:
        text += f" \u00b7 {c.partition}"
    return text


def window_subtitle(collection_name, partition):
    """Text under the window title: 'Home', 'Home \u00b7 party', 'party' or ''.
    The collection name is left out by the caller when there is only one."""
    shown = partition if partition and partition != DEFAULT_PARTITION else ""
    return " \u00b7 ".join(x for x in (collection_name, shown) if x)


# -- server information (like Cantata's "Server Information" dialog) ---------------

def format_span(seconds):
    """A long time span: '3 days 4:05:06', '1 day 0:00:10', '4:05:06'."""
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    days, rem = divmod(max(s, 0), 86400)
    h, rem = divmod(rem, 3600)
    m, sec = divmod(rem, 60)
    clock = f"{h}:{m:02d}:{sec:02d}"
    if not days:
        return clock
    return f"{days} day{'s' if days != 1 else ''} {clock}"


def _count(value):
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return ""


def _when(timestamp):
    import datetime
    try:
        t = datetime.datetime.fromtimestamp(int(timestamp))
    except (TypeError, ValueError, OverflowError, OSError):
        return ""
    return t.strftime("%Y-%m-%d %H:%M")


def fetch_server_info(client):
    """Everything the page shows, in a few small requests (run it off the UI
    thread: `stats` takes MPD ~0.3 s on a 186,000-song library)."""
    from .mpd_client import CommandError
    info = {"version": client.protocol_version or "", "stats": client.command_dict("stats")}
    for name, key in (("urlhandlers", "handler"), ("tagtypes", "tagtype")):
        try:
            info[name] = [v for k, v in client.command(name) if k.lower() == key]
        except CommandError:  # e.g. not allowed for this password
            info[name] = []
    return info


def server_info_rows(info, address=""):
    """[(group title, [(label, value)])] for the page; empty values are left out."""
    st = info.get("stats", {})
    server = [("Address", address), ("Protocol version", info.get("version", "")),
              ("Uptime", format_span(st.get("uptime"))), ("Time playing", format_span(st.get("playtime"))),
              ("Handlers", ", ".join(h.rstrip("/").rstrip(":") for h in info.get("urlhandlers", []))),
              ("Tags", ", ".join(info.get("tagtypes", [])))]
    database = [("Artists", _count(st.get("artists"))), ("Albums", _count(st.get("albums"))),
                ("Songs", _count(st.get("songs"))), ("Duration", format_span(st.get("db_playtime"))),
                ("Last updated", _when(st.get("db_update")))]
    return [(title, [(k, v) for k, v in rows if v]) for title, rows in (("Server", server), ("Database", database))]
