"""Tray icon over D-Bus: StatusNotifierItem (org.kde.StatusNotifierItem) with a
DBusMenu (com.canonical.dbusmenu) menu. On GNOME this needs the "AppIndicator
and KStatusNotifierItem Support" extension, which provides the watcher
(org.kde.StatusNotifierWatcher). No GTK and no libappindicator (that one is
GTK 3 only): Gio's D-Bus is enough.

The window passes callbacks; menu(state) describes the items, so the menu can be
rebuilt when e.g. play/pause or the window's visibility changes.
"""
import os

from gi.repository import Gio, GLib

WATCHER_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
ITEM_IFACE = "org.kde.StatusNotifierItem"
MENU_IFACE = "com.canonical.dbusmenu"

ITEM_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
  </interface>
</node>
"""

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/>
      <arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/>
      <arg type="(ia{sv}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{sv})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" name="events" direction="in"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" name="id" direction="in"/>
      <arg type="b" name="needUpdate" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="ai" name="updatesNeeded" direction="out"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{sv})" name="updatedProps"/>
      <arg type="a(ias)" name="removedProps"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg type="u" name="revision"/>
      <arg type="i" name="parent"/>
    </signal>
    <signal name="ItemActivationRequested">
      <arg type="i" name="id"/>
      <arg type="u" name="timestamp"/>
    </signal>
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
  </interface>
</node>
"""

# Menu item ids (0 is the root)
NOW, SEP1, PLAY_PAUSE, PREVIOUS, NEXT, STOP, SEP2, SHOW, QUIT, ARTIST = range(1, 11)

MAX_LABEL = 40  # characters; DBusMenu has no width limit, the menu grows to its longest label


