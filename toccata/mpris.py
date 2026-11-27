"""MPRIS 2 (org.mpris.MediaPlayer2) on the session bus, so GNOME's media
controls, media keys, KDE Connect etc. can see and control playback.

No GTK: only Gio's D-Bus. The window gives a `player` object (see Player
below) that reports MPD's state and carries out commands; this module turns
that into MPRIS properties, methods and signals.

Spec: https://specifications.freedesktop.org/mpris-spec/latest/
Bus name: org.mpris.MediaPlayer2.toccata
"""
import hashlib

from gi.repository import Gio, GLib

BUS_NAME = "org.mpris.MediaPlayer2.toccata"
OBJECT_PATH = "/org/mpris/MediaPlayer2"
ROOT_IFACE = "org.mpris.MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
NO_TRACK = "/org/mpris/MediaPlayer2/TrackList/NoTrack"

XML = """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="CanSetFullscreen" type="b" access="read"/>
    <property name="Fullscreen" type="b" access="readwrite"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek"><arg direction="in" name="Offset" type="x"/></method>
    <method name="SetPosition">
      <arg direction="in" name="TrackId" type="o"/>
      <arg direction="in" name="Position" type="x"/>
    </method>
    <method name="OpenUri"><arg direction="in" name="Uri" type="s"/></method>
    <signal name="Seeked"><arg name="Position" type="x"/></signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="LoopStatus" type="s" access="readwrite"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Shuffle" type="b" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"""


class Player:
    """What the window must provide. Everything is called on the main loop.

    status() -> MPD `status` dict; song() -> MPD `currentsong` dict ({} if none);
    position() -> seconds into the song; art_url() -> "file://..." or "".
    Commands: play, pause, play_pause, stop, next, previous, seek_to(seconds),
    set_volume(0-100), set_repeat(bool), set_single(bool), set_random(bool),
    raise_window, quit.
    """


# -- MPD state -> MPRIS values (pure functions) ---------------------------------------

def playback_status(status):
    return {"play": "Playing", "pause": "Paused"}.get((status or {}).get("state"), "Stopped")


def loop_status(status):
    status = status or {}
    if status.get("repeat") == "1":
        return "Track" if status.get("single") == "1" else "Playlist"
    return "None"


def track_id(song):
    """A D-Bus object path per queue entry (MPD's song id), or NoTrack."""
    songid = (song or {}).get("id")
    if songid is None or not str(songid).isdigit():
        return NO_TRACK
    return f"/io/github/toccata/track/{songid}"


def _int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def metadata(song, art_url=""):
    """MPRIS Metadata (a{sv}) for MPD's currentsong; {} with only the NoTrack id
    when nothing is current."""
    song = song or {}
    md = {"mpris:trackid": GLib.Variant("o", track_id(song))}
    if not song.get("file"):
        return md
    title = song.get("title") or song.get("name") or song["file"].rsplit("/", 1)[-1]
    md["xesam:title"] = GLib.Variant("s", title)
    md["xesam:url"] = GLib.Variant("s", song["file"])
    for key, tag in (("xesam:artist", "artist"), ("xesam:albumArtist", "albumartist"),
                     ("xesam:genre", "genre"), ("xesam:composer", "composer")):
        if song.get(tag):
            md[key] = GLib.Variant("as", [song[tag]])
    if song.get("album"):
        md["xesam:album"] = GLib.Variant("s", song["album"])
    track = _int(str(song.get("track", "")).split("/")[0], 0)
    if track:
        md["xesam:trackNumber"] = GLib.Variant("i", track)
    disc = _int(str(song.get("disc", "")).split("/")[0], 0)
    if disc:
        md["xesam:discNumber"] = GLib.Variant("i", disc)
    seconds = song.get("duration") or song.get("time")
    if seconds:
        md["mpris:length"] = GLib.Variant("x", int(float(seconds) * 1_000_000))
    if art_url:
        md["mpris:artUrl"] = GLib.Variant("s", art_url)
    return md


def volume(status):
    v = _int((status or {}).get("volume"), -1)
    return max(0.0, min(1.0, v / 100.0)) if v >= 0 else 0.0


def player_properties(status, song, art_url=""):
    """All Player properties except Position (which is read on demand and,
    per the spec, never announced in PropertiesChanged)."""
    status = status or {}
    has_song = bool((song or {}).get("file"))
    length = int(float((song or {}).get("duration") or (song or {}).get("time") or 0))
    stream = "://" in (song or {}).get("file", "")
    return {
        "PlaybackStatus": GLib.Variant("s", playback_status(status)),
        "LoopStatus": GLib.Variant("s", loop_status(status)),
        "Rate": GLib.Variant("d", 1.0),
        "Shuffle": GLib.Variant("b", status.get("random") == "1"),
        "Metadata": GLib.Variant("a{sv}", metadata(song, art_url)),
        "Volume": GLib.Variant("d", volume(status)),
        "MinimumRate": GLib.Variant("d", 1.0),
        "MaximumRate": GLib.Variant("d", 1.0),
        "CanGoNext": GLib.Variant("b", True),
        "CanGoPrevious": GLib.Variant("b", True),
        "CanPlay": GLib.Variant("b", True),
        "CanPause": GLib.Variant("b", True),
        "CanSeek": GLib.Variant("b", has_song and length > 0 and not stream),
        "CanControl": GLib.Variant("b", True),
    }


