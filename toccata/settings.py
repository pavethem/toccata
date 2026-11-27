"""Persistent settings (JSON in the XDG config dir) and the rules for how
command-line flags and environment variables override them.

Precedence for connection values:  --flag  >  environment  >  saved settings.
The settings dialog edits the *saved* values; `effective()` says which ones are
currently overridden so the dialog can tell the user.

The password is stored in plain text. The file is created readable only by the
user (0600), but it is not encrypted.
"""
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, replace

from .mpd_client import REPLAY_GAIN_MODES  # MPD's replay_gain_mode values

DEFAULT_PORT = 6600
MAX_SPLIT = 20000   # larger divider positions are certainly garbage
MAX_WINDOW = 20000  # ... and so are larger window sizes
DEFAULT_COLLECTION = "Default"
MAX_COLLECTION_NAME = 60
MAX_COLLECTIONS = 50
# What a collection remembers. The flat Settings fields with these names always
# hold the values of the *active* collection.
COLLECTION_FIELDS = ("host", "port", "password", "music_dir", "partition", "last_artist")


@dataclass
class Collection:
    """A named MPD server profile (a "collection")."""
    name: str
    host: str = "localhost"
    port: int = DEFAULT_PORT
    password: str = ""
    music_dir: str = ""
    partition: str = ""     # MPD partition to use ("" = default)
    last_artist: str = ""   # shown again when this collection is opened


@dataclass
class Settings:
    host: str = "localhost"          # hostname, IP, or absolute path of a unix socket
    port: int = DEFAULT_PORT
    password: str = ""               # MPD has passwords, not user names
    music_dir: str = ""              # empty = ask the server (local sockets only)
    replaygain: str = ""             # last mode the user chose; "" = never chosen
    apply_replaygain: bool = True    # re-apply `replaygain` on every connect
    last_artist: str = ""            # shown again on the next start
    main_split: int = 0              # divider library | queue, in pixels; 0 = default
    artist_split: int = 0            # divider artists | albums, in pixels; 0 = default
    main_split_max: int = 0          # the same two while the window is maximized; 0 = use the
    artist_split_max: int = 0        #   windowed value (layout.divider_position)
    window_width: int = 0            # size of the window when NOT maximized; 0 = default
    window_height: int = 0
    window_maximized: bool = False   # start maximized
    cover_dialog_width: int = 0      # size of the "Set Cover" window; 0 = default
    cover_dialog_height: int = 0
    partition: str = ""              # MPD partition of the active collection ("" = default)
    collection: str = ""             # name of the active collection ("" = not named yet)
    search_category: str = ""        # last search category
    view: str = ""                   # left pane: "library" / "playlists" / "search"
    queue_grouped: bool = False      # queue grouped by album
    queue_compact: bool = False      # queue rows on one line (more of the queue fits)
    playlist_grouped: bool = True    # playlist tracks grouped by album (Playlists view)
    albums_sort: str = "added"       # Albums view: "added" / "artist" / "title" / "year"
    ignore_prefixes: list = field(default_factory=lambda: ["The"])  # sorted as "Beatles, The"
    composer_genres: list = field(default_factory=list)  # these genres' songs go under their composer
    lastfm_api_key: str = ""         # the user's own Last.fm key (cover search, similar artists; "" = Toccata's built-in key)
    genre: str = ""                  # library genre filter ("" = All Genres)
    tray_icon: bool = True           # show a tray icon (needs a tray, e.g. GNOME's AppIndicator extension)
    close_to_tray: bool = True       # closing the window hides it to the tray (Quit from the tray menu)
    collections: list = field(default_factory=list)  # list[Collection]; see collections_of()

    def connection(self):
        """(host, port, password-or-None) as the MPD client wants it."""
        return (self.host, self.port, self.password or None)


def config_path(env=None):
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "toccata", "settings.json")