def shorten(text, limit=MAX_LABEL):
    """Cut to `limit` characters with an ellipsis, at a word boundary if one is near."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    space = cut.rfind(" ")
    if space >= limit * 2 // 3:  # do not throw away half the line for a nicer cut
        cut = cut[:space]
    return cut.rstrip(" ,.;:-\u2013\u2014") + "\u2026"


def label(text):
    """DBusMenu labels use "_" for access keys: show a real underscore as "__"."""
    return text.replace("_", "__")


def menu_items(state):
    """[(id, properties)] of the tray menu for `state`:
    {"title": str, "artist": str, "playing": bool, "window_visible": bool}.
    Title and artist are separate, shortened lines, so a long song name cannot
    make the menu as wide as the screen."""
    title = state.get("title") or "Nothing playing"
    now = [(NOW, {"label": label(shorten(title)), "enabled": False})]
    if state.get("artist"):
        now.append((ARTIST, {"label": label(shorten(state["artist"])), "enabled": False}))
    return now + [
        (SEP1, {"type": "separator"}),
        (PLAY_PAUSE, {"label": "Pause" if state.get("playing") else "Play",
                      "icon-name": "media-playback-pause-symbolic" if state.get("playing")
                      else "media-playback-start-symbolic"}),
        (PREVIOUS, {"label": "Previous", "icon-name": "media-skip-backward-symbolic"}),
        (NEXT, {"label": "Next", "icon-name": "media-skip-forward-symbolic"}),
        (STOP, {"label": "Stop", "icon-name": "media-playback-stop-symbolic"}),
        (SEP2, {"type": "separator"}),
        (SHOW, {"label": "Hide Window" if state.get("window_visible") else "Show Window"}),
        (QUIT, {"label": "Quit", "icon-name": "application-exit-symbolic"}),
    ]


def _props_variant(props):
    out = {}
    for k, v in props.items():
        out[k] = GLib.Variant("b", v) if isinstance(v, bool) else GLib.Variant("s", v)
    return out


def layout_variant(items, revision):
    """(u, (ia{sv}av)) for GetLayout: a root (id 0) with the items as children."""
    children = [GLib.Variant("(ia{sv}av)", (i, _props_variant(p), [])) for i, p in items]
    root = (0, {"children-display": GLib.Variant("s", "submenu")}, children)
    return GLib.Variant("(u(ia{sv}av))", (revision, root))


def tooltip_variant(title, text):
    return GLib.Variant("(sa(iiay)ss)", ("", [], title, text))


def icon_pixmaps(svg_path, sizes=(22, 44)):
    """IconPixmap fallback (ARGB32, network byte order) rendered from the SVG,
    for hosts that ignore IconThemePath."""
    try:
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
    except (ImportError, ValueError):
        return []
    out = []
    for size in sizes:
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_size(svg_path, size, size)
        except GLib.Error:
            continue
        if not pb.get_has_alpha():
            pb = pb.add_alpha(False, 0, 0, 0)
        w, h, stride, data = pb.get_width(), pb.get_height(), pb.get_rowstride(), pb.get_pixels()
        argb = bytearray()
        for y in range(h):
            row = data[y * stride:y * stride + w * 4]
            for x in range(0, w * 4, 4):
                r, g, b, a = row[x:x + 4]
                argb += bytes((a, r, g, b))
        out.append((w, h, bytes(argb)))
    return out


class Tray:
    """callbacks: dict with toggle_window, play_pause, previous, next, stop,
    quit, volume_step(delta: +1/-1), state() -> see menu_items()."""

    def __init__(self, callbacks, icon_dir, icon_name="io.github.toccata-panel", connection=None):
        self.cb = callbacks
        self.icon_dir = icon_dir
        self.icon_name = icon_name
        self._conn = connection
        self._revision = 1
        self._items = menu_items(callbacks["state"]())
        self._reg_ids = []
        self._owner_id = 0
        self._watch_id = 0
        self.registered = False
        self.service = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self._tooltip = ("Toccata", "")
        svg = os.path.join(icon_dir, icon_name + ".svg")
        self._pixmaps = icon_pixmaps(svg) if os.path.exists(svg) else []
        self._item_info = Gio.DBusNodeInfo.new_for_xml(ITEM_XML).interfaces[0]
        self._menu_info = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]

    # -- lifecycle
    def start(self):
        if self._conn is None:
            self._conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._reg_ids = [
            self._conn.register_object(ITEM_PATH, self._item_info, self._on_item_call, self._on_item_get, None),
            self._conn.register_object(MENU_PATH, self._menu_info, self._on_menu_call, self._on_menu_get, None),
        ]
        self._owner_id = Gio.bus_own_name_on_connection(self._conn, self.service, Gio.BusNameOwnerFlags.NONE,
                                                        lambda *_: self._register(), None)
        # (re-)register whenever a watcher appears, e.g. after GNOME Shell restarts
        self._watch_id = Gio.bus_watch_name_on_connection(self._conn, WATCHER_NAME, Gio.BusNameWatcherFlags.NONE,
                                                          lambda *_: self._register(), self._watcher_gone)

    def stop(self):
        if self._watch_id:
            Gio.bus_unwatch_name(self._watch_id)
            self._watch_id = 0
        for rid in self._reg_ids:
            self._conn.unregister_object(rid)
        self._reg_ids = []
        if self._owner_id:
            Gio.bus_unown_name(self._owner_id)
            self._owner_id = 0
        self.registered = False

    def _register(self):
        if not self._owner_id:
            return

        def done(conn, res):
            try:
                conn.call_finish(res)
                self.registered = True
            except GLib.Error:
                self.registered = False  # no tray host (e.g. extension disabled)
        self._conn.call(WATCHER_NAME, WATCHER_PATH, WATCHER_NAME, "RegisterStatusNotifierItem",
                        GLib.Variant("(s)", (self.service,)), None, Gio.DBusCallFlags.NONE, 2000, None, done)

    def _watcher_gone(self, *_):
        self.registered = False

    # -- StatusNotifierItem
    def _on_item_get(self, _c, _s, _p, _iface, prop):
        values = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", "toccata"),
            "Title": GLib.Variant("s", "Toccata"),
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", self.icon_name),
            "IconThemePath": GLib.Variant("s", self.icon_dir),
            "IconPixmap": GLib.Variant("a(iiay)", self._pixmaps),
            "OverlayIconName": GLib.Variant("s", ""),
            "AttentionIconName": GLib.Variant("s", ""),
            "ToolTip": tooltip_variant(*self._tooltip),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
        }
        return values.get(prop)

    def _on_item_call(self, _c, _s, _p, _iface, method, params, invocation):
        try:
            if method == "Activate":
                self.cb["toggle_window"]()
            elif method == "SecondaryActivate":  # middle click
                self.cb["play_pause"]()
            elif method == "Scroll":
                delta, orientation = params.unpack()
                if orientation.lower() == "vertical" and delta:
                    self.cb["volume_step"](1 if delta < 0 else -1)  # wheel up = louder
            invocation.return_value(None)
        except Exception as e:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.Failed", str(e))

    # -- DBusMenu
    def _on_menu_get(self, _c, _s, _p, _iface, prop):
        return {"Version": GLib.Variant("u", 3), "TextDirection": GLib.Variant("s", "ltr"),
                "Status": GLib.Variant("s", "normal"), "IconThemePath": GLib.Variant("as", [self.icon_dir])}.get(prop)

    def _on_menu_call(self, _c, _s, _p, _iface, method, params, invocation):
        try:
            if method == "GetLayout":
                invocation.return_value(layout_variant(self._items, self._revision))
            elif method == "GetGroupProperties":
                ids, _names = params.unpack()
                found = [(i, _props_variant(p)) for i, p in self._items if not ids or i in ids]
                invocation.return_value(GLib.Variant("(a(ia{sv}))", (found,)))
            elif method == "GetProperty":
                item_id, name = params.unpack()
                props = dict(self._items).get(item_id, {})
                if name not in props:
                    invocation.return_dbus_error("org.freedesktop.DBus.Error.InvalidArgs", "no such property")
                    return
                invocation.return_value(GLib.Variant("(v)", (_props_variant({name: props[name]})[name],)))
            elif method == "Event":
                item_id, event, _data, _ts = params.unpack()
                if event == "clicked":
                    self._clicked(item_id)
                invocation.return_value(None)
            elif method == "EventGroup":
                (events,) = params.unpack()
                for item_id, event, _data, _ts in events:
                    if event == "clicked":
                        self._clicked(item_id)
                invocation.return_value(GLib.Variant("(ai)", ([],)))
            elif method == "AboutToShow":
                self.refresh()  # the window may have been shown/hidden meanwhile
                invocation.return_value(GLib.Variant("(b)", (False,)))
            elif method == "AboutToShowGroup":
                self.refresh()
                invocation.return_value(GLib.Variant("(aiai)", ([], [])))
            else:
                invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)
        except Exception as e:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.Failed", str(e))

    def _clicked(self, item_id):
        action = {PLAY_PAUSE: "play_pause", PREVIOUS: "previous", NEXT: "next", STOP: "stop",
                  SHOW: "toggle_window", QUIT: "quit"}.get(item_id)
        if action:
            # run after the D-Bus reply, so a Quit does not cut the reply off
            GLib.idle_add(lambda: (self.cb[action](), False)[1])

    # -- updates from the window
    def refresh(self):
        """Rebuild menu labels from the current state; announce if they changed."""
        items = menu_items(self.cb["state"]())
        if items != self._items:
            self._items = items
            self._revision += 1
            if self._conn is not None and self._reg_ids:
                self._conn.emit_signal(None, MENU_PATH, MENU_IFACE, "LayoutUpdated",
                                       GLib.Variant("(ui)", (self._revision, 0)))

    def set_tooltip(self, title, text):
        if (title, text) != self._tooltip:
            self._tooltip = (title, text)
            if self._conn is not None and self._reg_ids:
                self._conn.emit_signal(None, ITEM_PATH, ITEM_IFACE, "NewToolTip", None)