ROOT_PROPERTIES = {
    "CanQuit": GLib.Variant("b", True),
    "CanRaise": GLib.Variant("b", True),
    "CanSetFullscreen": GLib.Variant("b", False),
    "Fullscreen": GLib.Variant("b", False),
    "HasTrackList": GLib.Variant("b", False),
    "Identity": GLib.Variant("s", "Toccata"),
    "DesktopEntry": GLib.Variant("s", "io.github.toccata"),
    "SupportedUriSchemes": GLib.Variant("as", []),
    "SupportedMimeTypes": GLib.Variant("as", []),
}


def changed_properties(old, new):
    """Names whose value differs (Variants compare by value)."""
    return {k: v for k, v in new.items() if k not in old or not old[k].equal(v)}


def art_file_name(key):
    """Stable file name for a cover in the cache (one per album folder)."""
    return "cover-" + hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:16] + ".png"


class MprisServer:
    """Owns the bus name and exports both interfaces. Call update() after every
    status/song change; seeked() after a jump in position."""

    def __init__(self, player, connection=None):
        self.player = player
        self._conn = connection
        self._owner_id = 0
        self._reg_ids = []
        self._props = {}
        self.name_acquired = False
        info = Gio.DBusNodeInfo.new_for_xml(XML)
        self._ifaces = {i.name: i for i in info.interfaces}

    def start(self):
        if self._conn is not None:
            self._on_bus(self._conn, BUS_NAME)
            self._owner_id = Gio.bus_own_name_on_connection(
                self._conn, BUS_NAME, Gio.BusNameOwnerFlags.DO_NOT_QUEUE,
                self._on_name_acquired, self._on_name_lost)
        else:
            self._owner_id = Gio.bus_own_name(
                Gio.BusType.SESSION, BUS_NAME, Gio.BusNameOwnerFlags.DO_NOT_QUEUE,
                self._on_bus, self._on_name_acquired, self._on_name_lost)

    def stop(self):
        if self._conn is not None:
            for rid in self._reg_ids:
                self._conn.unregister_object(rid)
        self._reg_ids = []
        if self._owner_id:
            Gio.bus_unown_name(self._owner_id)
            self._owner_id = 0

    # -- bus callbacks
    def _on_bus(self, conn, _name):
        self._conn = conn
        if self._reg_ids:
            return
        for iface in (ROOT_IFACE, PLAYER_IFACE):
            self._reg_ids.append(conn.register_object(
                OBJECT_PATH, self._ifaces[iface], self._on_call, self._on_get, self._on_set))

    def _on_name_acquired(self, _conn, _name):
        self.name_acquired = True

    def _on_name_lost(self, _conn, _name):
        self.name_acquired = False

    def _on_call(self, _conn, _sender, _path, iface, method, params, invocation):
        p = self.player
        try:
            if iface == ROOT_IFACE:
                {"Raise": p.raise_window, "Quit": p.quit}[method]()
            elif method in ("Next", "Previous", "Pause", "PlayPause", "Stop", "Play"):
                {"Next": p.next, "Previous": p.previous, "Pause": p.pause, "PlayPause": p.play_pause,
                 "Stop": p.stop, "Play": p.play}[method]()
            elif method == "Seek":
                (offset,) = params.unpack()
                length = self._length_us()
                target = p.position() * 1_000_000 + offset
                if length and target > length:
                    p.next()  # the spec: seeking past the end acts like Next
                else:
                    self._seek_us(max(0, target))
            elif method == "SetPosition":
                trackid, position = params.unpack()
                length = self._length_us()
                if trackid == track_id(p.song()) and 0 <= position <= (length or position):
                    self._seek_us(position)
            elif method == "OpenUri":
                invocation.return_dbus_error("org.mpris.MediaPlayer2.Error.NotSupported",
                                             "Opening URIs is not supported")
                return
            invocation.return_value(None)
        except Exception as e:  # never leave a D-Bus caller hanging
            invocation.return_dbus_error("org.mpris.MediaPlayer2.Error.Failed", str(e))

    def _length_us(self):
        song = self.player.song() or {}
        seconds = song.get("duration") or song.get("time")
        return int(float(seconds) * 1_000_000) if seconds else 0

    def _seek_us(self, position_us):
        self.player.seek_to(position_us / 1_000_000)
        self.seeked(position_us)

    def _on_get(self, _conn, _sender, _path, iface, prop):
        if iface == ROOT_IFACE:
            return ROOT_PROPERTIES.get(prop)
        if prop == "Position":
            return GLib.Variant("x", int(self.player.position() * 1_000_000))
        props = self._props or player_properties(self.player.status(), self.player.song(), self.player.art_url())
        return props.get(prop)

    def _on_set(self, _conn, _sender, _path, iface, prop, value):
        p = self.player
        if iface != PLAYER_IFACE:
            return True
        v = value.unpack()
        if prop == "Volume":
            p.set_volume(int(round(max(0.0, min(1.0, v)) * 100)))
        elif prop == "Shuffle":
            p.set_random(bool(v))
        elif prop == "LoopStatus":
            p.set_repeat(v != "None")
            p.set_single(v == "Track")
        return True  # Rate and Fullscreen: accepted and ignored (fixed values)

    # -- notifications
    def update(self):
        """Announce what changed since the last call (PropertiesChanged)."""
        new = player_properties(self.player.status(), self.player.song(), self.player.art_url())
        changed = changed_properties(self._props, new) if self._props else {}
        self._props = new
        if changed and self._conn is not None:
            self._conn.emit_signal(None, OBJECT_PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                                   GLib.Variant("(sa{sv}as)", (PLAYER_IFACE, changed, [])))
        return changed

    def seeked(self, position_us):
        if self._conn is not None:
            self._conn.emit_signal(None, OBJECT_PATH, PLAYER_IFACE, "Seeked",
                                   GLib.Variant("(x)", (int(position_us),)))