def _clean(data):
    """Build a valid Settings from untrusted JSON, falling back per field."""
    s = Settings()
    if not isinstance(data, dict):
        return s
    host = data.get("host")
    if isinstance(host, str) and host.strip():
        s.host = host.strip()
    port = data.get("port")
    if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535:
        s.port = port
    for key in ("password", "music_dir", "last_artist", "partition"):
        v = data.get(key)
        if isinstance(v, str):
            setattr(s, key, v)
    for key in ("search_category", "view", "albums_sort"):
        v = data.get(key)
        if isinstance(v, str) and len(v) <= 64:
            setattr(s, key, v)
    v = data.get("ignore_prefixes")
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        from .library import parse_prefixes
        s.ignore_prefixes = list(parse_prefixes(",".join(v)))
    v = data.get("composer_genres")
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        from .library import parse_genres
        s.composer_genres = list(parse_genres(",".join(v)))
    v = data.get("lastfm_api_key")
    if isinstance(v, str) and len(v) <= 100 and v.strip().isalnum():
        s.lastfm_api_key = v.strip()
    v = data.get("genre")
    if isinstance(v, str) and len(v) <= 1000:
        s.genre = v
    rg = data.get("replaygain")
    if rg in REPLAY_GAIN_MODES:
        s.replaygain = rg
    for key in ("main_split", "artist_split", "main_split_max", "artist_split_max"):
        v = data.get(key)
        if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= MAX_SPLIT:
            setattr(s, key, v)
    for key in ("main_split", "artist_split"):  # files from before the maximized layout had its own
        if key + "_max" not in data:            # values: their single layout is both
            setattr(s, key + "_max", getattr(s, key))
    for key in ("window_width", "window_height", "cover_dialog_width", "cover_dialog_height"):
        v = data.get(key)
        if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= MAX_WINDOW:
            setattr(s, key, v)
    for key in ("apply_replaygain", "window_maximized", "queue_grouped", "queue_compact", "tray_icon", "close_to_tray",
                "playlist_grouped"):
        v = data.get(key)
        if isinstance(v, bool):
            setattr(s, key, v)
    name = valid_collection_name(data.get("collection"))
    if name:
        s.collection = name
    raw = data.get("collections")
    if isinstance(raw, list):
        seen = set()
        for item in raw[:MAX_COLLECTIONS]:
            c = _clean_collection(item)
            if c is not None and c.name.casefold() not in seen:
                seen.add(c.name.casefold())
                s.collections.append(c)
    return s


def valid_collection_name(name):
    """The cleaned-up name, or None if it is not acceptable."""
    if not isinstance(name, str):
        return None
    name = name.strip()
    if not name or len(name) > MAX_COLLECTION_NAME or not name.isprintable():
        return None
    return name


def _clean_collection(data):
    """A valid Collection from untrusted JSON, or None. The fields are validated
    exactly like the flat settings are."""
    if not isinstance(data, dict):
        return None
    name = valid_collection_name(data.get("name"))
    if name is None:
        return None
    flat = _clean({k: v for k, v in data.items() if k in COLLECTION_FIELDS})
    return Collection(name=name, **{k: getattr(flat, k) for k in COLLECTION_FIELDS})


def load(path=None):
    """Load settings; a missing, unreadable or corrupt file gives defaults."""
    path = path or config_path()
    try:
        with open(path, encoding="utf-8") as f:
            return _clean(json.load(f))
    except (OSError, ValueError):
        return Settings()


def save(settings, path=None):
    """Write atomically with mode 0600. Raises OSError on failure."""
    path = path or config_path()
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".settings-", suffix=".tmp")  # created 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(asdict(settings), f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def parse_mpd_host(value):
    """Split MPD_HOST into (host, password-or-None).

    'secret@host' carries a password; a leading '@' means an abstract socket
    and is part of the host, not a password separator.
    """
    if "@" in value and not value.startswith("@"):
        password, host = value.split("@", 1)
        return host, password
    return value, None


def effective(saved, cli_host=None, cli_port=None, cli_password=None,
              cli_music_dir=None, env=None):
    """Apply flag/env overrides to saved settings.

    Returns (Settings, {field: source}) where source names what overrode it,
    e.g. {'host': 'MPD_HOST'}. Invalid MPD_PORT values are ignored.
    """
    env = os.environ if env is None else env
    eff, over = replace(saved), {}

    if cli_host is not None:
        eff.host, over["host"] = cli_host, "--host"
    elif env.get("MPD_HOST"):
        host, pw = parse_mpd_host(env["MPD_HOST"])
        eff.host, over["host"] = host, "MPD_HOST"
        if pw is not None:
            eff.password, over["password"] = pw, "MPD_HOST"

    if cli_password is not None:
        eff.password, over["password"] = cli_password, "--password"

    if cli_port is not None:
        eff.port, over["port"] = cli_port, "--port"
    elif env.get("MPD_PORT"):
        try:
            port = int(env["MPD_PORT"])
            if 1 <= port <= 65535:
                eff.port, over["port"] = port, "MPD_PORT"
        except ValueError:
            pass

    if cli_music_dir is not None:
        eff.music_dir, over["music_dir"] = cli_music_dir, "--music-dir"
    elif env.get("TOCCATA_MUSIC_DIR"):
        eff.music_dir, over["music_dir"] = env["TOCCATA_MUSIC_DIR"], "TOCCATA_MUSIC_DIR"

    return eff, over


# ---- collections -------------------------------------------------------------------
def snapshot(settings):
    """The active collection, built from the flat fields (which are authoritative)."""
    return Collection(name=settings.collection or DEFAULT_COLLECTION,
                      **{k: getattr(settings, k) for k in COLLECTION_FIELDS})


def collections_of(settings):
    """Every collection, as copies. The active one is always up to date, because
    it is rebuilt from the flat fields; a stale stored copy is replaced."""
    active, found, out = snapshot(settings), False, []
    for c in settings.collections:
        if c.name == active.name:
            out.append(active)
            found = True
        else:
            out.append(replace(c))
    if not found:
        out.insert(0, active)
    return out


def sync_current(settings):
    """Write the flat fields back into the collections list (before switching or listing)."""
    settings.collections = collections_of(settings)
    settings.collection = settings.collection or DEFAULT_COLLECTION


def _apply(settings, c):
    for k in COLLECTION_FIELDS:
        setattr(settings, k, getattr(c, k))
    settings.collection = c.name


def _find(settings, name):
    return next((i for i, c in enumerate(settings.collections) if c.name == name), None)


def _name_taken(settings, name, ignore=None):
    return any(c.name.casefold() == name.casefold() and c.name != ignore for c in settings.collections)


def activate(settings, name):
    """Make another collection the active one. Returns an error message or None."""
    sync_current(settings)
    i = _find(settings, name)
    if i is None:
        return f"There is no collection named \u201c{name}\u201d."
    if name != settings.collection:
        _apply(settings, settings.collections[i])
    return None


def _check(c):
    host = (c.host or "").strip()
    if not host:
        return None, "Please enter a host."
    if not (isinstance(c.port, int) and not isinstance(c.port, bool) and 1 <= c.port <= 65535):
        return None, "The port must be between 1 and 65535."
    name = valid_collection_name(c.name)
    if name is None:
        return None, f"Please enter a name (at most {MAX_COLLECTION_NAME} characters)."
    return replace(c, name=name, host=host), None


def add_collection(settings, c):
    """Add a collection. Returns an error message or None."""
    sync_current(settings)
    c, err = _check(c)
    if err:
        return err
    if len(settings.collections) >= MAX_COLLECTIONS:
        return f"At most {MAX_COLLECTIONS} collections are supported."
    if _name_taken(settings, c.name):
        return f"A collection named \u201c{c.name}\u201d already exists."
    settings.collections.append(c)
    return None


def update_collection(settings, old_name, c):
    """Replace a collection (it may be renamed). Returns an error message or None."""
    sync_current(settings)
    i = _find(settings, old_name)
    if i is None:
        return f"There is no collection named \u201c{old_name}\u201d."
    c, err = _check(c)
    if err:
        return err
    if _name_taken(settings, c.name, ignore=old_name):
        return f"A collection named \u201c{c.name}\u201d already exists."
    settings.collections[i] = c
    if old_name == settings.collection:
        _apply(settings, c)
    return None


def delete_collection(settings, name):
    """Delete a collection that is not in use. Returns an error message or None."""
    sync_current(settings)
    i = _find(settings, name)
    if i is None:
        return f"There is no collection named \u201c{name}\u201d."
    if name == settings.collection:
        return "The collection in use cannot be deleted. Switch to another one first."
    del settings.collections[i]
    return None
