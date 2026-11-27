"""GTK4 / libadwaita front-end. Needs: python-gobject, gtk4, libadwaita >= 1.5."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import os
import sys
import threading
import time
from dataclasses import replace

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, GObject, Graphene, Gtk, Pango  # noqa: E402

from . import tags as taglib  # noqa: E402
from .infoview import InfoView  # noqa: E402
from .connection import ChangeBatcher, IdleWatcher, call_with_reconnect  # noqa: E402
from .covers import (CoverFetcher, CoverWriteError, album_folders, cover_extension, cover_fit_rect,  # noqa: E402
                     existing_covers, preview_size, write_cover)
from .dnd import drag_label, insert_index, line_y, plan_moves, tracks_label  # noqa: E402
from .layout import UserDragFilter, WindowMemory, divider_key, divider_position  # noqa: E402
from .library import (UNKNOWN, LibraryFetcher, fetch_db_stamp, fetch_genres, int_prefix, parse_genres, parse_prefixes,  # noqa: E402
                      sort_names)
from . import playlists as pllib  # noqa: E402
from . import search as searchlib  # noqa: E402
from .playback import (DbReloadGate, NowPlayingMarker, changed_range, db_update_job, db_update_text, entry_to_play,  # noqa: E402
                       now_playing, play_options, reuse_entries)
from . import queuegroups  # noqa: E402
from . import queuefilter  # noqa: E402
from .queueundo import QueueHistory, restore_commands  # noqa: E402
from . import albums as albumslib  # noqa: E402
from .typeahead import Matcher, TypeAhead  # noqa: E402
from . import selection  # noqa: E402
from . import coversearch  # noqa: E402
from . import lastfm  # noqa: E402
from . import mpris as mprislib  # noqa: E402
from .tray import Tray  # noqa: E402
from .mpd_client import (REPLAY_GAIN_MODES, CommandError, MPDClient, MPDError,  # noqa: E402
                         describe_error, probe)
from .serverview import (collection_subtitle, fetch_server_info, partition_rows, server_info_rows,  # noqa: E402
                         split_outputs, window_subtitle)
from .settings import (Collection, activate, add_collection, collections_of, config_path,  # noqa: E402
                       delete_collection, effective, snapshot, update_collection)
from .settings import load as load_settings, save as save_settings  # noqa: E402

APP_ID = "io.github.toccata"
ICON_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "icons")


def fmt_time(seconds):
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    return f"{s // 60}:{s % 60:02d}"


class Song(GObject.Object):
    """Wraps an MPD song dict so it can live in a Gio.ListStore."""
    __gtype_name__ = "ToccataSong"
    # "" normally; "play" or "pause" for the queue entry that is in use. Rows follow it.
    playing = ""  # plain attribute: "play" / "pause" / "" (read when a row is bound)
    row = None  # the SongRow currently showing this song, if any
    # Queue grouping (plain attributes, see queuegroups.py):
    group = None    # queuegroups.Group this entry belongs to (grouped queue only)
    hidden = False  # inside a collapsed album: filtered out of the list
    summary = False # first entry of a collapsed album: its row stands for the whole album
    texts = None    # (title, subtitle, duration) shown instead of the defaults

    def __init__(self, data):
        super().__init__()
        self.data = data

    @property
    def title(self):
        return self.data.get("title") or os.path.basename(self.data.get("file", ""))

    @property
    def subtitle(self):
        parts = [self.data.get("artist", ""), self.data.get("album", "")]
        return " — ".join(p for p in parts if p)

    @property
    def uri(self):
        return self.data.get("file", "")

    @property
    def duration(self):
        return fmt_time(self.data.get("duration") or self.data.get("time"))


def _set_text_compact(box, title, sub, on):
    """Two lines (title over a small subtitle), or compact: one line, the subtitle
    dimmed after the title, at the same font size so both share a baseline. The
    row's height then comes from the .queue-compact CSS alone, the same for track
    and album header rows (see QueueAlbum)."""
    box.set_orientation(Gtk.Orientation.HORIZONTAL if on else Gtk.Orientation.VERTICAL)
    box.set_spacing(10 if on else 0)
    (sub.remove_css_class if on else sub.add_css_class)("caption")
    title.set_hexpand(False)
    sub.set_hexpand(on)  # compact: the subtitle takes the rest of the line


_drag_text = [""]  # the text of the drag being started (Window._tag); PyGObject cannot read it back


class DragSource(Gtk.DragSource):
    """Every drag of the app: shows its text (dnd.drag_label) as a small label
    beside the pointer. GTK's default icon for a text sits at the pointer
    (hotspot -2, -2), so the cursor covered the text (user report 2026-11-13).
    GTK only fills the icon after "drag-begin" if it has no child yet."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.connect("drag-begin", self._show_text)

    @staticmethod
    def _show_text(_src, drag):
        text = _drag_text[0]
        if not text:
            return  # GTK's default icon
        label = Gtk.Label(label=text, ellipsize=Pango.EllipsizeMode.END, max_width_chars=48)
        label.add_css_class("drag-label")
        Gtk.DragIcon.get_for_drag(drag).set_child(label)
        # The label's top left corner goes right of and a little below the pointer's
        # tip, clear of the arrow (which is about as wide as 2/3 of the cursor size).
        size = Gtk.Settings.get_default().get_property("gtk-cursor-theme-size") or 24
        drag.set_hotspot(-round(size * 0.8), -round(size * 0.4))


class SongRow(Gtk.Box):
    """One row of a song list. A Python subclass, so its attributes persist
    between the factory's setup/bind/unbind calls."""

    def __init__(self, extra_buttons, on_drag=None, show_edit=True, cover=None):
        super().__init__(spacing=12)
        self.add_css_class("song-row")  # padding lives in CSS so the highlight fills the whole line
        # cover: optional callable(song, thumb) that fills a cover thumbnail at the row's start
        # (Search view, like Cantata); the request is dropped if the row is rebound meanwhile.
        self._cover = cover
        self.thumb = None
        if cover is not None:
            self.thumb = CoverThumb(QUEUE_COVER_SIZE)
            self.append(self.thumb)
        # Always takes its space (just invisible), so the text does not jump when a song starts.
        self.indicator = Gtk.Image(icon_name="media-playback-start-symbolic", pixel_size=16,
                                   valign=Gtk.Align.CENTER, opacity=0.0)
        self.indicator.add_css_class("accent")
        self.append(self.indicator)
        # search results are never the playing queue entry: no gap for its icon after the cover
        self.indicator.set_visible(cover is None)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        self._text = text
        self.title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.sub = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.sub.add_css_class("dim-label")
        self.sub.add_css_class("caption")
        text.append(self.title)
        text.append(self.sub)
        self.dur = Gtk.Label()
        self.dur.add_css_class("dim-label")
        self.dur.add_css_class("numeric")
        self.append(text)
        self.append(self.dur)
        self.extra = []
        for icon, tip, cb in extra_buttons:
            b = Gtk.Button(icon_name=icon, tooltip_text=tip, valign=Gtk.Align.CENTER)
            b.add_css_class("flat")
            self.append(b)
            self.extra.append((b, cb))
        self.edit = None
        if show_edit:
            self.edit = Gtk.Button(icon_name="document-edit-symbolic", tooltip_text="Edit tags",
                                   valign=Gtk.Align.CENTER)
            self.edit.add_css_class("flat")
            self.append(self.edit)
        self._handlers = []
        self._song = None
        if on_drag is not None:
            src = DragSource(actions=Gdk.DragAction.MOVE | Gdk.DragAction.COPY)
            src.connect("prepare", lambda _s, _x, _y: on_drag(self._song))
            self.add_controller(src)

    def set_compact(self, on):
        """Compact queue: title and subtitle on one line (see _set_text_compact)."""
        _set_text_compact(self._text, self.title, self.sub, on)

    def _show_state(self, state):
        """Highlight this row while its song is playing or paused."""
        active = state in ("play", "pause")
        self.indicator.set_opacity(1.0 if active else 0.0)
        if active:
            self.indicator.set_from_icon_name("media-playback-pause-symbolic" if state == "pause"
                                              else "media-playback-start-symbolic")
        # only CSS classes change here, never anything that alters the row's size
        for widget, css in ((self.title, "accent"), (self, "now-playing")):
            (widget.add_css_class if active else widget.remove_css_class)(css)

    def bind_song(self, song, on_edit):
        self._song = song
        song.row = self  # so a status change can restyle this visible row directly
        self._show_state(song.playing)
        self.refresh_texts()
        if self.thumb is not None:
            self.thumb.set_texture(None)
            self._cover(song, self)
        for b, cb in self.extra:
            self._handlers.append((b, b.connect("clicked", lambda _b, s=song, cb=cb: cb(s))))
        if self.edit is not None:
            self._handlers.append((self.edit, self.edit.connect("clicked", lambda _b, s=song: on_edit(s))))

    def refresh_texts(self):
        song = self._song
        if song is None:
            return
        title, sub, dur = song.texts or (song.title, song.subtitle, song.duration)
        self.title.set_text(title)
        self.sub.set_text(sub)
        self.dur.set_text(dur)

    def unbind_song(self):
        for widget, hid in self._handlers:
            widget.disconnect(hid)
        self._handlers.clear()
        if self._song is not None and self._song.row is self:
            self._song.row = None
        self._song = None
        self._show_state("")


def make_song_list(store, on_activate, on_edit, extra_buttons=(), multi=False, on_drag=None,
                   show_edit=True, on_menu=None, cover=None, header_owner=None):
    """A ListView of Songs. `extra_buttons` are (icon, tooltip, callback(song));
    show_edit=False drops the per-row "Edit tags" button; on_menu(song, row, x, y)
    is called on a right-click; cover(song, row) fills a cover thumbnail per row.
    header_owner: the list may also hold QueueAlbum items, shown as album header
    rows (QueueHeader(header_owner, as_row=True)) exactly as tall as the track rows,
    like the queue (see QueueAlbum: GTK section headers make the list jump)."""
    factory = Gtk.SignalListItemFactory()

    def make_row():
        row = SongRow(extra_buttons, on_drag, show_edit, cover)
        if on_menu is not None:
            _right_click(row, lambda x, y, r=row: r._song is not None and on_menu(r._song, r, x, y))
        return row

    if header_owner is None:
        factory.connect("setup", lambda _f, item: item.set_child(make_row()))
        factory.connect("bind", lambda _f, item: item.get_child().bind_song(item.get_item(), on_edit))
        factory.connect("unbind", lambda _f, item: item.get_child().unbind_song())
    else:
        def setup(_f, item):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            box.song_row = make_row()
            box.song_row.add_css_class("queue-row")  # same height as the header rows
            box.header = QueueHeader(header_owner, as_row=True)
            box.append(box.song_row)
            box.append(box.header)
            item.set_child(box)

        def bind(_f, item):
            box, obj = item.get_child(), item.get_item()
            album = isinstance(obj, QueueAlbum)
            item.set_selectable(not album)
            item.set_activatable(not album)
            box.song_row.set_visible(not album)
            box.header.set_visible(album)
            if album:
                obj.widget = box.header
                box.header.bind(obj.group)
            else:
                box.song_row.bind_song(obj, on_edit)

        def unbind(_f, item):
            box, obj = item.get_child(), item.get_item()
            if isinstance(obj, QueueAlbum):
                if obj.widget is box.header:
                    obj.widget = None
                box.header.unbind()
            else:
                box.song_row.unbind_song()
        factory.connect("setup", setup)
        factory.connect("bind", bind)
        factory.connect("unbind", unbind)
    # MultiSelection: click selects, Shift+click selects a range, Ctrl+click toggles
    sel = (Gtk.MultiSelection if multi else Gtk.SingleSelection)(model=store)
    view = Gtk.ListView(model=sel, factory=factory, single_click_activate=False)
    view.connect("activate", lambda _v, pos: isinstance(sel.get_item(pos), Song) and on_activate(sel.get_item(pos)))
    return view


class QueueAlbum(GObject.Object):
    """An album header as an item of the queue list, right before the album's tracks.

    The headers used to be GTK section headers. GTK's ListView counts a section
    header that is not laid out as 0 px high, so the list's estimated height changed
    with every scroll position (only nearby headers counted), and dragging the
    scrollbar of a grouped queue made it jump back and forth (user report
    2026-11-11). As items of the list, of the same height as track rows, the
    estimate is exact. Kept per album (by its first entry's id) across refreshes, so
    GTK keeps its scroll anchor; `group` is updated on every regrouping."""
    __gtype_name__ = "ToccataQueueAlbum"
    widget = None  # the QueueHeader showing it, if any

    def __init__(self, group):
        super().__init__()
        self.group = group


def make_queue_list(model, window, extra_buttons):
    """The queue's ListView: rows are Songs (SongRow) or QueueAlbums (QueueHeader).
    Header rows are neither selectable nor activatable; their own gestures select,
    drag and open the menu for the album."""
    factory = Gtk.SignalListItemFactory()

    def setup(_f, item):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.song_row = SongRow(extra_buttons, window._drag_queue, show_edit=False)
        box.song_row.add_css_class("queue-row")
        _right_click(box.song_row, lambda x, y, r=box.song_row: r._song is not None and window._queue_menu(r._song, r, x, y))
        box.header = QueueHeader(window, as_row=True)
        box.song_row.set_compact(window.queue_compact)
        box.header.set_compact(window.queue_compact)
        box.append(box.song_row)
        box.append(box.header)
        item.set_child(box)

    def bind(_f, item):
        box, obj = item.get_child(), item.get_item()
        album = isinstance(obj, QueueAlbum)
        item.set_selectable(not album)
        item.set_activatable(not album)
        box.song_row.set_visible(not album)
        box.header.set_visible(album)
        if album:
            obj.widget = box.header
            box.header.bind(obj.group)
        else:
            box.song_row.bind_song(obj, window.edit_queue_song)

    def unbind(_f, item):
        box, obj = item.get_child(), item.get_item()
        if isinstance(obj, QueueAlbum):
            if obj.widget is box.header:
                obj.widget = None
            box.header.unbind()
        else:
            box.song_row.unbind_song()
    factory.connect("setup", setup)
    factory.connect("bind", bind)
    factory.connect("unbind", unbind)
    sel = Gtk.MultiSelection(model=model)
    view = Gtk.ListView(model=sel, factory=factory, single_click_activate=False)
    view.connect("activate", lambda _v, pos: isinstance(sel.get_item(pos), Song) and window.play_queue_song(sel.get_item(pos)))
    return view


class QueueHeader(Gtk.Box):
    """Album header of a grouped list: cover, title, "N Tracks (time)", collapse
    button. Clicking it selects the album's tracks; dragging it moves the whole album.
    In the queue it is a row of the list (as_row: exactly as tall as a track row, see
    QueueAlbum); in the Playlists view a Gtk.ListHeader child."""

    def __init__(self, window, as_row=False):
        if as_row:  # padding and height from the .song-row / .queue-row CSS, like a track row
            super().__init__(spacing=12)
            self.add_css_class("song-row")
            self.add_css_class("queue-row")
        else:
            super().__init__(spacing=12, margin_top=10, margin_bottom=2, margin_start=12, margin_end=12)
        self.add_css_class("queue-header")
        self.set_focusable(True)  # see _on_click
        self._window = window
        self.group = None
        self.thumb = CoverThumb(QUEUE_COVER_SIZE)
        self.append(self.thumb)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        self._text = text
        self._compact = False
        self.title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.title.add_css_class("heading")
        self.sub = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.sub.add_css_class("dim-label")
        self.sub.add_css_class("caption")
        text.append(self.title)
        text.append(self.sub)
        self.append(text)
        self.playing = Gtk.Image(icon_name="media-playback-start-symbolic", pixel_size=16,
                                 valign=Gtk.Align.CENTER, opacity=0.0)
        self.playing.add_css_class("accent")
        self.append(self.playing)
        # Play / Add to Queue, shown only while the pointer is over the header (CSS
        # .queue-header:hover, like the Artists view's album headers; user request
        # 2026-11-19). Only where the owner offers them (the Playlists view): in the
        # queue the album is already queued. Opacity, not visibility: the title keeps its width.
        self.hover_buttons = []  # [Play, Add], left to right
        if hasattr(window, "play_group"):
            for icon, tip, act in (("media-playback-start-symbolic", "Play", window.play_group),
                                   ("list-add-symbolic", "Add to queue", window.add_group)):
                # focus_on_click off: focusing it on press made the ListView scroll the
                # row into view (26 px in the smoke test), the release then missed the
                # button and the first click did nothing (gtk_smoke_plhover.py)
                b = Gtk.Button(icon_name=icon, tooltip_text=tip, valign=Gtk.Align.CENTER,
                               focus_on_click=False)
                b.add_css_class("flat")
                b.add_css_class("album-hover-btn")
                b.connect("clicked", lambda _b, act=act: self.group is not None and act(self.group))
                self.append(b)
                self.hover_buttons.append(b)
        self.toggle = Gtk.Button(valign=Gtk.Align.CENTER, tooltip_text="Collapse or expand this album")
        self.toggle.add_css_class("flat")
        self.toggle.connect("clicked", lambda _b: self.group and window.toggle_group(self.group))
        self.append(self.toggle)
        click = Gtk.GestureClick()
        click.connect("released", self._on_click)
        self.add_controller(click)
        src = DragSource(actions=Gdk.DragAction.MOVE)
        src.connect("prepare", lambda _s, _x, _y: window.drag_group(self.group))
        self.add_controller(src)
        _right_click(self, lambda x, y: self.group is not None and window.header_menu(self.group, self, x, y))

    def set_compact(self, on):
        """Compact queue: one line and a small cover. The cover must not be taller
        than a compact row's content (CSS min-height), or the header would be taller
        than a track row (QueueAlbum)."""
        self._compact = on
        _set_text_compact(self._text, self.title, self.sub, on)
        self.thumb.set_size(COMPACT_COVER_SIZE if on else QUEUE_COVER_SIZE)
        # the card style's 12 px corners would make a 20 px cover a circle
        (self.thumb.add_css_class if on else self.thumb.remove_css_class)("compact-cover")

    def _on_click(self, gesture, _n, x, y):
        if self.group is None:
            return
        picked = self.pick(x, y, Gtk.PickFlags.DEFAULT)
        while picked is not None and picked is not self:
            if picked is self.toggle or picked in self.hover_buttons:
                return  # the button handles its own click
            picked = picked.get_parent()
        mods = gesture.get_current_event_state()
        self._window.select_group(self.group, extend=bool(mods & Gdk.ModifierType.CONTROL_MASK))
        # Take the keyboard focus, so keys reach the list's controllers (Delete in the
        # queue); headers are not focusable by default and the focus stayed elsewhere.
        self.grab_focus()

    def bind(self, group):
        self.group = group
        group.header = self
        self.title.set_text(group.title)
        self.refresh()
        self.thumb.set_texture(None)
        uri = self._window.queue_song_at(group.start)
        if self._window.show_covers and uri and "://" not in uri:
            self._window.cover_loader.request(
                uri, lambda tex, g=group: self.thumb.set_texture(tex) if self.group is g else None,
                wanted=lambda g=group: self.group is g,
                song=self._window.queue_tags_at(group.start), size=THUMB_SIZE)
        self.thumb.set_visible(self._window.show_covers)

    def refresh(self):
        g = self.group
        if g is None:
            return
        closed = g.head_id in self._window.collapsed_groups
        self.sub.set_text(g.summary())
        self.toggle.set_icon_name("pan-end-symbolic" if closed else "pan-down-symbolic")
        state = getattr(g, "playing", "")
        self.playing.set_opacity(1.0 if state else 0.0)
        if state:
            self.playing.set_from_icon_name("media-playback-pause-symbolic" if state == "pause"
                                            else "media-playback-start-symbolic")
        (self.title.add_css_class if state else self.title.remove_css_class)("accent")

    def unbind(self):
        if self.group is not None and getattr(self.group, "header", None) is self:
            self.group.header = None
        self.group = None


def _decode_texture(data):
    """Runs on the cover worker thread (Gdk.Texture is immutable and thread-safe)."""
    return Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))


THUMB_SIZE = 128  # px, shorter side of list covers (sharp at 56 px on a 2x screen)


def _thumbnail_png(data, size):
    """Scale an image so its shorter side is `size` (never enlarged) and return it
    as PNG bytes. Runs on the cover worker thread, so the UI never decodes big
    covers: a 1000 px JPEG becomes a ~30 KB PNG, decoded once and cached on disk."""
    loader = GdkPixbuf.PixbufLoader()

    def prepared(ld, w, h):
        shorter = min(w, h)
        if shorter > size:
            ld.set_size(max(1, round(w * size / shorter)), max(1, round(h * size / shorter)))
    loader.connect("size-prepared", prepared)
    try:
        loader.write(data)
        loader.close()
    except GLib.Error:
        return None
    pixbuf = loader.get_pixbuf()
    if pixbuf is None:
        return None
    ok, png = pixbuf.save_to_bufferv("png", [], [])
    return bytes(png) if ok else None


def _on_main_thread(fn, *args):
    GLib.idle_add(lambda: (fn(*args), False)[1])


COVER_SIZE = 72


class CoverArt(Gtk.Widget):
    """Draws a texture "cover-fitted" into a square of *fixed* size.

    Gtk.Picture reports the image's own pixel size as its preferred size, so a
    1000px cover made the player bar huge and pushed the lists out of view.
    This widget always measures as COVER_SIZE x COVER_SIZE and ignores the
    image size; the picture is scaled and clipped to fit.
    """
    __gtype_name__ = "ToccataCoverArt"

    def __init__(self, size=COVER_SIZE):
        super().__init__()
        self._size = size
        self._texture = None
        self.set_halign(Gtk.Align.START)
        self.set_valign(Gtk.Align.CENTER)
        self.set_hexpand(False)
        self.set_vexpand(False)

    def set_texture(self, texture):
        self._texture = texture
        self.queue_draw()

    def do_measure(self, orientation, for_size):
        return (self._size, self._size, -1, -1)  # min, natural, baselines

    def do_snapshot(self, snapshot):
        tex = self._texture
        w, h = self.get_width(), self.get_height()
        if tex is None or w <= 0 or h <= 0:
            return
        x, y, dw, dh = cover_fit_rect(w, h, tex.get_width(), tex.get_height())
        snapshot.push_clip(Graphene.Rect().init(0, 0, w, h))
        snapshot.append_texture(tex, Graphene.Rect().init(x, y, dw, dh))
        snapshot.pop()


ALBUM_COVER_SIZE = 56
QUEUE_COVER_SIZE = 40
COMPACT_COVER_SIZE = 20  # = the .queue-compact .queue-row min-height


class PreviewArt(Gtk.Widget):
    """The whole cover at an exact size (no cropping), for the hover preview.
    Fixed-size like CoverArt, so a big image cannot make its popover huge."""
    __gtype_name__ = "ToccataPreviewArt"

    def __init__(self):
        super().__init__()
        self._texture, self._w, self._h = None, 1, 1

    def show(self, texture, w, h):
        self._texture, self._w, self._h = texture, max(1, w), max(1, h)
        self.queue_resize()
        self.queue_draw()

    def do_measure(self, orientation, for_size):
        n = self._w if orientation == Gtk.Orientation.HORIZONTAL else self._h
        return (n, n, -1, -1)

    def do_snapshot(self, snapshot):
        if self._texture is not None:
            snapshot.append_texture(self._texture,
                                    Graphene.Rect().init(0, 0, self.get_width(), self.get_height()))


class CoverThumb(Gtk.Stack):
    """A cover of fixed size: shows a placeholder until a texture is set."""
    __gtype_name__ = "ToccataCoverThumb"

    def __init__(self, size):
        super().__init__(valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER,
                         overflow=Gtk.Overflow.HIDDEN)
        self.set_size_request(size, size)
        self.add_css_class("card")
        placeholder = Gtk.Image(icon_name="audio-x-generic-symbolic", pixel_size=max(16, size // 2))
        placeholder.add_css_class("dim-label")
        self._placeholder = placeholder
        self._art = CoverArt(size)
        self.add_named(placeholder, "none")
        self.add_named(self._art, "pic")
        self.set_visible_child_name("none")

    def set_size(self, size):
        """Change the fixed size (the compact queue's album headers)."""
        self.set_size_request(size, size)
        self._placeholder.set_pixel_size(max(12, min(16, size // 2)) if size < 32 else max(16, size // 2))
        self._art._size = size
        self._art.queue_resize()

    def set_texture(self, texture):
        if texture is None:
            self.set_visible_child_name("none")
        else:
            self._art.set_texture(texture)
            self.set_visible_child_name("pic")


class TagEditor(Adw.Dialog):
    """Edits tags for one or more songs. Only changed fields are written.
    Several songs: "All tracks" or each track on its own (as Cantata), switched with
    the track list or the arrows (Alt+Left / Alt+Right); see tags.MultiEdit."""

    def __init__(self, window, songs, paths, merged):
        super().__init__(title="Edit tags" if len(paths) == 1 else f"Edit tags ({len(paths)} files)",
                         content_width=480)
        self.win, self.songs, self.paths, self.merged = window, songs, paths, merged
        self.rows = {}
        try:  # each file's own tags, for the track pages
            per_file = [taglib.read_tags(p) for p in paths] if len(paths) > 1 else []
        except taglib.TagError:
            per_file = []
        self.edit = taglib.MultiEdit(per_file) if per_file else None
        self.page = None  # None: all tracks
        self._switching = False

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _b: self.close())
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", self.on_save)
        header.pack_start(cancel)
        header.pack_end(save)
        toolbar.add_top_bar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                          margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        if self.edit is not None:
            nav = Gtk.Box(spacing=6)
            self.prev_btn = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Previous track (Alt+\u2190)")
            self.next_btn = Gtk.Button(icon_name="go-next-symbolic", tooltip_text="Next track (Alt+\u2192)")
            self.pages = Gtk.StringList.new(["All tracks"] + [self.edit.label(i) for i in range(len(self.edit))])
            self.picker = Gtk.DropDown(model=self.pages, hexpand=True, enable_search=len(self.edit) > 8,
                                       expression=Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))
            self.picker.connect("notify::selected", lambda *_: self._on_pick())
            self.prev_btn.connect("clicked", lambda _b: self.step(-1))
            self.next_btn.connect("clicked", lambda _b: self.step(1))
            for w in (self.prev_btn, self.picker, self.next_btn):
                nav.append(w)
            content.append(nav)
            self.page_hint = Gtk.Label(xalign=0, wrap=True, ellipsize=Pango.EllipsizeMode.MIDDLE, lines=2)
            self.page_hint.add_css_class("dim-label")
            self.page_hint.add_css_class("caption")
            content.append(self.page_hint)
            keys = Gtk.ShortcutController()  # bubble: an open preview dialog takes Escape first
            for key, d in (("<Alt>Left", -1), ("<Alt>Right", 1)):
                keys.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string(key),
                                                   Gtk.CallbackAction.new(lambda *_a, d=d: (self.step(d), True)[1])))
            self.add_controller(keys)

        group = Adw.PreferencesGroup()
        for key, label in taglib.FIELDS:
            row = Adw.EntryRow(title=label)
            group.add(row)
            self.rows[key] = row
        content.append(group)
        # ReplayGain: read-only (scanners like rsgain write it), shown on request
        self._rg_cache = {}
        self.rg_popover = Gtk.Popover()
        self.rg_popover.connect("show", lambda _p: self._fill_replaygain())
        self.rg_button = Gtk.MenuButton(label="ReplayGain", popover=self.rg_popover, halign=Gtk.Align.START,
                                        margin_top=6, tooltip_text="Show the ReplayGain values (read-only)")
        content.append(self.rg_button)
        scroller = Gtk.ScrolledWindow(propagate_natural_height=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(content)
        toolbar.set_content(scroller)
        self.set_child(toolbar)
        self.set_default_widget(save)
        self._show_page()

    # -- pages (several files)
    def _view(self):
        return self.edit.view(self.page) if self.edit is not None else self.merged

    def _show_page(self):
        labels = dict(taglib.FIELDS)
        for key, (value, mixed) in self._view().items():
            row = self.rows[key]
            row.set_title(labels[key] + (" (multiple values)" if mixed else ""))
            row.set_text(value)
        if self.edit is None:
            return
        n = len(self.edit)
        if self.page is None:
            self.page_hint.set_text(f"Changes here apply to all {n} tracks. Fields marked \u201cmultiple "
                                    "values\u201d keep each track\u2019s own value unless you type one.")
        else:
            self.page_hint.set_text(f"Track {self.page + 1} of {n} only: {os.path.basename(self.paths[self.page])}")
        self.prev_btn.set_sensitive(self.page is not None)
        self.next_btn.set_sensitive(self.page is None or self.page < n - 1)

    def _commit(self):
        if self.edit is not None:
            self.edit.commit(self.page, {k: r.get_text() for k, r in self.rows.items()})

    def _on_pick(self):
        if self._switching:
            return
        self._commit()
        sel = self.picker.get_selected()
        self.page = None if sel in (0, Gtk.INVALID_LIST_POSITION) else sel - 1
        self._refresh_labels()
        self._show_page()

    def _refresh_labels(self):
        """The track list shows which tracks have unsaved edits (\u2022)."""
        self._switching = True
        try:
            sel = self.picker.get_selected()
            for i in range(len(self.edit)):
                label = self.edit.label(i)
                if self.pages.get_string(i + 1) != label:
                    self.pages.splice(i + 1, 1, [label])
            self.picker.set_selected(sel)
        finally:
            self._switching = False

    def step(self, d):
        if self.edit is None:
            return
        pos = 0 if self.page is None else self.page + 1  # position in the list
        pos = max(0, min(len(self.edit), pos + d))
        self.picker.set_selected(pos)  # -> _on_pick

    # -- ReplayGain
    def _replaygain(self, i):
        path = self.paths[i]
        if path not in self._rg_cache:
            try:
                self._rg_cache[path] = taglib.read_replaygain(path)
            except taglib.TagError as e:
                self._rg_cache[path] = str(e)
        return self._rg_cache[path]

    def _track_name(self, i):
        if self.edit is not None:
            return self.edit.label(i).rstrip(" \u2022")
        return os.path.basename(self.paths[i])

    def _fill_replaygain(self):
        """Fill the popover for what the page shows: one track, or all of them."""
        if self.edit is not None and self.page is None:
            files = list(range(len(self.paths)))
        else:
            files = [self.page if self.edit is not None else 0]
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=6, margin_bottom=6,
                      margin_start=6, margin_end=6)
        grid = Gtk.Grid(column_spacing=18, row_spacing=4)

        def cell(text, col, row, dim=False, right=False):
            label = Gtk.Label(label=text, xalign=1 if right else 0, selectable=not dim,
                              ellipsize=Pango.EllipsizeMode.END if col == 0 else Pango.EllipsizeMode.NONE,
                              max_width_chars=30 if col == 0 else -1)
            if dim:
                label.add_css_class("dim-label")
            if right:
                label.add_css_class("numeric")
            grid.attach(label, col, row, 1, 1)
        names = ("Track gain", "Track peak", "Album gain", "Album peak")
        values = [self._replaygain(i) for i in files]
        found = any(isinstance(v, dict) and any(x is not None for x in v.values()) for v in values)
        if len(files) == 1:
            v = values[0]
            if isinstance(v, str):
                cell(v, 0, 0)
            else:
                for r, (name, f) in enumerate(zip(names, taglib.RG_FIELDS)):
                    cell(name, 0, r, dim=True)
                    fmt = taglib.format_peak if f.endswith("peak") else taglib.format_gain
                    cell(fmt(v[f]), 1, r, right=True)
        else:
            for c, name in enumerate(("Track",) + names):
                cell(name, c, 0, dim=True, right=c > 0)
            for r, (i, v) in enumerate(zip(files, values), 1):
                cell(self._track_name(i), 0, r)
                if isinstance(v, str):
                    cell("cannot read", 1, r, dim=True)
                    continue
                for c, f in enumerate(taglib.RG_FIELDS, 1):
                    fmt = taglib.format_peak if f.endswith("peak") else taglib.format_gain
                    cell(fmt(v[f]).split(" (")[0], c, r, right=True)  # table: peak without dBFS
        if not found and not all(isinstance(v, str) for v in values):
            note = "No ReplayGain tags. A scanner such as rsgain can add them."
        else:
            note = "Read-only. MPD applies it when Replay gain is on (Preferences)."
        hint = Gtk.Label(label=note, xalign=0, wrap=True, max_width_chars=40)
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        scroll = Gtk.ScrolledWindow(child=grid, propagate_natural_height=True, propagate_natural_width=True,
                                    max_content_height=320, hscrollbar_policy=Gtk.PolicyType.NEVER)
        box.append(scroll)
        box.append(hint)
        self.rg_popover.set_child(box)

    def on_save(self, _btn):
        errors, changed = [], False
        if self.edit is not None:
            self._commit()
            for i, changes in self.edit.changes():
                changed = True
                try:
                    taglib.write_tags(self.paths[i], changes)
                except taglib.TagError as e:
                    errors.append(str(e))
        else:
            changes = {}
            for key, row in self.rows.items():
                value, mixed = self.merged[key]
                new = row.get_text().strip()
                # Mixed + untouched (still empty) => keep each file's own value.
                if new != value and not (mixed and new == ""):
                    changes[key] = new
            changed = bool(changes)
            for path in self.paths:
                try:
                    taglib.write_tags(path, changes)
                except taglib.TagError as e:
                    errors.append(str(e))
        self.win.after_tag_write(self.songs, changed, errors)
        self.close()


def _loading_box(text):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                  valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
    box.append(Gtk.Spinner(spinning=True, width_request=32, height_request=32))
    label = Gtk.Label(label=text)
    label.add_css_class("dim-label")
    box.append(label)
    return box


class SettingsDialog(Adw.PreferencesDialog):
    """Connection, library, playback and interface settings. Changes are
    applied when the dialog closes."""

    RG_LABELS = ["None", "Track", "Album", "Auto"]  # same order as REPLAY_GAIN_MODES

    def __init__(self, window):
        super().__init__(title="Preferences")
        self.win = window
        s, ov = window.settings, window.overrides
        self._initial_rg = None

        # -- connection
        conn = Adw.PreferencesGroup(title="Connection")
        notes = ["MPD has no user names, only an optional password.",
                 "The password is stored unencrypted in " + config_path() + " (readable only by you)."]
        labels = {"host": "host", "port": "port", "password": "password", "music_dir": "music folder"}
        for field, label in labels.items():
            if field in ov:
                notes.append(f"The {label} is currently set by {ov[field]} and overrides what you enter here.")
        conn.set_description(GLib.markup_escape_text("\n".join(notes)))

        self.host_row = Adw.EntryRow(title="Host or socket path")
        self.host_row.set_text(s.host)
        self.port_row = Adw.SpinRow.new_with_range(1, 65535, 1)
        self.port_row.set_title("Port")
        self.port_row.set_numeric(True)
        self.port_row.set_value(s.port)
        self.pw_row = Adw.PasswordEntryRow(title="Password")
        self.pw_row.set_text(s.password)
        self.test_row = Adw.ActionRow(title="Test connection",
                                      subtitle="Checks the values above without saving them")
        self.test_btn = Gtk.Button(label="Test", valign=Gtk.Align.CENTER)
        self.test_btn.connect("clicked", self._on_test)
        self.test_row.add_suffix(self.test_btn)
        for row in (self.host_row, self.port_row, self.pw_row, self.test_row):
            conn.add(row)

        # -- library
        lib = Adw.PreferencesGroup(
            title="Library",
            description="Where MPD's music is on this computer. Needed for tag editing when you "
                        "connect over the network. Leave empty to ask the server (local sockets only).")
        self.music_row = Adw.EntryRow(title="Music folder")
        self.music_row.set_text(s.music_dir)
        pick = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER,
                          tooltip_text="Choose folder")
        pick.add_css_class("flat")
        pick.connect("clicked", self._on_pick_folder)
        self.music_row.add_suffix(pick)
        lib.add(self.music_row)

        # -- playback
        play = Adw.PreferencesGroup(title="Playback")
        self.rg_row = Adw.ComboRow(title="Replay gain", model=Gtk.StringList.new(self.RG_LABELS))
        self.rg_apply = Adw.SwitchRow(
            title="Apply on every connect",
            subtitle="MPD forgets the replay gain mode when it restarts. This sets it again each time we connect.")
        self.rg_apply.set_active(s.apply_replaygain)
        server_rg = None
        if window.client.connected:
            try:
                server_rg = window.client.replay_gain_status()
            except (OSError, MPDError):
                server_rg = None
        if not window.client.connected:
            self.rg_row.set_sensitive(False)
            self.rg_row.set_subtitle("Not connected")
        elif server_rg is None:
            self.rg_row.set_sensitive(False)
            self.rg_apply.set_sensitive(False)
            self.rg_row.set_subtitle("Not supported by this server")
        else:
            idx = REPLAY_GAIN_MODES.index(server_rg) if server_rg in REPLAY_GAIN_MODES else 0
            self.rg_row.set_selected(idx)
            self._initial_rg = idx  # what the server uses right now
        play.add(self.rg_row)
        play.add(self.rg_apply)

        # -- interface
        ui = Adw.PreferencesGroup(title="Interface")
        self.tray_row = Adw.SwitchRow(title="Show tray icon",
                                      subtitle="Needs a tray; on GNOME the AppIndicator extension")
        self.tray_row.set_active(s.tray_icon)
        self.close_tray_row = Adw.SwitchRow(title="Close to tray",
                                            subtitle="Closing the window keeps the app in the tray. "
                                                     "Quit from the tray menu or with Ctrl+Q.")
        self.close_tray_row.set_active(s.close_to_tray)
        self.tray_row.bind_property("active", self.close_tray_row, "sensitive", GObject.BindingFlags.SYNC_CREATE)
        ui.add(self.tray_row)
        ui.add(self.close_tray_row)
        self.prefixes_row = Adw.EntryRow(title="Ignore prefixes when sorting (comma separated)")
        self.prefixes_row.set_text(", ".join(s.ignore_prefixes))
        self.prefixes_row.set_tooltip_text('Artists and albums starting with these words sort without them: '
                                           '"The Beatles" under B')
        ui.add(self.prefixes_row)
        self.composer_row = Adw.EntryRow(title="Use the composer for these genres (comma separated)")
        self.composer_row.set_text(", ".join(s.composer_genres))
        self.composer_row.set_tooltip_text("Songs of these genres that have a composer are listed under the "
                                           "composer instead of the album artist, e.g. Classical, Opera")
        ui.add(self.composer_row)

        # -- covers
        covers = Adw.PreferencesGroup(
            title="Cover Search",
            description="\u201cSet Cover\u201d searches Cover Art Archive, Deezer, iTunes and Last.fm. "
                        "Only the search text is sent. Last.fm uses Toccata\u2019s built-in API key, which "
                        "all users share; if it hits Last.fm\u2019s rate limit you can use your own free key "
                        "instead (<a href=\"https://www.last.fm/api/account/create\">create one at last.fm</a>).")
        self.lastfm_row = Adw.PasswordEntryRow(title="Own Last.fm API key (optional)")
        self.lastfm_row.set_text(s.lastfm_api_key)
        self.lastfm_row.set_tooltip_text("Empty: Toccata\u2019s built-in key. Also used for "
                                         "\u201cSimilar artists\u201d in the Info view")
        covers.add(self.lastfm_row)

        page = Adw.PreferencesPage()
        for group in (conn, lib, play, ui, covers):
            page.add(group)
        self.add(page)
        self.connect("closed", self._on_closed)

    # -- actions
    def _form_connection(self):
        return (self.host_row.get_text().strip() or "localhost",
                int(self.port_row.get_value()),
                self.pw_row.get_text() or None)

    def _on_test(self, btn):
        host, port, password = self._form_connection()
        btn.set_sensitive(False)
        self.test_row.set_subtitle("Testing\u2026")

        def work():
            try:
                info = probe(host, port, password)
                msg = f"Connected: MPD {info['version']}"
                if info["music_directory"]:
                    msg += f"\nServer reports music folder: {info['music_directory']}"
            except (OSError, MPDError) as e:
                msg = "Failed: " + describe_error(e, host, port)
            GLib.idle_add(done, msg)

        def done(msg):
            self.test_row.set_subtitle(GLib.markup_escape_text(msg))
            btn.set_sensitive(True)
            return False

        threading.Thread(target=work, daemon=True).start()

    def _on_pick_folder(self, _btn):
        dialog = Gtk.FileDialog(title="Select music folder")
        current = os.path.expanduser(self.music_row.get_text().strip())
        if current and os.path.isdir(current):
            dialog.set_initial_folder(Gio.File.new_for_path(current))
        dialog.select_folder(self.win, None, self._on_folder_chosen)

    def _on_folder_chosen(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return  # cancelled
        if folder is not None and folder.get_path():
            self.music_row.set_text(folder.get_path())

    # -- applying
    def _rg_changed_by_user(self):
        return (self._initial_rg is not None and self.rg_row.get_sensitive()
                and self.rg_row.get_selected() != self._initial_rg)

    def _on_closed(self, _dialog):
        win, old = self.win, self.win.settings
        host, port, password = self._form_connection()
        # replace() keeps every field this dialog does not edit (last artist, dividers, ...)
        new = replace(old, host=host, port=port, password=password or "",
                      music_dir=self.music_row.get_text().strip(),
                      apply_replaygain=self.rg_apply.get_active(),
                      tray_icon=self.tray_row.get_active(),
                      close_to_tray=self.close_tray_row.get_active(),
                      ignore_prefixes=list(parse_prefixes(self.prefixes_row.get_text())),
                      composer_genres=list(parse_genres(self.composer_row.get_text())),
                      lastfm_api_key=lastfm.valid_key(self.lastfm_row.get_text()))
        if self._rg_changed_by_user():
            mode = REPLAY_GAIN_MODES[self.rg_row.get_selected()]
            new.replaygain = mode
            win.safe(win.client.set_replay_gain_mode, mode)  # takes effect immediately
        if new.music_dir and not os.path.isdir(os.path.expanduser(new.music_dir)):
            win.show_toast("Music folder does not exist: " + new.music_dir)
        if new != old:
            try:
                save_settings(new)
            except OSError as e:
                win.show_toast(f"Could not save settings: {e}")
            win.settings = new
            if new.composer_genres != old.composer_genres:
                win.composer_genres_changed()
            elif new.ignore_prefixes != old.ignore_prefixes:
                win.prefixes_changed()
            if new.tray_icon:
                win.start_tray()
            else:
                win.stop_tray()
            win.apply_settings()
        elif not win.client.connected:
            win.reconnect()  # opening and closing the dialog retries a failed connection


def _text_button(label, callback):
    b = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
    b.connect("clicked", lambda _b: callback())
    return b


def _icon_button(icon, tooltip, callback):
    b = Gtk.Button(icon_name=icon, tooltip_text=tooltip, valign=Gtk.Align.CENTER)
    b.add_css_class("flat")
    b.connect("clicked", lambda _b: callback())
    return b


def _confirm(parent, heading, body, action_label, on_confirm):
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("confirm", action_label)
    dialog.set_response_appearance("confirm", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")
    dialog.connect("response", lambda _d, response: on_confirm() if response == "confirm" else None)
    dialog.present(parent)


class CollectionEditor(Adw.Dialog):
    """Add or edit a collection (a named server profile)."""

    def __init__(self, existing, on_done):
        super().__init__(title="Edit collection" if existing else "New collection", content_width=420)
        self.existing, self.on_done = existing, on_done
        base = existing or Collection(name="")

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _b: self.close())
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", self._on_save)
        header.pack_start(cancel)
        header.pack_end(save)
        toolbar.add_top_bar(header)

        group = Adw.PreferencesGroup(margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        self.name_row = Adw.EntryRow(title="Name")
        self.name_row.set_text(base.name)
        self.host_row = Adw.EntryRow(title="Host or socket path")
        self.host_row.set_text(base.host)
        self.port_row = Adw.SpinRow.new_with_range(1, 65535, 1)
        self.port_row.set_title("Port")
        self.port_row.set_numeric(True)
        self.port_row.set_value(base.port)
        self.pw_row = Adw.PasswordEntryRow(title="Password")
        self.pw_row.set_text(base.password)
        self.music_row = Adw.EntryRow(title="Music folder")
        self.music_row.set_text(base.music_dir)
        for row in (self.name_row, self.host_row, self.port_row, self.pw_row, self.music_row):
            group.add(row)
        self.error_label = Gtk.Label(wrap=True, xalign=0, visible=False,
                                     margin_start=12, margin_end=12, margin_bottom=12)
        self.error_label.add_css_class("error")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(group)
        box.append(self.error_label)
        toolbar.set_content(Gtk.ScrolledWindow(propagate_natural_height=True,
                                               hscrollbar_policy=Gtk.PolicyType.NEVER, child=box))
        self.set_child(toolbar)
        self.set_default_widget(save)

    def _on_save(self, _btn):
        c = replace(self.existing or Collection(name=""),
                    name=self.name_row.get_text(), host=self.host_row.get_text(),
                    port=int(self.port_row.get_value()), password=self.pw_row.get_text(),
                    music_dir=self.music_row.get_text().strip())
        err = self.on_done(self.existing.name if self.existing else None, c)
        if err:
            self.error_label.set_text(err)
            self.error_label.set_visible(True)
        else:
            self.close()


class ServerDialog(Adw.PreferencesDialog):
    """Outputs, partitions and collections. Everything is read fresh from the
    server (output ids change when outputs move) and rebuilt after each action."""

    def __init__(self, window, page="outputs"):
        super().__init__(title="MPD server")
        self.win = window
        self._rows = {}          # page key -> [(group, row)] to remove on refresh
        self._switches = {}      # output name -> (row, handler id), to update in place
        self._outputs_sig = None  # what the outputs page currently shows

        # -- outputs
        self.outputs_here = Adw.PreferencesGroup(title="Outputs")
        self.outputs_elsewhere = Adw.PreferencesGroup(
            title="Outputs in other partitions", description="Move an output here to use it in this partition.")
        outputs = Adw.PreferencesPage(name="outputs", title="Outputs", icon_name="audio-speakers-symbolic")
        outputs.add(self.outputs_here)
        outputs.add(self.outputs_elsewhere)

        # -- partitions
        self.partition_group = Adw.PreferencesGroup(
            title="Partitions",
            description="Each partition has its own queue, playback state and outputs. "
                        "This window works in one of them at a time.")
        self.new_partition_group = Adw.PreferencesGroup(title="New partition")
        self.new_partition_row = Adw.EntryRow(title="Name of the new partition")
        self.new_partition_row.set_show_apply_button(True)
        self.new_partition_row.connect("apply", self._on_new_partition)
        self.new_partition_group.add(self.new_partition_row)
        partitions = Adw.PreferencesPage(name="partitions", title="Partitions", icon_name="view-dual-symbolic")
        partitions.add(self.partition_group)
        partitions.add(self.new_partition_group)

        # -- collections
        self.collection_group = Adw.PreferencesGroup(title="Collections")
        self.collection_group.set_header_suffix(_text_button("Add\u2026", lambda: self._edit_collection(None)))
        collections = Adw.PreferencesPage(name="collections", title="Collections", icon_name="network-server-symbolic")
        collections.add(self.collection_group)

        # -- server information (Cantata's "Server Information")
        self.info_groups = []
        self.info_page = Adw.PreferencesPage(name="info", title="Information",
                                             icon_name="dialog-information-symbolic")
        self._info_gen = 0

        for p in (outputs, partitions, collections, self.info_page):
            self.add(p)
        self.set_visible_page_name(page)
        self.refresh()

    # -- shared plumbing
    def refresh(self):
        self._refresh_outputs()
        self._refresh_partitions()
        self._refresh_collections()
        self._refresh_info()

    def _add_row(self, key, group, row):
        group.add(row)
        self._rows.setdefault(key, []).append((group, row))

    def _remove_rows(self, key):
        for group, row in self._rows.pop(key, []):
            group.remove(row)

    # -- server information
    def _refresh_info(self):
        """stats is slow on big libraries: read in the background, shown when done."""
        self._info_gen += 1
        gen = self._info_gen
        if not self.info_groups:
            self._show_info([("Server", [("Reading\u2026", "")])])

        def done(info, err):
            if gen != self._info_gen:
                return
            if err is not None:
                self._show_info([("Server", [("Could not read the server information",
                                              describe_error(err, *self.win.cfg[:2]))])])
                return
            host, port = self.win.cfg[:2]
            self._show_info(server_info_rows(info, f"{host}:{port}"))
        self.win.album_fetcher.run(fetch_server_info, done)

    def _show_info(self, sections):
        for g in self.info_groups:
            self.info_page.remove(g)
        self.info_groups = []
        for title, rows in sections:
            group = Adw.PreferencesGroup(title=title)
            for label, value in rows:
                if len(value) > 40:  # handler and tag lists: wrapped under the label
                    row = Adw.ActionRow(title=label, subtitle=value)
                    row.set_subtitle_selectable(True)
                else:
                    row = Adw.ActionRow(title=label)
                    if value:
                        row.add_suffix(Gtk.Label(label=value, selectable=True, css_classes=["dim-label"]))
                group.add(row)
            self.info_page.add(group)
            self.info_groups.append(group)

    # -- outputs
    def _reload_outputs(self):
        self._outputs_sig = None
        self._refresh_outputs()
        return False

    def _refresh_outputs(self):
        outputs = self.win.safe(self.win.client.all_outputs)
        current = self.win.safe(self.win.client.current_partition) or "default"
        if outputs is None:
            self._outputs_sig = None
            self._remove_rows("outputs")
            self._switches.clear()
            self._add_row("outputs", self.outputs_here, Adw.ActionRow(
                title="Could not read the outputs", subtitle="Is the server reachable?"))
            self.outputs_elsewhere.set_visible(False)
            return
        here, elsewhere = split_outputs(outputs, current)
        signature = (current, tuple((o.name, o.partition) for o in outputs))
        if signature == self._outputs_sig:  # same outputs: just update the switches in place
            for o in here:
                row, handler = self._switches[o.name]
                if row.get_active() != o.enabled:
                    row.handler_block(handler)
                    row.set_active(o.enabled)
                    row.handler_unblock(handler)
            return
        self._outputs_sig = signature
        self._remove_rows("outputs")
        self._switches.clear()
        self.outputs_here.set_description(
            GLib.markup_escape_text(f"Partition: {current}") if current != "default" or elsewhere else "")
        if not here:
            self._add_row("outputs", self.outputs_here, Adw.ActionRow(title="No outputs in this partition"))
        for o in here:
            row = Adw.SwitchRow(title=GLib.markup_escape_text(o.name), subtitle=GLib.markup_escape_text(o.plugin))
            row.set_active(o.enabled)  # before connecting, so this does not send a command
            handler = row.connect("notify::active", lambda r, _p, name=o.name: self._on_output_toggled(name, r))
            self._switches[o.name] = (row, handler)
            self._add_row("outputs", self.outputs_here, row)
        self.outputs_elsewhere.set_visible(bool(elsewhere))
        for o in elsewhere:
            row = Adw.ActionRow(title=GLib.markup_escape_text(o.name),
                                subtitle=GLib.markup_escape_text(f"In partition \u201c{o.partition}\u201d \u00b7 {o.plugin}"))
            row.add_suffix(_text_button("Move here", lambda n=o.name: self._move_output(n)))
            self._add_row("outputs", self.outputs_elsewhere, row)

    def _on_output_toggled(self, name, row):
        enabled = row.get_active()
        # by name, not by id: ids are renumbered whenever outputs move between partitions
        if not self.win.safe(lambda: self.win.client.set_output_enabled_by_name(name, enabled) or True):
            GLib.idle_add(self._reload_outputs)  # show the real state again

    def _move_output(self, name):
        if self.win.safe(lambda: self.win.client.move_output(name) or True):
            self._reload_outputs()

    # -- partitions
    def _refresh_partitions(self):
        self._remove_rows("partitions")
        names = self.win.safe(self.win.client.listpartitions)
        if names is None:
            self.new_partition_group.set_visible(False)
            self._add_row("partitions", self.partition_group, Adw.ActionRow(
                title="Partitions are not available", subtitle="They need MPD 0.22 or newer."))
            return
        self.new_partition_group.set_visible(True)
        current = self.win.safe(self.win.client.current_partition) or "default"
        for r in partition_rows(names, current):
            row = Adw.ActionRow(title=GLib.markup_escape_text(r.name),
                                subtitle="In use by this window" if r.current else "")
            if r.current:
                row.add_suffix(Gtk.Image(icon_name="object-select-symbolic"))
            if r.can_switch:
                row.add_suffix(_text_button("Switch", lambda n=r.name: self._switch_partition(n)))
            if r.can_delete:
                row.add_suffix(_icon_button("user-trash-symbolic", "Delete partition",
                                            lambda n=r.name: self._confirm_delete_partition(n)))
            self._add_row("partitions", self.partition_group, row)

    def _switch_partition(self, name):
        if self.win.switch_partition(name):
            self._outputs_sig = None
            self.refresh()

    def _confirm_delete_partition(self, name):
        _confirm(self, f"Delete partition \u201c{name}\u201d?",
                 "Its queue is lost, and its outputs move to the default partition.", "Delete",
                 lambda: self._delete_partition(name))

    def _delete_partition(self, name):
        if self.win.safe(lambda: self.win.client.delete_partition(name) or True):
            self._outputs_sig = None
            self.refresh()

    def _on_new_partition(self, row):
        name = row.get_text().strip()
        if name and self.win.safe(lambda: self.win.client.new_partition(name) or True):
            row.set_text("")
            self._refresh_partitions()

    # -- collections
    def _refresh_collections(self):
        self._remove_rows("collections")
        s, win = self.win.settings, self.win
        notes = ["A collection is a saved MPD server. Switching reconnects and also brings back "
                 "that server's last artist and partition."]
        pinned = {f: win.overrides[f] for f in ("host", "port", "password", "music_dir") if f in win.overrides}
        if pinned:
            field, source = next(iter(pinned.items()))
            notes.append(f"The {field.replace('_', ' ')} is currently set by {source}, which overrides collections.")
        self.collection_group.set_description(GLib.markup_escape_text(" ".join(notes)))
        all_collections = collections_of(s)
        active = snapshot(s).name
        for c in all_collections:
            row = Adw.ActionRow(title=GLib.markup_escape_text(c.name),
                                subtitle=GLib.markup_escape_text(collection_subtitle(c)))
            if c.name == active:
                row.add_suffix(Gtk.Image(icon_name="object-select-symbolic"))
            else:
                row.add_suffix(_text_button("Use", lambda n=c.name: self._use_collection(n)))
            row.add_suffix(_icon_button("document-edit-symbolic", "Edit collection",
                                        lambda c=c: self._edit_collection(c)))
            if c.name != active and len(all_collections) > 1:
                row.add_suffix(_icon_button("user-trash-symbolic", "Delete collection",
                                            lambda n=c.name: self._confirm_delete_collection(n)))
            self._add_row("collections", self.collection_group, row)

    def _use_collection(self, name):
        self.win.switch_collection(name)
        self._outputs_sig = None
        self.refresh()

    def _edit_collection(self, collection):
        CollectionEditor(collection, self._save_collection).present(self)

    def _save_collection(self, old_name, collection):
        s = self.win.settings
        err = add_collection(s, collection) if old_name is None else update_collection(s, old_name, collection)
        if err:
            return err
        self.win.collections_changed()
        self._refresh_collections()
        return None

    def _confirm_delete_collection(self, name):
        _confirm(self, f"Delete collection \u201c{name}\u201d?",
                 "Only the saved settings are removed. The server itself is not touched.", "Delete",
                 lambda: self._delete_collection(name))

    def _delete_collection(self, name):
        err = delete_collection(self.win.settings, name)
        if err:
            self.win.show_toast(err)
            return
        self.win.collections_changed()
        self._refresh_collections()


def _pin_list_top(adj, enabled):
    """Keep a list with album headers at the very top while the user has it there.

    GTK (4.24) anchors a list on its first *row*: after the rows change it
    re-lays out with row 0 at the top, so the first album header ends up just
    above the visible area (scroll_to(0) does the same). That re-layout
    arrives as the adjustment's "changed" signal; a scroll by the user is a
    "value-changed" without it. So: pinned while at 0, released by the
    user's own scrolling, and every re-layout that moves a pinned list down
    is undone (at most 20 times per 2 s, so this can never fight GTK forever;
    the undo runs right after GTK's layout pass, see fix()).
    When the app itself replaces the rows (_rows_changing), a move in the next
    second also counts as GTK's: with unchanged contents GTK moves the list
    without a "changed" signal. `enabled()` says whether the pin is active."""
    state = {"pinned": True, "relayout": False, "fixes": 0, "since": 0.0, "grace": 0.0, "pending": False,
             "user": 0.0}  # until when moves are the user's own (_watch_user_scroll)

    def users():
        return time.monotonic() < state["user"]

    def fix(a):
        """Undo GTK's move, but not right here: these signals come from inside GTK's
        layout pass, and setting the value there left rows laid out at the old
        place (the top album's first track missing until the user scrolled, user
        report 2026-11-11). So it runs in an idle right after the layout; one frame
        may show the list moved (a small hiccup)."""
        if state["pending"]:
            return
        now = time.monotonic()
        if now - state["since"] > 2:
            state["since"], state["fixes"] = now, 0
        if state["fixes"] < 20:
            state["fixes"] += 1
            state["pending"] = True
            # Not from the frame clock's layout phase: re-requesting layout there froze
            # the whole app on the user's machine (2026-11-11, see DECISIONS).
            GLib.idle_add(apply_fix, a, priority=GLib.PRIORITY_HIGH_IDLE)

    def apply_fix(a):
        state["pending"] = False
        if enabled() and state["pinned"] and a.get_value() > 0 and not users():
            if os.environ.get("TOCCATA_DEBUG"):
                print(f"queue pin: GTK moved the list to {a.get_value():.0f} (upper {a.get_upper():.0f}), "
                      f"back to 0", file=sys.stderr)
            state["relayout"] = True  # the value-changed this causes is ours
            a.set_value(0)
        return False

    def on_changed(a):
        if enabled() and state["pinned"] and a.get_value() > 0:
            fix(a)

    def on_value(a):
        if state["relayout"]:
            state["relayout"] = False
        elif users():
            # The user scrolls: never undone, also within the grace second after a change
            # (it was, so the queue could not be scrolled for a second after a drop; user
            # report 2026-11-11).
            state["grace"] = 0.0
            state["pinned"] = a.get_value() <= 0
        elif state["pending"]:
            pass  # GTK's own move; the fix is on its way
        elif enabled() and state["pinned"] and a.get_value() > 0 and time.monotonic() < state["grace"]:
            fix(a)  # GTK re-anchoring after our own row change
        else:
            state["pinned"] = a.get_value() <= 0
    adj.connect("changed", on_changed)
    adj.connect("value-changed", on_value)
    return state


USER_SCROLL_HOLD = 0.5  # s after the last scroll input in which moves still count as the user's


def _pin_user_input(pin, hold=USER_SCROLL_HOLD):
    """The user is scrolling (or about to): moves of the list in the next `hold`
    seconds are theirs, so the top pin leaves them alone. hold=None: until further notice."""
    if pin is None:
        return
    if hold is None:
        pin["user"] = float("inf")
    elif pin["user"] != float("inf"):  # a held scrollbar stays held until released
        pin["user"] = max(pin["user"], time.monotonic() + hold)


def _watch_user_scroll(scroller, pin_getter):
    """Report the user's scrolling of `scroller` to its top pin: wheel / touchpad
    (incl. kinetic glide), scrollbar presses and drags, and scroll keys."""
    scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.BOTH_AXES
                                       | Gtk.EventControllerScrollFlags.KINETIC)
    scroll.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    scroll.connect("scroll-begin", lambda _c: _pin_user_input(pin_getter()))
    scroll.connect("scroll", lambda _c, _dx, _dy: _pin_user_input(pin_getter()) or False)
    scroll.connect("decelerate", lambda _c, _vx, _vy: _pin_user_input(pin_getter(), 3.0))
    scroller.add_controller(scroll)
    bar = scroller.get_vscrollbar()
    if bar is not None:
        press = Gtk.GestureClick()
        press.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        pin_hold = lambda *_a: _pin_user_input(pin_getter(), None)  # held while the button is down
        pin_end = lambda *_a: _pin_user_input_end(pin_getter())
        press.connect("pressed", pin_hold)
        press.connect("released", pin_end)
        press.connect("stopped", pin_end)
        press.connect("cancel", pin_end)
        bar.add_controller(press)
    keys = Gtk.EventControllerKey()
    keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    scroll_keys = {Gdk.KEY_Up, Gdk.KEY_Down, Gdk.KEY_Page_Up, Gdk.KEY_Page_Down, Gdk.KEY_Home, Gdk.KEY_End,
                   Gdk.KEY_KP_Up, Gdk.KEY_KP_Down, Gdk.KEY_KP_Page_Up, Gdk.KEY_KP_Page_Down,
                   Gdk.KEY_KP_Home, Gdk.KEY_KP_End}
    keys.connect("key-pressed", lambda _c, kv, _code, _m: (kv in scroll_keys and _pin_user_input(pin_getter())) and False)
    scroller.add_controller(keys)


def _pin_user_input_end(pin):
    if pin is not None:
        pin["user"] = time.monotonic() + USER_SCROLL_HOLD


def _rows_changing(pin):
    if pin is not None:
        pin["grace"] = time.monotonic() + 1.0


def _apply_groups(songs, collapsed, ids=None):
    """Album groups for a list of Song objects (queue or playlist): sets the plain
    attributes group / hidden / summary / texts and returns the groups.
    ids: per-song identities for the collapse state (default: MPD's song id)."""
    data = [s.data for s in songs] if ids is None else [dict(s.data, id=i) for s, i in zip(songs, ids)]
    groups = queuegroups.group_runs(data)
    rows = queuegroups.layout(groups, collapsed)
    for g in groups:
        g.header = None
        g.playing = ""
        for pos in range(g.start, g.end):
            song = songs[pos]
            song.group = g
            song.hidden, song.summary = rows[pos]
            if song.playing:
                g.playing = song.playing
            if song.summary:
                song.texts = (g.summary(), g.artist, "")
            else:
                song.texts = (queuegroups.track_title(song.data),
                              song.data.get("artist", "") or g.artist, song.duration)
    return groups


def _ungroup(songs):
    for song in songs:
        song.group, song.hidden, song.summary, song.texts = None, False, False, None


def _context_menu(widget, x, y, entries):
    """A right-click menu at (x, y) of `widget`. entries: (label, callback) or None
    for a separator."""
    menu, section, group = Gio.Menu(), Gio.Menu(), Gio.SimpleActionGroup()
    for i, entry in enumerate(entries):
        if entry is None:
            menu.append_section(None, section)
            section = Gio.Menu()
            continue
        label, callback = entry
        action = Gio.SimpleAction.new(f"a{i}", None)
        action.connect("activate", lambda *_a, cb=callback: cb())
        group.add_action(action)
        section.append(label, f"ctx.a{i}")
    menu.append_section(None, section)
    popover = Gtk.PopoverMenu.new_from_model(menu)
    popover.insert_action_group("ctx", group)
    popover.set_has_arrow(False)
    popover.set_parent(widget)
    rect = Gdk.Rectangle()
    rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
    popover.set_pointing_to(rect)
    # unparent only after the clicked action ran (it needs the popover's action group)
    popover.connect("closed", lambda p: GLib.idle_add(lambda: (p.unparent(), False)[1]))
    popover.popup()
    # A parent has to size its popovers (Gtk.Popover.present() in its size_allocate);
    # list rows do, but e.g. the Gtk.Label of an artist row does not, and the menu came
    # out 13 px too short. So size it ourselves once it is on screen.
    for delay in (0, 50, 200):
        GLib.timeout_add(delay, lambda: (popover.get_visible() and popover.present(), False)[1])
    return popover


def _select_like_click(view, start, count=1, extend=False):
    """Select items start .. start+count-1 of a ListView/GridView the way a click does,
    so a later Shift+click range starts here. Selection-model calls alone leave GTK's
    range start at the last real click, and Shift+click then dropped everything
    between (user report 2026-11-11). extend: add to the selection (Ctrl)."""
    # "list.select-item" (pos, modify, extend) is what GTK's own click runs; it moves
    # the range start to `start`. With modify it toggles, so select_range puts it back.
    view.activate_action("list.select-item", GLib.Variant("(ubb)", (start, extend, False)))
    if count > 1 or extend:
        view.get_model().select_range(start, count, False)


class RubberBand:
    """Click-and-drag selection for a list in a ScrolledWindow: a drag that starts on
    empty space (is_empty(picked widget) says so) draws a band on `overlay` (whose
    child is the scroller) and calls on_band(anchor_y, current_y, ctrl) with
    content coordinates (viewport y + scroll offset) on every change. on_start(ctrl,
    x, y) runs once the drag is a band (it remembers what the selection was).
    Near the top or bottom edge the list scrolls. A press on a row is left alone,
    so dragging rows (drag-and-drop) keeps working. on_click(ctrl, shift), if given,
    runs for a press and release on empty space that did not become a band."""

    def __init__(self, scroller, overlay, is_empty, on_start, on_band, pin_getter=lambda: None,
                 on_click=None):
        self.on_click = on_click
        self.scroller, self.overlay = scroller, overlay
        self.is_empty, self.on_start, self.on_band, self.pin_getter = is_empty, on_start, on_band, pin_getter
        self.band = Gtk.Box(css_name="rubberband", can_target=False, visible=False,
                            halign=Gtk.Align.START, valign=Gtk.Align.START)
        overlay.add_overlay(self.band)
        self.active = self.armed = False
        self.ctrl = False
        self.x0 = self.y0 = self.x = self.y = 0.0
        self.anchor = 0.0  # content y of the press
        self.timer = None
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._begin)
        drag.connect("drag-update", self._update)
        drag.connect("drag-end", self._end)
        drag.connect("cancel", lambda *_: self._end(cancelled=True))
        scroller.add_controller(drag)
        # A press on a row that turns into drag-and-drop never brings its button release
        # here (the drag takes the pointer), so the gesture still held that press and
        # swallowed the next one: it took two clicks to band again after a drag-and-drop
        # (user report 2026-11-12). Every new press starts the gesture afresh.
        fresh = Gtk.EventControllerLegacy(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        fresh.connect("event", lambda c, _event: self._fresh_press(drag, c))
        scroller.add_controller(fresh)

    def _fresh_press(self, drag, controller):
        event = controller.get_current_event()  # PyGObject passes the signal's event as None
        if event is not None and event.get_event_type() == Gdk.EventType.BUTTON_PRESS and not self.armed:
            drag.reset()
        return False

    def _adj(self):
        return self.scroller.get_vadjustment()

    def _begin(self, gesture, x, y):
        self.active, self.armed, self.ctrl = False, False, False
        picked = self.scroller.pick(x, y, Gtk.PickFlags.DEFAULT)
        if picked is None or not self.is_empty(picked):
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        self.armed = True
        self.x0, self.y0 = self.x, self.y = x, y
        self.anchor = y + self._adj().get_value()

    def _update(self, gesture, dx, dy):
        if not self.armed:
            return
        self.x, self.y = self.x0 + dx, self.y0 + dy
        if not self.active:
            if max(abs(dx), abs(dy)) < selection.BAND_THRESHOLD:
                return
            self.active = True
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            # Ctrl is read here, not at the press: pressed a moment late, it still counts
            self.ctrl = bool(gesture.get_current_event_state() & Gdk.ModifierType.CONTROL_MASK)
            self.on_start(self.ctrl, self.x0, self.y0)
            self.band.set_visible(True)
            self.timer = GLib.timeout_add(30, self._autoscroll)
        self._refresh()

    def _end(self, gesture=None, *_a, cancelled=False):
        if self.armed and not self.active and not cancelled and self.on_click is not None and gesture is not None:
            mods = gesture.get_current_event_state()
            self.on_click(bool(mods & Gdk.ModifierType.CONTROL_MASK), bool(mods & Gdk.ModifierType.SHIFT_MASK))
        self.armed = False
        if self.timer is not None:
            GLib.source_remove(self.timer)
            self.timer = None
        if self.active:
            self.active = False
            self.band.set_visible(False)

    def _autoscroll(self):
        adj = self._adj()
        step = selection.edge_scroll(self.y, self.scroller.get_height())
        if step:
            value = max(adj.get_lower(), min(adj.get_value() + step, adj.get_upper() - adj.get_page_size()))
            if value != adj.get_value():
                _pin_user_input(self.pin_getter())  # the user's own scrolling, not GTK's
                adj.set_value(value)
                self._refresh()
        return True

    def _refresh(self):
        offset = self._adj().get_value()
        width, height = self.scroller.get_width(), self.scroller.get_height()
        current = self.y + offset
        top = max(0.0, min(self.anchor, current) - offset)
        bottom = min(float(height), max(self.anchor, current) - offset)
        left = max(0.0, min(self.x0, self.x))
        right = min(float(width), max(self.x0, self.x))
        self.band.set_margin_start(int(left))
        self.band.set_margin_top(int(top))
        self.band.set_size_request(max(1, int(right - left)), max(1, int(bottom - top)))
        self.on_band(self.anchor, current, self.ctrl)


def _right_click(widget, handler):
    """Call handler(x, y) on a secondary click (right mouse button) on `widget`."""
    gesture = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
    gesture.connect("pressed", lambda g, _n, x, y: (g.set_state(Gtk.EventSequenceState.CLAIMED), handler(x, y)))
    widget.add_controller(gesture)


def _typeahead_overlay(child):
    """(overlay, label): `child` with a type-ahead label at its bottom."""
    overlay = Gtk.Overlay(child=child)
    label = Gtk.Label(halign=Gtk.Align.CENTER, valign=Gtk.Align.END, margin_bottom=12, visible=False,
                      can_target=False, ellipsize=Pango.EllipsizeMode.END)
    label.add_css_class("osd")
    label.add_css_class("typeahead")
    overlay.add_overlay(label)
    return overlay, label


def _flash_typeahead(label, text, found, timeout, no_match):
    """Show the typed text (red when nothing matches); hide it after the timeout."""
    old = getattr(label, "hide_id", 0)
    if old:
        GLib.source_remove(old)
    label.hide_id = 0
    if not text:
        label.set_visible(False)
        return
    label.set_text(text)
    (label.remove_css_class if found else label.add_css_class)("error")
    label.set_tooltip_text(None if found else no_match)
    label.set_visible(True)

    def done():
        label.hide_id = 0
        label.set_visible(False)
        return False
    label.hide_id = GLib.timeout_add(int(timeout * 1000) + 200, done)


def _expander_header(expander):
    """The inner header row of an Adw.ExpanderRow (an ActionRow with the "header"
    style class); it is what opens the row on a click. None if not found."""
    stack = [expander.get_first_child()]
    while stack:
        w = stack.pop(0)
        if w is None:
            continue
        if isinstance(w, Gtk.ListBoxRow) and w is not expander and w.has_css_class("header"):
            return w
        c = w.get_first_child()
        while c is not None:
            stack.append(c)
            c = c.get_next_sibling()
    return None


def _ask_name(parent, heading, body, initial, action_label, on_name, problem=None,
              suggest=None, note=None):
    """Ask for a name. `problem(name)` returns why it cannot be used (or None);
    the action button stays disabled while there is a problem. `suggest(typed)`
    gives names to offer in a list under the field (clicking one takes it);
    `note(name)` an extra hint when there is no problem (e.g. "replaces ...")."""
    dialog = Adw.AlertDialog(heading=heading, body=body)
    entry = Gtk.Entry(text=initial or "", activates_default=True)
    hint = Gtk.Label(xalign=0, wrap=True)
    hint.add_css_class("dim-label")
    hint.add_css_class("caption")
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    box.append(entry)
    box.append(hint)
    listing = None
    if suggest is not None:
        listing = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listing.add_css_class("boxed-list")
        scroller = Gtk.ScrolledWindow(child=listing, max_content_height=220, propagate_natural_height=True,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER)
        box.append(scroller)

        def take(_lb, row):
            entry.set_text(row.name)
            entry.grab_focus()
            entry.set_position(-1)
        listing.connect("row-activated", take)

        def refill():
            typed = entry.get_text()
            names = suggest("" if typed == (initial or "") else typed)  # the pre-filled name shows them all
            child = listing.get_first_child()
            while child is not None:
                nxt = child.get_next_sibling()
                listing.remove(child)
                child = nxt
            for n in names:
                row = Gtk.ListBoxRow()
                row.name = n
                row.set_child(Gtk.Label(label=n, xalign=0, margin_top=8, margin_bottom=8, margin_start=12,
                                        margin_end=12, ellipsize=Pango.EllipsizeMode.END))
                listing.append(row)
            scroller.set_visible(bool(names))
        refill()
    dialog.set_extra_child(box)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("ok", action_label)
    dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("ok")
    dialog.set_close_response("cancel")

    def check(*_):
        why = problem(entry.get_text()) if problem else (None if entry.get_text().strip() else "Enter a name")
        hint.set_text(why or (note(entry.get_text()) if note else "") or "")
        dialog.set_response_enabled("ok", why is None)
        if listing is not None:
            refill()
    entry.connect("changed", check)
    check()
    dialog.connect("response", lambda _d, r: on_name(entry.get_text()) if r == "ok" else None)
    dialog.present(parent)
    entry.grab_focus()


class CoverSearchDialog(Adw.Window):
    """"Set Cover...": results from Cover Art Archive, Deezer, iTunes (and Last.fm with a
    key; coversearch.py) as a grid; the chosen one is downloaded and saved into the
    album folder. "Choose File..." uses an image file instead.
    A modal window, not an Adw.Dialog, so the user can resize it (user request
    2026-11-17); its size is kept in the settings."""

    THUMB = 160
    WIDTH, HEIGHT = 720, 560

    def __init__(self, window, artist, album, on_cover, on_file=None):
        settings = getattr(window, "settings", None)
        super().__init__(title="Set Cover", transient_for=window, modal=True, destroy_with_parent=True,
                         default_width=getattr(settings, "cover_dialog_width", 0) or self.WIDTH,
                         default_height=getattr(settings, "cover_dialog_height", 0) or self.HEIGHT)
        self.set_size_request(360, 320)
        self.win, self.artist, self.album, self.on_cover = window, artist, album, on_cover
        self.on_file = on_file  # "Choose File...": the caller opens its file dialog
        self._gen = 0
        self._closed = False
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="coversearch")
        self.connect("close-request", self._on_close_request)
        keys = Gtk.ShortcutController()  # bubble: an open preview dialog takes Escape first
        keys.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("Escape"),
                                       action=Gtk.CallbackAction.new(lambda *_: (self.close(), True)[1])))
        self.add_controller(keys)

        self.entry = Gtk.SearchEntry(hexpand=True, text=coversearch.default_query(artist, album))
        self.entry.connect("activate", lambda *_: self.search())
        go = Gtk.Button(label="Search")
        go.add_css_class("suggested-action")
        go.connect("clicked", lambda *_: self.search())
        top = Gtk.Box(spacing=6, margin_top=6, margin_bottom=6, margin_start=12, margin_end=12)
        top.append(self.entry)
        top.append(go)
        self.note = Gtk.Label(wrap=True, xalign=0, margin_start=12, margin_end=12)
        self.note.add_css_class("dim-label")
        self.note.add_css_class("caption")
        self.lastfm_key = lastfm.effective_key(getattr(getattr(window, "settings", None), "lastfm_api_key", ""))
        sources = "Cover Art Archive (MusicBrainz), Deezer, iTunes" + (" and Last.fm" if self.lastfm_key else "")
        self.note.set_text(f"Searches {sources}. Only the text above is sent."
                           + ("" if self.lastfm_key else " (Last.fm: add your API key in Preferences.)"))

        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.SINGLE, homogeneous=True,
                                max_children_per_line=6, min_children_per_line=2, valign=Gtk.Align.START,
                                margin_top=6, margin_bottom=6, margin_start=6, margin_end=6,
                                activate_on_single_click=False)
        self.flow.connect("selected-children-changed", lambda *_: self.use.set_sensitive(bool(self.flow.get_selected_children())))
        self.flow.connect("child-activated", lambda _f, child: self._preview(child.get_child()))
        self.status = Adw.StatusPage(icon_name="system-search-symbolic", title="Searching\u2026")
        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(Gtk.ScrolledWindow(child=self.flow), "results")
        self.stack.add_named(self.status, "status")

        self.use = Gtk.Button(label="Use This Cover", sensitive=False)
        self.use.add_css_class("suggested-action")
        self.use.connect("clicked", lambda *_: self._use())
        bar = Gtk.ActionBar()
        bar.pack_end(self.use)
        if self.on_file is not None:
            pick = Gtk.Button(label="Choose File\u2026", tooltip_text="Use an image file from this computer instead")
            pick.connect("clicked", lambda *_: self._choose_file())
            bar.pack_start(pick)
        self.spinner = Gtk.Spinner()
        bar.pack_start(self.spinner)

        page = Adw.ToolbarView()
        page.add_top_bar(Adw.HeaderBar())
        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header.append(top)
        header.append(self.note)
        page.add_top_bar(header)
        page.set_content(self.stack)
        page.add_bottom_bar(bar)
        self.set_content(page)
        self.search()

    def _on_close_request(self, _w):
        settings = getattr(self.win, "settings", None)
        if settings is not None and not self.is_maximized():
            w, h = self.get_width(), self.get_height()
            if w > 0 and h > 0 and (w, h) != (settings.cover_dialog_width, settings.cover_dialog_height):
                settings.cover_dialog_width, settings.cover_dialog_height = w, h
                self.win.save_settings_now()
        self._shutdown()
        return False

    def _shutdown(self):
        self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _later(self, fn, *args):
        GLib.idle_add(lambda: (None if self._closed else fn(*args), False)[1])

    def search(self):
        self._gen += 1
        gen = self._gen
        query = self.entry.get_text()
        child = self.flow.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.flow.remove(child)
            child = nxt
        self.status.set_title("Searching\u2026")
        self.status.set_description(None)
        self.stack.set_visible_child_name("status")
        self.spinner.start()
        artist = self.artist if query == coversearch.default_query(self.artist, self.album) else ""
        album = self.album if artist else ""
        key = self.lastfm_key
        self._pool.submit(lambda: self._later(self._results, gen,
                                              *coversearch.search(query, artist, album, lastfm_key=key)))

    def _results(self, gen, found, errors):
        if gen != self._gen:
            return
        self.spinner.stop()
        if not found:
            self.status.set_title("No covers found")
            self.status.set_description("Try other words. " + "; ".join(errors) if errors else "Try other words.")
            return
        self.note.set_text(f"{len(found)} results" + (f" (not reached: {'; '.join(e.split(':')[0] for e in errors)})"
                                                      if errors else ""))
        self.stack.set_visible_child_name("results")
        for c in found:
            tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            tile.candidate = c
            pic = CoverThumb(self.THUMB)
            tile.thumb = pic
            tile.append(pic)
            for text, css in ((c.label, None), (f"{c.provider}" + (f" \u00b7 {c.size}" if c.size else ""), "dim-label")):
                lbl = Gtk.Label(label=text, ellipsize=Pango.EllipsizeMode.END, max_width_chars=18)
                lbl.add_css_class("caption")
                if css:
                    lbl.add_css_class(css)
                tile.append(lbl)
            tile.set_tooltip_text(f"{c.label}\n{c.provider}")
            self.flow.append(tile)
            self._pool.submit(self._load_thumb, gen, tile)

    def _load_thumb(self, gen, tile):  # worker thread
        try:
            data = coversearch.http_get(tile.candidate.thumb_url)
            texture = _decode_texture(data)
        except Exception:
            texture = None
        self._later(self._thumb_loaded, gen, tile, texture)

    def _thumb_loaded(self, gen, tile, texture):
        if gen != self._gen:
            return
        if texture is None:  # e.g. Cover Art Archive has no cover for that release: drop it
            parent = tile.get_parent()
            if parent is not None:
                self.flow.remove(parent)
            return
        tile.thumb.set_texture(texture)

    def _choose_file(self):
        self.close()
        self.on_file()

    def _preview(self, tile):
        """Double-click (or Enter) on a result: show the full image in a dialog on
        top of this one; its "Use This Cover" uses the already downloaded data."""
        self.flow.select_child(tile.get_parent())
        CoverPreviewDialog(self, tile).present(self)

    def _full_data(self, tile, done):
        """Download the full image once per tile (worker thread); done(data, err)
        runs on the UI thread."""
        data = getattr(tile, "full_data", None)
        if data:
            done(data, None)
            return
        url = tile.candidate.full_url

        def work():
            try:
                data, err = coversearch.http_get(url), None
            except Exception as e:
                data, err = None, e
            self._later(self._got_full, tile, data, err, done)
        self._pool.submit(work)

    def _got_full(self, tile, data, err, done):
        if data:
            tile.full_data = data
        done(data, err)

    def _use(self, tile=None):
        if tile is None:
            chosen = self.flow.get_selected_children()
            if not chosen:
                return
            tile = chosen[0].get_child()
        c = tile.candidate
        self.use.set_sensitive(False)
        self.spinner.start()
        self.note.set_text(f"Downloading the cover from {c.provider}\u2026")
        self._full_data(tile, self._downloaded)

    def _downloaded(self, data, err):
        self.spinner.stop()
        if err is not None or not data:
            self.note.set_text(f"Download failed: {err}")
            self.use.set_sensitive(True)
            return
        self.close()
        self.on_cover(data)


class CoverPreviewDialog(Adw.Dialog):
    """The full image of one cover search result (CoverSearchDialog._preview)."""

    def __init__(self, search, tile):
        c = tile.candidate
        super().__init__(title=c.label, content_width=640, content_height=680)
        self.search, self.tile = search, tile
        self._closed = False
        self.connect("closed", lambda *_: setattr(self, "_closed", True))
        self.title = Adw.WindowTitle(title=c.label, subtitle=c.provider)
        header = Adw.HeaderBar(title_widget=self.title)
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.SCALE_DOWN, can_shrink=True,  # never enlarged: shows the real resolution
                                   hexpand=True, vexpand=True)
        self.status = Adw.StatusPage(title="Loading\u2026")
        self.spinner = Gtk.Spinner(spinning=True, width_request=32, height_request=32)
        self.status.set_child(self.spinner)
        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(self.status, "status")
        self.stack.add_named(self.picture, "picture")
        self.use = Gtk.Button(label="Use This Cover", sensitive=False)
        self.use.add_css_class("suggested-action")
        self.use.connect("clicked", lambda *_: self._use())
        bar = Gtk.ActionBar()
        bar.pack_end(self.use)
        page = Adw.ToolbarView()
        page.add_top_bar(header)
        page.set_content(self.stack)
        page.add_bottom_bar(bar)
        self.set_child(page)
        search._full_data(tile, self._loaded)

    def _loaded(self, data, err):
        if self._closed:
            return
        self.spinner.set_spinning(False)
        texture = None
        if data and err is None:
            try:
                texture = _decode_texture(data)
            except Exception as e:
                err = e
        if texture is None:
            self.status.set_title("Could not load the image")
            self.status.set_description(str(err) if err else None)
            self.status.set_icon_name("image-missing-symbolic")
            self.status.set_child(None)
            return
        self.picture.set_paintable(texture)
        self.stack.set_visible_child_name("picture")
        self.title.set_subtitle(f"{self.tile.candidate.provider} \u00b7 "
                                f"{texture.get_width()} \u00d7 {texture.get_height()} px")
        self.use.set_sensitive(True)

    def _use(self):
        self.close()
        self.search._use(self.tile)


class SearchPage:
    """Window mixin: the Search view. Logic is in search.py."""

    def _build_search_page(self):
        self._search_gen = 0
        self._search_result = None
        self._search_cats = searchlib.available_categories([], (0, 19, 0))  # until we know the server
        self._filling_cats = False
        self.search_cat_model = Gtk.StringList()
        self.search_cat = Gtk.DropDown(model=self.search_cat_model, valign=Gtk.Align.CENTER,
                                       tooltip_text="What to search in")
        self.search_entry = Gtk.SearchEntry(hexpand=True)
        self.search_entry.connect("activate", lambda _e: self.run_search())
        self._fill_search_categories(self.settings.search_category or searchlib.DEFAULT)
        self.search_cat.connect("notify::selected", self._on_search_category)
        top = Gtk.Box(spacing=6, margin_top=6, margin_start=12, margin_end=12)
        top.append(self.search_cat)
        top.append(self.search_entry)
        self.search_help = Gtk.Label(xalign=0, wrap=True, margin_start=12, margin_end=12, margin_bottom=6)
        self.search_help.add_css_class("dim-label")
        self.search_help.add_css_class("caption")

        self.search_store = Gio.ListStore(item_type=Song)
        view = make_song_list(self.search_store, self._search_activate, self._search_edit_row,
                              [("list-add-symbolic", "Add to queue", self._search_add_row)],
                              multi=True, on_drag=self._drag_search, cover=self._search_cover,
                              on_menu=self._search_menu)
        self.search_view = view
        self.search_sel = view.get_model()
        self.search_sel.connect("selection-changed", lambda *_: self._search_selection_changed())
        self.search_status = Adw.StatusPage(
            icon_name="system-search-symbolic", title="Search",
            description="Type above and press Enter to search your library via MPD. "
                        "The menu next to the field chooses what to search in.")
        self.search_stack = Gtk.Stack(vexpand=True)
        self.search_stack.add_named(self.search_status, "status")
        self.search_stack.add_named(_loading_box("Searching…"), "loading")
        self.search_stack.add_named(Gtk.ScrolledWindow(child=view), "results")

        bar = Gtk.ActionBar()
        self.search_stats = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.search_stats.add_css_class("dim-label")
        bar.pack_start(self.search_stats)
        self.s_add = _text_button("Add to queue", lambda: self.add_tracks(self._search_targets()))
        self.s_play = _text_button("Play", lambda: self.play_tracks(self._search_targets()))
        self.s_more = Gtk.MenuButton(icon_name="view-more-symbolic", tooltip_text="More")
        more = Gio.Menu()
        more.append("Add to Playlist…", "win.search-to-playlist")
        more.append("Show in Artists", "win.search-show-artists")
        more.append("Show in Albums", "win.search-show-albums")
        more.append("Edit Tags…", "win.search-edit")
        self.s_more.set_menu_model(more)
        for name, fn in (("search-to-playlist", lambda: self.add_to_playlist(self._search_targets())),
                         ("search-show-artists", lambda: self._search_show("artists")),
                         ("search-show-albums", lambda: self._search_show("albums")),
                         ("search-edit", lambda: self.edit_tracks(self._search_targets()))):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a, fn=fn: fn())
            self.add_action(action)
        for w in (self.s_more, self.s_play, self.s_add):
            bar.pack_end(w)

        page = Adw.ToolbarView()
        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        header.append(top)
        header.append(self.search_help)
        page.add_top_bar(header)
        page.set_content(self.search_stack)
        page.add_bottom_bar(bar)
        self._show_search_help()
        self._search_selection_changed()
        return page

    def _current_search_category(self):
        i = self.search_cat.get_selected()
        return self._search_cats[i] if 0 <= i < len(self._search_cats) else self._search_cats[-1]

    def _fill_search_categories(self, key):
        self._filling_cats = True
        try:
            self.search_cat_model.splice(0, self.search_cat_model.get_n_items(),
                                         [c.label for c in self._search_cats])
            keys = [c.key for c in self._search_cats]
            want = key if key in keys else searchlib.DEFAULT
            self.search_cat.set_selected(keys.index(want) if want in keys else len(keys) - 1)
        finally:
            self._filling_cats = False

    def refresh_search_categories(self):
        """Offer what this server supports (tagtypes, version)."""
        try:
            tags = self.client.tagtypes()
        except (OSError, MPDError):
            tags = []
        cats = searchlib.available_categories(tags, searchlib.parse_version(self.client.protocol_version))
        if [c.key for c in cats] != [c.key for c in self._search_cats]:
            key = self._current_search_category().key
            self._search_cats = cats
            self._fill_search_categories(key)
            self._show_search_help()

    def _show_search_help(self):
        cat = self._current_search_category()
        self.search_help.set_text(cat.help)
        self.search_entry.set_placeholder_text(f"Search by {cat.label.lower()}…")

    def _on_search_category(self, *_):
        if self._filling_cats:
            return
        cat = self._current_search_category()
        self._show_search_help()
        if self.settings.search_category != cat.key:
            self.settings.search_category = cat.key
            self._schedule_flush()
        if self.search_entry.get_text().strip():
            self.run_search(quiet=True)  # text typed for the old category may not fit the new one

    def focus_search(self):
        self.views.set_visible_child_name("search")
        self.search_entry.grab_focus()

    def run_search(self, quiet=False):
        text = self.search_entry.get_text().strip()
        cat = self._current_search_category()
        self._search_gen += 1
        gen = self._search_gen
        if not text:
            self._show_search_result(None)
            return
        if cat.key == searchlib.MODIFIED and searchlib.parse_modified(text) is None:
            if quiet:
                self._show_search_result(None)
            else:
                self.show_toast("Enter a number of days, or a date like 2015/01/31")
            return
        self.search_stack.set_visible_child_name("loading")
        self.search_stats.set_text("")
        self.search_fetcher.run(lambda client: searchlib.run_search(client, cat.key, text),
                                 lambda result, err: self._search_done(gen, result, err),
                                 wanted=lambda: gen == self._search_gen)

    def _search_done(self, gen, result, err):
        if gen != self._search_gen:  # a newer search (or a reconnect) superseded it
            return
        if err is not None:
            self._show_search_result(None, "Search failed", describe_error(err, *self.cfg[:2]))
            return
        self._show_search_result(result)

    def _show_search_result(self, result, title="Search", description=None):
        self._search_result = result
        songs = result.songs if result else []
        self.search_store.splice(0, self.search_store.get_n_items(), [Song(s) for s in songs])
        if result is not None and songs:
            self.search_stack.set_visible_child_name("results")
        else:
            if result is not None:
                title, description = "No tracks found", "Try another word or category."
            self.search_status.set_title(title)
            self.search_status.set_description(
                description or "Type above and press Enter to search your library via MPD. "
                               "The menu next to the field chooses what to search in.")
            self.search_stack.set_visible_child_name("status")
        self.search_stats.set_text(searchlib.stats_text(result) if result is not None else "")
        if result is not None and result.truncated:
            self.search_stats.set_tooltip_text(f"Only the first {searchlib.LIMIT} results are shown")
        else:
            self.search_stats.set_tooltip_text(None)
        self._search_selection_changed()

    def _search_selected(self):
        bits = self.search_sel.get_selection()
        return [self.search_store.get_item(bits.get_nth(i)) for i in range(bits.get_size())]

    def _search_targets(self):
        """Selected results, or all of them when nothing is selected."""
        songs = self._search_selected() or [self.search_store.get_item(i)
                                            for i in range(self.search_store.get_n_items())]
        return [s.data for s in songs]

    def _search_for_row(self, song):
        selected = self._search_selected()
        return [s.data for s in (selected if song in selected else [song])]

    def _search_selection_changed(self):
        n_sel = len(self._search_selected())
        n_all = self.search_store.get_n_items()
        for w in (self.s_add, self.s_play, self.s_more):
            w.set_sensitive(n_all > 0)
        for name in ("search-show-artists", "search-show-albums"):
            self.lookup_action(name).set_enabled(n_all > 0)
        if n_sel:
            self.s_add.set_label(f"Add {n_sel} to queue" if n_sel > 1 else "Add to queue")
            self.s_play.set_label(f"Play {n_sel}" if n_sel > 1 else "Play")
        else:
            self.s_add.set_label("Add all to queue" if n_all > 1 else "Add to queue")
            self.s_play.set_label("Play all" if n_all > 1 else "Play")

    def _search_cover(self, song, row):
        """Album cover of a search result (cached per album folder by the loader)."""
        row.thumb.set_visible(self.show_covers)
        uri = song.uri
        if not self.show_covers or not uri or "://" in uri:
            return
        self.cover_loader.request(
            uri, lambda tex: row.thumb.set_texture(tex) if row._song is song else None,
            wanted=lambda: row._song is song, song=song.data, size=THUMB_SIZE)

    def _search_menu(self, song, row, x, y):
        """Right-click on a result: the library's track menu plus Show in Artists / Albums,
        for the selection (a right-click outside it selects that row first)."""
        if song not in self._search_selected():
            for i in range(self.search_sel.get_n_items()):
                if self.search_sel.get_item(i) is song:
                    _select_like_click(self.search_view, i)
                    break
        tracks = self._search_for_row(song)
        entries = self._track_menu_entries(lambda fn: fn(tracks))
        entries[4:4] = [("Show in Artists", lambda: self._search_show("artists", tracks)),
                        ("Show in Albums", lambda: self._search_show("albums", tracks))]
        _context_menu(row, x, y, entries)

    def _search_activate(self, song):  # double click / Enter: add to the queue
        self.add_tracks(self._search_for_row(song))

    def _search_add_row(self, song):
        self.add_tracks(self._search_for_row(song))

    def _search_edit_row(self, song):
        self.edit_tracks(self._search_for_row(song))

    def _drag_search(self, song):
        if song is None:
            return None
        self._drag = ("tracks", self._search_for_row(song))
        return self._tag(tracks_label(self._drag[1]))

    def _search_show(self, where, tracks=None):
        """The first (selected) result's album in the Artists or Albums view, selected,
        like the queue's Show in Artists / Show in Albums."""
        tracks = tracks if tracks is not None else self._search_targets()
        if tracks:
            (self.show_song_in_artists if where == "artists" else self.show_song_in_albums)(tracks[0])

    def reset_search(self):
        self._search_gen += 1
        self._show_search_result(None)


class _PlaylistGrouping:
    """The owner QueueHeader expects, for the playlist track list: collapse state,
    covers, and select/drag of a whole album. Collapse ids are playlist positions."""

    def __init__(self, win):
        self.w = win
        self.collapsed_groups = set()
        self.groups = []

    @property
    def show_covers(self):
        return self.w.show_covers

    @property
    def cover_loader(self):
        return self.w.cover_loader

    def queue_song_at(self, pos):
        songs = self.w._pl_songs
        return songs[pos].get("file") if 0 <= pos < len(songs) else None

    def queue_tags_at(self, pos):
        songs = self.w._pl_songs
        return songs[pos] if 0 <= pos < len(songs) else None

    def toggle_group(self, group):
        self.collapsed_groups ^= {group.head_id}
        self.w._pl_regroup()

    def select_group(self, group, extend=False):
        objs = self.w._pl_objs
        count = sum(1 for s in objs[group.start:group.end] if not s.hidden)
        start = self.w._pl_row_index(objs[group.start]) if count else None
        if start is not None:
            _select_like_click(self.w.pl_songs_view, start, count, extend)

    def _group_tracks(self, group):
        return self.w._pl_songs[group.start:group.end]

    def play_group(self, group):
        """A header's hover Play button: replace the queue with this album, play."""
        self.w.play_tracks(self._group_tracks(group))

    def add_group(self, group):
        """A header's hover Add button: append this album to the queue."""
        self.w.add_tracks(self._group_tracks(group), group.title or None)

    def header_menu(self, group, _widget, _x, _y):
        """Right-click on a playlist album header: selects the album (no menu yet)."""
        self.select_group(group)

    def drag_group(self, group):
        if group is None:
            return None
        self.w._drag = ("tracks", self.w._pl_songs[group.start:group.end])
        return self.w._tag(group.title or tracks_label(self.w._drag[1]))


class PlaylistsPage:
    """Window mixin: the Playlists view (MPD's stored playlists).
    Logic is in playlists.py, MPD commands in mpd_client.py."""

    def _build_playlists_page(self):
        self._playlists = []        # [(name, last_modified)], sorted
        self._pl_name = None        # playlist shown on the right
        self._pl_songs = []         # its songs (dicts), in playlist order
        self._pl_loaded = False     # fetched since the last (re)connect
        self._pl_refreshing = False
        self._last_playlist = ""    # preselected in "Add to playlist"

        self.pl_names = Gtk.StringList()
        self.pl_sel = Gtk.SingleSelection(model=self.pl_names)
        self.pl_sel.connect("selection-changed", self._on_playlist_selected)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup_playlist_row)
        factory.connect("bind", self._bind_playlist_row)
        pl_view = Gtk.ListView(model=self.pl_sel, factory=factory)
        pl_view.add_css_class("navigation-sidebar")
        self.pl_view = pl_view
        self.pl_left = Gtk.Stack()
        # Type-ahead in both lists (typing jumps to a playlist / a track), as in the library.
        self._pl_typeahead, self._pl_matcher = TypeAhead(), Matcher([])
        self._plsong_typeahead, self._plsong_matcher = TypeAhead(), Matcher([])
        self.pl_list_scroll = Gtk.ScrolledWindow(min_content_width=150, child=pl_view)
        overlay, self.pl_typeahead_label = _typeahead_overlay(self.pl_list_scroll)
        self.pl_left.add_named(overlay, "list")
        empty = Adw.StatusPage(icon_name="view-list-symbolic", title="No playlists",
                               description="Save the queue, or use “Add to playlist” in the "
                                           "library or search, to create playlists.")
        empty.add_css_class("compact")
        self.pl_left.add_named(empty, "empty")

        # store: Songs, and when grouped a QueueAlbum header item before each album (list
        # rows, not GTK section headers: those made the list jump, see QueueAlbum)
        # -> filter (hides tracks of collapsed albums)
        self.pl_store = Gio.ListStore(item_type=GObject.Object)
        self._pl_objs = []           # the Song objects in playlist order (Song.pos = position)
        self._pl_rows = []           # what pl_store holds, in order
        self._pl_album_items = {}    # head id -> QueueAlbum, reused across regroupings
        self._plg = _PlaylistGrouping(self)
        self._pl_grouped = self.settings.playlist_grouped
        self._pl_filter = Gtk.CustomFilter.new(lambda obj: isinstance(obj, QueueAlbum) or not obj.hidden)
        filtered = Gtk.FilterListModel(model=self.pl_store, filter=self._pl_filter)
        songs_view = make_song_list(filtered, self._pl_activate, self._pl_edit_row,
                                    [("list-remove-symbolic", "Remove from playlist", self._pl_remove_row)],
                                    multi=True, on_drag=self._drag_pl_songs, header_owner=self._plg)
        self.pl_songs_view = songs_view
        self.pl_songs_sel = songs_view.get_model()
        self._pl_fixing_selection = False
        self.pl_songs_sel.connect("selection-changed", self._pl_drop_header_selection)
        self.pl_songs_sel.connect("selection-changed", lambda *_: self._pl_update_actions())
        self.pl_status = Adw.StatusPage(icon_name="view-list-symbolic", title="No playlist selected")
        self.pl_right = Gtk.Stack(vexpand=True)
        self.pl_songs_scroll = Gtk.ScrolledWindow(child=songs_view)
        self._pl_pin = _pin_list_top(self.pl_songs_scroll.get_vadjustment(), lambda: self._pl_grouped)
        _watch_user_scroll(self.pl_songs_scroll, lambda: self._pl_pin)
        overlay, self.plsong_typeahead_label = _typeahead_overlay(self.pl_songs_scroll)
        self.pl_right.add_named(overlay, "songs")
        self.pl_right.add_named(self.pl_status, "status")
        self.pl_right.set_visible_child_name("status")

        split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, position=190,
                          resize_start_child=False, resize_end_child=True,
                          shrink_start_child=False, shrink_end_child=False)
        split.set_start_child(self.pl_left)
        split.set_end_child(self.pl_right)
        self.pl_split = split

        bar = Gtk.ActionBar()
        self.pl_info = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.pl_info.add_css_class("dim-label")
        self.p_add = _text_button("Add to queue", self._pl_add_to_queue)
        self.p_play = _text_button("Play", self._pl_play)
        self.p_remove = _text_button("Remove", self._pl_remove_selected)
        more = Gio.Menu()
        more.append("Rename…", "win.pl-rename")
        more.append("Remove Duplicates", "win.pl-dedupe")
        more.append("Remove Invalid Tracks", "win.pl-invalid")
        more.append("Delete Playlist…", "win.pl-delete")
        grouping = Gio.Menu()
        grouping.append("Group Albums", "win.pl-grouped")
        grouping.append("Collapse All Albums", "win.pl-collapse-all")
        grouping.append("Expand All Albums", "win.pl-expand-all")
        more.append_section(None, grouping)
        self.p_more = Gtk.MenuButton(icon_name="view-more-symbolic", tooltip_text="More", menu_model=more)
        for name, fn in (("pl-rename", self._pl_rename), ("pl-dedupe", self._pl_dedupe),
                         ("pl-invalid", self._pl_remove_invalid), ("pl-delete", self._pl_delete)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a, fn=fn: fn())
            self.add_action(action)
        grouped = Gio.SimpleAction.new_stateful("pl-grouped", None, GLib.Variant.new_boolean(self._pl_grouped))
        grouped.connect("change-state", lambda a, v: self._set_pl_grouped(v.get_boolean()))
        self.add_action(grouped)
        for name, closed in (("pl-collapse-all", True), ("pl-expand-all", False)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a, closed=closed: self._pl_collapse_all(closed))
            self.add_action(action)
        self._set_pl_grouped(self._pl_grouped, save=False)
        bar.pack_start(self.p_add)
        bar.pack_start(self.p_play)
        bar.pack_start(self.pl_info)
        bar.pack_end(self.p_more)
        bar.pack_end(self.p_remove)

        page = Adw.ToolbarView()
        page.set_content(split)
        page.add_bottom_bar(bar)
        self._pl_update_actions()
        return page

    def _setup_playlist_row(self, _factory, item):
        label = Gtk.Label(xalign=0, margin_top=8, margin_bottom=8, margin_start=12, margin_end=12,
                          ellipsize=Pango.EllipsizeMode.END)
        label.playlist = None
        src = DragSource(actions=Gdk.DragAction.COPY)
        src.connect("prepare", lambda _s, _x, _y, lbl=label: self._drag_playlist(lbl.playlist))
        label.add_controller(src)
        item.set_child(label)

    def _bind_playlist_row(self, _factory, item):
        name = item.get_item().get_string()
        item.get_child().playlist = name
        item.get_child().set_text(name)

    def _jump_to_playlist(self, text, step=False):
        current = self.pl_sel.get_selected()
        after = current if step and current != Gtk.INVALID_LIST_POSITION else -1
        idx = self._pl_matcher.find(text, after)
        if idx is None:
            return False
        if idx != current:
            self.pl_sel.set_selected(idx)  # shows that playlist
        self.pl_view.scroll_to(idx, Gtk.ListScrollFlags.FOCUS, None)
        return True

    def _jump_to_playlist_song(self, text, step=False):
        bits = self.pl_songs_sel.get_selection()
        current = bits.get_nth(0) if bits.get_size() else -1
        if current >= 0:
            item = self.pl_songs_sel.get_item(current)
            current = item.pos if item is not None else -1
        idx = self._plsong_matcher.find(text, current if step else -1)
        if idx is None or idx >= len(self._pl_objs):
            return False
        song = self._pl_objs[idx]
        if song.hidden and song.group is not None:  # inside a collapsed album: open it
            self._plg.collapsed_groups.discard(song.group.head_id)
            self._pl_regroup()
        row = self._pl_row_index(song)
        if row is None:
            return False
        self.pl_songs_view.scroll_to(row, Gtk.ListScrollFlags.FOCUS | Gtk.ListScrollFlags.SELECT, None)
        return True

    def _pl_row_index(self, obj):
        """Position of a playlist item in the shown (filtered) list, or None."""
        sel = self.pl_songs_sel
        return next((i for i in range(sel.get_n_items()) if sel.get_item(i) is obj), None)

    def _pl_drop_header_selection(self, sel, position, n_items):
        """Album header rows are never selected (Ctrl+A and Shift ranges would include
        them); the album is selected through its tracks (as in the queue)."""
        if self._pl_fixing_selection:
            return
        self._pl_fixing_selection = True
        try:
            for pos in range(position, min(position + n_items, sel.get_n_items())):
                if sel.is_selected(pos) and isinstance(sel.get_item(pos), QueueAlbum):
                    sel.unselect_item(pos)
        finally:
            self._pl_fixing_selection = False

    def _drag_playlist(self, name):
        if not name:
            return None
        self._drag = ("playlist", name)
        return self._tag(name)

    def ensure_playlists(self):
        if not self._pl_loaded and self.client.connected:
            self.refresh_playlists()

    def refresh_playlists(self):
        entries = self.safe(self.client.listplaylists)
        if entries is None:
            return
        self._pl_loaded = True
        self._playlists = pllib.sorted_playlists(entries)
        names = [n for n, _ in self._playlists]
        keep = self._pl_name if self._pl_name in names else (names[0] if names else None)
        self._pl_refreshing = True
        try:
            self.pl_names.splice(0, self.pl_names.get_n_items(), names)
            self.pl_sel.set_selected(names.index(keep) if keep is not None else Gtk.INVALID_LIST_POSITION)
        finally:
            self._pl_refreshing = False
        self.pl_left.set_visible_child_name("list" if names else "empty")
        self._pl_matcher = Matcher(names)
        self._show_playlist(keep)

    def _on_playlist_selected(self, sel, *_):
        if self._pl_refreshing:
            return
        item = sel.get_selected_item()
        self._show_playlist(item.get_string() if item is not None else None)

    def _show_playlist(self, name):
        self._pl_name = name
        songs = []
        if name is not None:
            songs = self.safe(self.client.listplaylistinfo, name)
            if songs is None:
                songs = []
        if name != getattr(self, "_pl_grouped_for", None):
            self._plg.collapsed_groups = set()  # another playlist: start with every album open
        self._pl_grouped_for = name
        self._pl_songs = songs
        objs = []
        for i, t in enumerate(songs):
            o = Song(t)
            o.pos = i
            objs.append(o)
        self._pl_objs = objs
        self._pl_album_items = {}  # another list: no header items to keep
        self._pl_prepare()
        _rows_changing(self._pl_pin)
        self._pl_apply_rows()
        # title first, then the artist (a later word matches too: "davis" finds Miles Davis' tracks)
        self._plsong_matcher = Matcher([f"{Song(t).title} {t.get('artist', '')}" for t in songs])
        if name is None:
            self.pl_status.set_title("No playlist selected")
            self.pl_status.set_description(None)
            self.pl_right.set_visible_child_name("status")
        elif not songs:
            self.pl_status.set_title("Empty playlist")
            self.pl_status.set_description(f"“{name}” has no tracks.")
            self.pl_right.set_visible_child_name("status")
        else:
            self.pl_right.set_visible_child_name("songs")
        if songs:
            n = len(songs)
            self.pl_info.set_text(f"{n} track{'s' if n != 1 else ''} "
                                  f"({searchlib.format_duration(pllib.total_duration(songs))})")
        else:
            self.pl_info.set_text("")
        self._pl_update_actions()

    def _pl_selected_positions(self):
        """Playlist positions of the selected rows; a collapsed album's row counts
        as all its tracks."""
        bits = self.pl_songs_sel.get_selection()
        out = []
        for i in range(bits.get_size()):
            out.extend(self._pl_positions_of(self.pl_songs_sel.get_item(bits.get_nth(i))))
        return out

    def _pl_positions_of(self, song):
        if not isinstance(song, Song):
            return []  # nothing, or an album header row (never selected)
        if song.summary and song.group is not None:
            return list(range(song.group.start, song.group.end))
        return [song.pos]

    # -- album groups in the playlist (like the queue's "Group Albums") -------------------
    def _pl_prepare(self):
        objs = self._pl_objs
        if not self._pl_grouped:
            self._plg.groups = []
            _ungroup(objs)
            return
        ids = [f"p{i}" for i in range(len(objs))]
        self._plg.groups = _apply_groups(objs, self._plg.collapsed_groups, ids=ids)

    def _pl_apply_rows(self):
        """Put the songs into the store, when grouped with a QueueAlbum before each
        album. Header items are kept per album (head id) and get the current group;
        only the changed part of the store is spliced (playback.changed_range), so
        GTK keeps its scroll anchor on the rows it shows."""
        rows, items = [], {}
        if self._pl_grouped:
            for g in self._plg.groups:
                item = self._pl_album_items.get(g.head_id) or QueueAlbum(g)
                item.group = g
                items[g.head_id] = item
                rows.append(item)
                rows.extend(self._pl_objs[g.start:g.end])
        else:
            rows = list(self._pl_objs)
        self._pl_album_items = items
        start, n_remove, insert = changed_range(self._pl_rows, rows)
        self._pl_rows = rows
        if n_remove or insert:
            self.pl_store.splice(start, n_remove, insert)
        for item in items.values():  # headers on screen show the current group
            if item.widget is not None:
                item.widget.bind(item.group)

    def _pl_regroup(self):
        self._pl_prepare()
        _rows_changing(self._pl_pin)
        self._pl_apply_rows()
        self._pl_filter.changed(Gtk.FilterChange.DIFFERENT)
        for song in self._pl_objs:
            if song.row is not None:
                song.row.refresh_texts()
        self._pl_update_actions()

    def _set_pl_grouped(self, on, save=True):
        on = bool(on)
        self.lookup_action("pl-grouped").set_state(GLib.Variant.new_boolean(on))
        for name in ("pl-collapse-all", "pl-expand-all"):
            self.lookup_action(name).set_enabled(on)
        if save and on != self.settings.playlist_grouped:
            self.settings.playlist_grouped = on
            self._schedule_flush()
        self._pl_grouped = on
        self._pl_regroup()

    def _pl_collapse_all(self, closed):
        self._plg.collapsed_groups = {g.head_id for g in self._plg.groups} if closed else set()
        self._pl_regroup()

    def _pl_update_actions(self):
        has = self._pl_name is not None
        n_sel = len(self._pl_selected_positions())
        self.p_add.set_sensitive(has and bool(self._pl_songs))
        self.p_play.set_sensitive(has and bool(self._pl_songs))
        self.p_more.set_sensitive(has)
        self.p_remove.set_sensitive(n_sel > 0)
        self.p_remove.set_label(f"Remove ({n_sel})" if n_sel > 1 else "Remove")
        self.p_add.set_label(f"Add {n_sel} to queue" if n_sel > 1 else "Add to queue")
        self.p_play.set_label(f"Play {n_sel}" if n_sel > 1 else "Play")
        for name in ("pl-dedupe", "pl-invalid"):
            self.lookup_action(name).set_enabled(has and bool(self._pl_songs))

    def _pl_rows_for(self, song):
        """Positions a row's button acts on: the selection if the row is in it."""
        own = self._pl_positions_of(song)
        selected = self._pl_selected_positions()
        return selected if own and own[0] in selected else own

    def _pl_activate(self, song):
        self.add_tracks([self._pl_songs[p] for p in self._pl_rows_for(song)])

    def _pl_edit_row(self, song):
        self.edit_tracks([self._pl_songs[p] for p in self._pl_rows_for(song)])

    def _drag_pl_songs(self, song):
        if song is None:
            return None
        self._drag = ("tracks", [self._pl_songs[p] for p in self._pl_rows_for(song)])
        return self._tag(tracks_label(self._drag[1]))

    def _pl_add_to_queue(self):
        positions = self._pl_selected_positions()
        if positions:
            self.add_tracks([self._pl_songs[p] for p in positions])
        elif self._pl_name and self.change_queue(lambda: self.client.load_playlist(self._pl_name) or True):
            self.show_toast(f"Added “{self._pl_name}”")

    def _pl_play(self):
        positions = self._pl_selected_positions()
        if positions:
            self.play_tracks([self._pl_songs[p] for p in positions])
        elif self._pl_name:
            name = self._pl_name
            self.change_queue(lambda: (self.client.clear(), self.client.load_playlist(name), self.client.play()))

    def _pl_remove(self, positions, done_text=None):
        if not positions or self._pl_name is None:
            return
        name = self._pl_name
        if self.safe(lambda: self.client.playlist_delete(name, positions) or True):
            if done_text:
                self.show_toast(done_text)
        self.refresh_playlists()

    def _pl_remove_row(self, song):
        self._pl_remove(self._pl_rows_for(song))

    def _pl_remove_selected(self):
        self._pl_remove(self._pl_selected_positions())

    def _pl_dedupe(self):
        positions = pllib.duplicate_positions(self._pl_songs)
        if not positions:
            self.show_toast("No duplicates found")
            return
        n = len(positions)
        self._pl_remove(positions, f"Removed {n} duplicate{'s' if n != 1 else ''}")

    def _pl_remove_invalid(self):
        positions = pllib.invalid_positions(self._pl_songs)
        if not positions:
            self.show_toast("No invalid tracks found")
            return
        n = len(positions)
        self._pl_remove(positions, f"Removed {n} invalid track{'s' if n != 1 else ''}")

    def _playlist_names(self):
        return [n for n, _ in self._playlists]

    def _pl_rename(self):
        old = self._pl_name
        if old is None:
            return

        def rename(new):
            def go():
                if self.safe(lambda: self.client.rename_playlist(old, new) or True):
                    self._pl_name = new
                self.refresh_playlists()
            if pllib.is_overwrite(new, self._playlist_names(), current=old):
                _confirm(self, "Overwrite Playlist?", f"A playlist named “{new}” already exists.",
                         "Overwrite", lambda: self.safe(lambda: self.client.delete_playlist(new) or True) and go())
            else:
                go()
        _ask_name(self, "Rename Playlist", "Enter new name for playlist:", old, "Rename", rename,
                  problem=lambda n: pllib.name_problem(n, current=old))

    def _pl_delete(self):
        name = self._pl_name
        if name is None:
            return

        def go():
            self.safe(lambda: self.client.delete_playlist(name) or True)
            self._pl_name = None
            self.refresh_playlists()
        _confirm(self, "Delete Playlist?", f"“{name}” will be removed. This cannot be undone.",
                 "Delete", go)

    def save_queue_as_playlist(self):
        def save(name):
            def go(overwrite):
                if self.safe(lambda: self.client.save_playlist(name, overwrite=overwrite) or True):
                    self._last_playlist = name
                    self.show_toast(f"Saved queue as “{name}”")
                if self._pl_loaded:
                    self._pl_name = name
                    self.refresh_playlists()
            if pllib.is_overwrite(name, existing):
                _confirm(self, "Overwrite Playlist?", f"A playlist named “{name}” already exists.",
                         "Overwrite", lambda: go(True))
            else:
                go(False)
        existing = [n for n, _ in (self.safe(self.client.listplaylists) or [])]
        _ask_name(self, "Save Queue", "Enter a name for the playlist, or pick one to replace:",
                  self._last_playlist, "Save", save, problem=pllib.name_problem,
                  suggest=lambda typed: pllib.suggest(existing, typed),
                  note=lambda n: "This playlist exists: saving replaces it (you are asked first)."
                  if pllib.is_overwrite(n, existing) else "")

    def add_to_playlist(self, tracks):
        """"Add to Playlist": choose an existing playlist or name a new one."""
        uris = [t["file"] for t in tracks if t.get("file")]
        if not uris:
            return
        entries = self.safe(self.client.listplaylists)
        if entries is None:
            return
        names = [n for n, _ in pllib.sorted_playlists(entries)]
        n = len(uris)
        dialog = Adw.AlertDialog(heading="Add to Playlist",
                                 body=f"Add {n} track{'s' if n != 1 else ''} to:")
        choices = Gtk.StringList()
        choices.splice(0, 0, ["New playlist…"] + names)
        drop = Gtk.DropDown(model=choices)
        entry = Gtk.Entry(placeholder_text="Name of the new playlist", activates_default=True)
        hint = Gtk.Label(xalign=0, wrap=True)
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for w in (drop, entry, hint):
            box.append(w)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("ok", "Add")
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ok")
        dialog.set_close_response("cancel")

        def target():
            i = drop.get_selected()
            return entry.get_text() if i == 0 else names[i - 1]

        def check(*_):
            new = drop.get_selected() == 0
            entry.set_visible(new)
            why = pllib.name_problem(entry.get_text()) if new else None
            if why is None and new and entry.get_text() in names:
                hint.set_text("That playlist exists; the tracks will be added to its end")
                dialog.set_response_enabled("ok", True)
                return
            hint.set_text(why or "")
            dialog.set_response_enabled("ok", why is None)

        def respond(_d, response):
            if response != "ok":
                return
            name = target()
            if self.safe(lambda: self.client.playlist_add(name, uris) or True):
                self._last_playlist = name
                self.show_toast(f"Added {n} track{'s' if n != 1 else ''} to “{name}”")
                if self._pl_loaded:
                    self.refresh_playlists()
        if self._last_playlist in names:
            drop.set_selected(names.index(self._last_playlist) + 1)
        drop.connect("notify::selected", check)
        entry.connect("changed", check)
        dialog.connect("response", respond)
        check()
        dialog.present(self)

    def reset_playlists(self):
        self._pl_loaded = False
        self._playlists = []
        self._pl_refreshing = True
        try:
            self.pl_names.splice(0, self.pl_names.get_n_items(), [])
        finally:
            self._pl_refreshing = False
        self._show_playlist(None)


class _MprisPlayer:
    """What mpris.MprisServer asks for (see mpris.Player), answered by the window."""

    def __init__(self, win):
        self.w = win

    def status(self):
        return self.w.status

    def song(self):
        return self.w._current_song

    def position(self):
        return self.w.elapsed()

    def art_url(self):
        return self.w._art_url

    def play(self):
        w = self.w
        if w.status.get("state") == "pause":
            w.safe(w.client.command, "pause", "0")
        else:
            w.safe(w.client.play)

    def pause(self):
        if self.w.status.get("state") == "play":
            self.w.safe(self.w.client.command, "pause", "1")

    def play_pause(self):
        self.w.on_play_pause(None)

    def stop(self):
        self.w.safe(self.w.client.stop)

    def next(self):
        self.w.safe(self.w.client.next)

    def previous(self):
        self.w.safe(self.w.client.previous)

    def seek_to(self, seconds):
        self.w.seek_to(seconds, announce=False)  # the MPRIS server sends Seeked itself

    def set_volume(self, value):
        self.w.safe(self.w.client.setvol, value)

    def set_repeat(self, on):
        self.w.safe(self.w.client.set_repeat, on)

    def set_single(self, on):
        self.w.safe(self.w.client.set_single, on)

    def set_random(self, on):
        self.w.safe(self.w.client.set_random, on)

    def raise_window(self):
        self.w.show_window()

    def quit(self):
        self.w.request_quit()


class AlbumItem(GObject.Object):
    """An albums.AlbumEntry in a Gio.ListStore (plain attributes, like Song)."""
    __gtype_name__ = "ToccataAlbumItem"
    tile = None  # the AlbumTile showing it, if any

    def __init__(self, entry):
        super().__init__()
        self.entry = entry


ALBUM_TILE_SIZE = 128


class AlbumTile(Gtk.Box):
    def __init__(self, on_drag, on_button=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=6, margin_bottom=6,
                         margin_start=6, margin_end=6, width_request=ALBUM_TILE_SIZE)
        self.item = None
        self.add_css_class("album-tile")
        self.thumb = CoverThumb(ALBUM_TILE_SIZE)
        # Play / Add to Queue on the cover, shown only while the pointer is over the
        # tile (CSS .album-tile:hover; user request 2026-11-17, like the Artists view's
        # album headers). They act on this album only, not the selection. Opacity, not
        # visibility, as in the Artists view. With covers off there is no cover to sit
        # on: the overlay is hidden with the thumb.
        self.cover = Gtk.Overlay(child=self.thumb, halign=Gtk.Align.CENTER)
        buttons = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER, valign=Gtk.Align.END, margin_bottom=8)
        self.hover_buttons = []  # [Play, Add], left to right
        for icon, tip, how in (("media-playback-start-symbolic", "Play", "play"),
                               ("list-add-symbolic", "Add to queue", "add")):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip)
            for c in ("circular", "osd", "album-hover-btn"):
                b.add_css_class(c)
            b.connect("clicked", lambda _b, how=how: on_button and self.item is not None
                      and on_button(self.item, how))
            buttons.append(b)
            self.hover_buttons.append(b)
        self.cover.add_overlay(buttons)
        self.title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=16, lines=1)
        self.sub = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=16)
        self.sub.add_css_class("dim-label")
        self.sub.add_css_class("caption")
        for w in (self.cover, self.title, self.sub):
            self.append(w)
        src = DragSource(actions=Gdk.DragAction.COPY)
        src.connect("prepare", lambda _s, _x, _y: on_drag(self.item))
        self.add_controller(src)


class AlbumsPage:
    """Window mixin: the Albums view, a cover grid of every album. Logic and the
    MPD side (cheap queries only) are in albums.py."""

    def _build_albums_page(self):
        self._albums_all = None         # every album (sorting by artist/title/year), loaded once
        self._albums_recent = None      # albums.RecentAlbums walking "recently added"
        self._albums_loading = False
        self._albums_gen = 0
        self._albums_loaded = False     # filled since the last (re)connect / database change
        self._albums_matcher = Matcher([])
        self._albums_typeahead = TypeAhead()
        self.albums_store = Gio.ListStore(item_type=AlbumItem)
        sorts = [label for _k, label in albumslib.SORTS]
        self.albums_sort = Gtk.DropDown(model=Gtk.StringList.new(sorts), valign=Gtk.Align.CENTER,
                                        tooltip_text="Sort albums")
        keys = [k for k, _l in albumslib.SORTS]
        self.albums_sort.set_selected(keys.index(self.settings.albums_sort)
                                      if self.settings.albums_sort in keys else 0)
        self.albums_sort.connect("notify::selected", lambda *_: self._on_albums_sort())
        self.albums_count = Gtk.Label(xalign=0, hexpand=True)
        self.albums_count.add_css_class("dim-label")
        top = Gtk.Box(spacing=8, margin_top=6, margin_bottom=6, margin_start=12, margin_end=12)
        top.append(self.albums_sort)
        top.append(self.albums_count)
        self.albums_spinner = Gtk.Spinner(visible=False)
        top.append(self.albums_spinner)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", lambda _f, li: li.set_child(self._new_album_tile()))
        factory.connect("bind", self._bind_album_tile)
        factory.connect("unbind", self._unbind_album_tile)
        self.albums_sel = Gtk.MultiSelection(model=self.albums_store)
        self.albums_sel.connect("selection-changed", lambda *_: self._albums_selection_changed())
        self.albums_grid = Gtk.GridView(model=self.albums_sel, factory=factory, max_columns=40,
                                        enable_rubberband=False)
        self.albums_grid.connect("activate", lambda _g, pos: self._albums_act("add", [pos]))
        self.albums_scroll = Gtk.ScrolledWindow(vexpand=True, child=self.albums_grid)
        self.albums_scroll.get_vadjustment().connect("value-changed", lambda *_: self._albums_maybe_more())
        self.albums_scroll.get_vadjustment().connect("changed", lambda *_: self._albums_maybe_more())
        overlay, self.albums_typeahead_label = _typeahead_overlay(self.albums_scroll)
        self.albums_status = Adw.StatusPage(icon_name="media-optical-symbolic", title="Albums")
        self.albums_stack = Gtk.Stack(vexpand=True)
        self.albums_stack.add_named(overlay, "grid")
        self.albums_stack.add_named(self.albums_status, "status")
        self.albums_stack.add_named(_loading_box("Loading albums\u2026"), "loading")

        bar = Gtk.ActionBar()
        self.a_add = _text_button("Add to queue", lambda: self._albums_act("add"))
        self.a_play = _text_button("Play", lambda: self._albums_act("play"))
        more = Gio.Menu()
        more.append("Add to Playlist\u2026", "win.albums-to-playlist")
        more.append("Show in Artists", "win.albums-locate")
        more.append("Edit Tags\u2026", "win.albums-edit")
        self.a_more = Gtk.MenuButton(icon_name="view-more-symbolic", tooltip_text="More", menu_model=more)
        for name, how in (("albums-to-playlist", "playlist"), ("albums-locate", "locate"), ("albums-edit", "edit")):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a, how=how: self._albums_act(how))
            self.add_action(action)
        bar.pack_start(self.a_add)
        bar.pack_start(self.a_play)
        bar.pack_end(self.a_more)

        page = Adw.ToolbarView()
        page.add_top_bar(top)
        page.set_content(self.albums_stack)
        page.add_bottom_bar(bar)
        self.albums_page = page
        self._albums_selection_changed()
        return page

    def _new_album_tile(self):
        tile = AlbumTile(self._drag_albums, self._album_tile_button)
        _right_click(tile, lambda x, y, t=tile: self._album_tile_menu(t, x, y))
        return tile

    def _album_tile_pos(self, item):
        return next((i for i in range(self.albums_store.get_n_items()) if self.albums_store.get_item(i) is item),
                    None)

    def _album_tile_button(self, item, how):
        """A tile's hover Play / Add button: that album only; the selection stays."""
        pos = self._album_tile_pos(item)
        if pos is not None:
            self._albums_act(how, [pos])

    def _album_tile_menu(self, tile, x, y):
        item = tile.item
        if item is None:
            return
        pos = self._album_tile_pos(item)
        if pos is None:
            return
        if not self.albums_sel.is_selected(pos):
            _select_like_click(self.albums_grid, pos)  # right-click outside the selection selects that album
        _context_menu(tile, x, y, [
            ("Add to Queue", lambda: self._albums_act("add")),
            ("Play", lambda: self._albums_act("play")),
            ("Add to Playlist\u2026", lambda: self._albums_act("playlist")),
            ("Show in Artists", lambda: self._albums_act("locate")),
            None,
            ("Set Cover\u2026", lambda: self._albums_act("cover-online", [pos])),
            ("Edit Tags\u2026", lambda: self._albums_act("edit"))])

    def _albums_sort_key(self):
        return albumslib.SORTS[self.albums_sort.get_selected()][0]

    # -- loading
    def composer_genres_changed(self):
        """Other genres go under their composer: load the artist list again (the
        artist cache is dropped, as an artist's albums change too)."""
        for f in (self.library_fetcher, self.search_fetcher, self.album_fetcher):
            f.composer_genres = tuple(self.settings.composer_genres)
        if self.client.connected:
            self.reload_library(fresh=True)

    def prefixes_changed(self):
        """The ignored prefixes changed: sort artists and albums again."""
        if self.client.connected:
            self.reload_library(fresh=False)  # the artist names again (one small query), sorted anew
        if self._albums_loaded and self._albums_sort_key() != "added":
            self.reload_albums()  # resorts the album list it already has

    def ensure_albums(self):
        if not self._albums_loaded and self.client.connected:
            self.reload_albums()

    def invalidate_albums(self):
        """The database changed: load again next time (now, if the view is shown)."""
        self._albums_all = None
        self._albums_loaded = False
        if self.views.get_visible_child_name() == "albums":
            self.reload_albums()

    def reload_albums(self):
        self._albums_gen += 1
        self._albums_loaded = True
        self._albums_loading = False
        self.albums_store.remove_all()
        self._albums_entries = []
        self._albums_matcher = Matcher([])
        how = self._albums_sort_key()
        self._albums_upper_at_batch = None
        if how == "added":
            self._albums_recent = albumslib.RecentAlbums(albumslib.recent_field(self.client.protocol_version))
            self.albums_stack.set_visible_child_name("loading")
            self._albums_load_more()
        elif self._albums_all is not None:
            self._albums_show(albumslib.sort_albums(self._albums_all, how, self.settings.ignore_prefixes), done=True)
        else:
            gen = self._albums_gen
            self.albums_stack.set_visible_child_name("loading")
            self.album_fetcher.run(albumslib.fetch_all_albums,
                                   lambda res, err: self._albums_all_loaded(gen, res, err))

    def _albums_all_loaded(self, gen, entries, err):
        if gen != self._albums_gen:
            return
        if err is not None:
            self._albums_error(err)
            return
        self._albums_all = entries
        self._albums_show(albumslib.sort_albums(entries, self._albums_sort_key(), self.settings.ignore_prefixes),
                          done=True)

    def _albums_load_more(self):
        r = self._albums_recent
        if r is None or r.done or self._albums_loading:
            return
        self._albums_loading = True
        self.albums_spinner.set_visible(True)
        self.albums_spinner.start()
        gen = self._albums_gen
        self.album_fetcher.run(r.load_more, lambda res, err: self._albums_more_loaded(gen, res, err))

    def _albums_more_loaded(self, gen, entries, err):
        if gen != self._albums_gen:
            return
        self._albums_loading = False
        self.albums_spinner.stop()
        self.albums_spinner.set_visible(False)
        if err is not None:
            self._albums_error(err)
            return
        self._albums_show(entries or [], done=self._albums_recent.done, append=True)
        # the grid's "changed" (after layout) calls _albums_maybe_more to fill the screen

    def _albums_show(self, entries, done, append=False):
        if not append:
            self._albums_entries = []
            self.albums_store.remove_all()
        self._albums_entries = self._albums_entries + list(entries)
        self.albums_store.splice(self.albums_store.get_n_items(), 0, [AlbumItem(e) for e in entries])
        self._albums_matcher = Matcher([f"{e.album} {e.artist}" for e in self._albums_entries])
        n = len(self._albums_entries)
        if n:
            self.albums_stack.set_visible_child_name("grid")
        elif done:
            self.albums_status.set_title("No albums")
            self.albums_status.set_description("MPD's database has no albums.")
            self.albums_stack.set_visible_child_name("status")
        more = "" if done else "+"
        self.albums_count.set_text(f"{n}{more} album{'s' if n != 1 else ''}")
        if getattr(self, "_albums_pending", None) is not None and (n or done):
            self._albums_check_pending(done)

    def _albums_error(self, err):
        self.albums_status.set_title("Could not load albums")
        self.albums_status.set_description(describe_error(err, *self.cfg[:2]))
        self.albums_stack.set_visible_child_name("status")

    def _albums_maybe_more(self):
        """Recently added: fetch the next batch when the end of the grid is near.
        Gentle on MPD: one batch at a time, at most one per 0.5 s, and only after
        the grid has been laid out with the previous batch (otherwise the list
        still looks short and batch after batch would be fetched)."""
        if self._albums_recent is None or self._albums_recent.done or self._albums_loading:
            return
        if self.views.get_visible_child_name() != "albums" or self._albums_sort_key() != "added":
            return
        adj = self.albums_scroll.get_vadjustment()
        if adj.get_upper() == getattr(self, "_albums_upper_at_batch", None):
            return  # not laid out with the last batch yet; "changed" calls us again
        if time.monotonic() - getattr(self, "_albums_batch_time", 0.0) < 0.5:
            if not getattr(self, "_albums_retry", 0):
                self._albums_retry = GLib.timeout_add(500, self._albums_retry_more)
            return
        if adj.get_upper() - (adj.get_value() + adj.get_page_size()) < 2 * max(adj.get_page_size(), 300):
            self._albums_upper_at_batch = adj.get_upper()
            self._albums_batch_time = time.monotonic()
            self._albums_load_more()

    def _albums_retry_more(self):
        self._albums_retry = 0
        self._albums_maybe_more()
        return False

    def _on_albums_sort(self):
        key = self._albums_sort_key()
        if key != self.settings.albums_sort:
            self.settings.albums_sort = key
            self._schedule_flush()
        if self._albums_loaded:
            self.reload_albums()

    # -- tiles
    def _bind_album_tile(self, _factory, li):
        tile, item = li.get_child(), li.get_item()
        e = item.entry
        tile.item = item
        item.tile = tile
        tile.title.set_text(e.album)
        tile.sub.set_text(f"{e.artist} \u00b7 {e.year}" if e.year else e.artist)
        tile.set_tooltip_text(f"{e.artist} \u2013 {e.album}" + (f" ({e.year})" if e.year else ""))
        tile.thumb.set_texture(None)
        tile.thumb.set_visible(self.show_covers)
        tile.cover.set_visible(self.show_covers)
        if self.show_covers:
            self._album_tile_cover(item)

    def _unbind_album_tile(self, _factory, li):
        tile, item = li.get_child(), li.get_item()
        if item is not None and item.tile is tile:
            item.tile = None
        tile.item = None

    def _album_tile_cover(self, item):
        e = item.entry

        def show(tex, it=item):
            if it.tile is not None and it.tile.item is it:
                it.tile.thumb.set_texture(tex)
        if e.uri:
            self.cover_loader.request(e.uri, show, wanted=lambda it=item: it.tile is not None,
                                      song={"albumartist": e.artist, "album": e.album}, size=THUMB_SIZE)
            return
        # no song known yet (album list): one small lookup, only while the tile is on screen
        gen = self._albums_gen

        def found(uri, err, it=item):
            if gen != self._albums_gen or err is not None or not uri:
                return
            it.entry.uri = uri
            if it.tile is not None:
                self._album_tile_cover(it)
        self.album_fetcher.run(lambda c, e=e: albumslib.fetch_album_uri(c, e), found,
                               wanted=lambda it=item: it.tile is not None)

    # -- selection and actions
    def _albums_selected_positions(self):
        bits = self.albums_sel.get_selection()
        return [bits.get_nth(i) for i in range(bits.get_size())]

    def _albums_selection_changed(self):
        n = len(self._albums_selected_positions())
        for w in (self.a_add, self.a_play, self.a_more):
            w.set_sensitive(n > 0)
        self.a_add.set_label(f"Add {n} albums to queue" if n > 1 else "Add to queue")
        self.a_play.set_label(f"Play {n} albums" if n > 1 else "Play")

    def _albums_act(self, how, positions=None):
        """Fetch the tracks of the selected albums (in the background), then act."""
        positions = positions if positions is not None else self._albums_selected_positions()
        entries = [self.albums_store.get_item(p).entry for p in positions if p < self.albums_store.get_n_items()]
        if not entries:
            return
        if how == "locate":
            self.locate_album(entries[0])
            return

        def fetch(client):
            return [t for e in entries for t in albumslib.fetch_album_tracks(client, e)]

        def done(tracks, err):
            if err is not None:
                self.show_toast(describe_error(err, *self.cfg[:2]))
                return
            if not tracks:
                self.show_toast("No tracks found for that album")
            elif how == "add":
                self.add_tracks(tracks, entries[0].album if len(entries) == 1 else None)
            elif how == "play":
                self.play_tracks(tracks)
            elif how == "playlist":
                self.add_to_playlist(tracks)
            elif how == "edit":
                self.edit_tracks(tracks)
            elif how == "cover-online":
                self.search_cover_online(tracks, entries[0].album)
        self.album_fetcher.run(fetch, done)

    def _drag_albums(self, item):
        if item is None:
            return None
        positions = self._albums_selected_positions()
        entries = [self.albums_store.get_item(p).entry for p in positions]
        if item.entry not in entries:
            entries = [item.entry]
        self._drag = ("albums", entries)
        return self._tag(drag_label([e.album for e in entries], "album"))

    def insert_albums(self, entries, at):
        """Drop of album tiles on the queue: fetch their tracks, then insert them."""
        def fetch(client):
            return [t for e in entries for t in albumslib.fetch_album_tracks(client, e)]
        self.album_fetcher.run(fetch, lambda tracks, err: self._insert_tracks(tracks, at) if err is None
                               else self.show_toast(describe_error(err, *self.cfg[:2])))

    def locate_album(self, entry):
        """Show the album in the Artists view (its artist, the album selected)."""
        i = self._artist_index(entry.artist)
        if i is not None:
            self._pending_album = entry.album
            self.views.set_visible_child_name("library")
            if self.art_sel.get_selected() == i and self._shown_artist is not None:
                self._select_pending_album()
            else:
                self.art_sel.set_selected(i)  # loads the artist; _show_artist selects the album
            GLib.timeout_add(300, self._scroll_to_artist, i)
            return
        self.show_toast(f"\u201c{entry.artist}\u201d is not in the artist list (genre filter?)")

    def _select_pending_album(self):
        title = getattr(self, "_pending_album", None)
        quiet, self._pending_album_quiet = getattr(self, "_pending_album_quiet", False), False
        self._pending_album = None
        if not title:
            return
        for row in self._album_rows():
            if row.album.title == title:
                self._set_album_selection([row], row)
                self._scroll_album_into_view(row)
                if not quiet:  # after a database update: leave the focus where it is
                    (getattr(row, "header", None) or row).grab_focus()
                return

    # -- type-ahead
    def _jump_to_album_tile(self, text, step=False):
        bits = self.albums_sel.get_selection()
        current = bits.get_nth(0) if bits.get_size() else -1
        idx = self._albums_matcher.find(text, current if step else -1)
        if idx is None:
            return False
        self.albums_grid.scroll_to(idx, Gtk.ListScrollFlags.FOCUS | Gtk.ListScrollFlags.SELECT, None)
        return True


class Window(SearchPage, PlaylistsPage, AlbumsPage, Adw.ApplicationWindow):
    def __init__(self, app, settings, cli, show_covers=True, highlight=True):
        super().__init__(application=app, title="Toccata",
                         default_width=settings.window_width or 1200,
                         default_height=settings.window_height or 700)
        self.settings = settings  # what is saved on disk
        self.cli = cli            # explicit command-line values (override saved ones)
        self._recompute_effective()
        self.client = self._new_client()
        self.status = {}
        self._elapsed_at = (0.0, time.monotonic())
        self._seeking = False
        self._seek_source = None   # pending GLib timeout that sends the seek
        self._seek_target = 0.0
        self._cover_key = None
        self._idle = None
        self._now_marker = NowPlayingMarker()  # which queue entry is highlighted
        self._queue_by_id = {}
        self._server_dialog = None  # the open Outputs/Partitions/Collections dialog
        self._lib_gen = 0            # bumped per (re)load of the artist list; older answers are ignored
        self._album_gen = 0          # bumped per artist shown; stale album and cover answers are ignored
        self._selected_artist = settings.last_artist or None  # shown again on start
        self._genre = settings.genre or None  # library genre filter (None = All Genres)
        self._genres = []                     # genres offered in the filter, sorted
        self._shown_artist = None    # Artist whose albums are on screen
        self._updating_artists = False
        self._flush_scheduled = False
        self._window_memory = WindowMemory()
        self._divider_restore = 0    # tick callback restoring dividers after (un)maximize
        self._closing = False
        self.library_fetcher = self._new_library_fetcher()
        self.search_fetcher = self._new_library_fetcher()  # own connection: a slow search never delays the library
        self.album_fetcher = self._new_library_fetcher()   # Albums view: its queries never delay the others
        self.show_covers = show_covers
        self.highlight = highlight
        self._queue_songs = []
        self.queue_history = QueueHistory()  # undo / redo of queue changes made here
        self._drag = None  # what is being dragged: (kind, data); the drag itself only carries a tag
        self.cover_loader = self._new_cover_loader()
        self.player_cover_loader = self._new_player_cover_loader()
        self.connect("close-request", self._on_close_request)
        prefs = Gio.SimpleAction.new("preferences", None)
        prefs.connect("activate", lambda *_: self.open_settings())
        self.add_action(prefs)
        grouped = Gio.SimpleAction.new_stateful("queue-grouped", None,
                                                GLib.Variant.new_boolean(settings.queue_grouped))
        grouped.connect("change-state", lambda a, v: self._set_grouped(v.get_boolean()))
        self.add_action(grouped)
        compact = Gio.SimpleAction.new_stateful("queue-compact", None,
                                                GLib.Variant.new_boolean(settings.queue_compact))
        compact.connect("change-state", lambda a, v: self._set_compact(v.get_boolean()))
        self.add_action(compact)
        self.queue_compact = settings.queue_compact  # read by the queue rows' factory
        for name, closed in (("queue-collapse-all", True), ("queue-expand-all", False)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a, closed=closed: self.collapse_all(closed))
            self.add_action(action)
        for name, redo in (("queue-undo", False), ("queue-redo", True)):  # Ctrl+Z, Ctrl+Shift+Z / Ctrl+Y
            action = Gio.SimpleAction.new(name, None)
            action.set_enabled(False)
            action.connect("activate", lambda *_a, redo=redo: self.undo_queue(redo))
            self.add_action(action)
        center = Gio.SimpleAction.new("queue-center", None)  # Ctrl+L: scroll the queue to the playing track
        center.connect("activate", lambda *_: self.center_queue_on_playing())
        self.add_action(center)
        qfind = Gio.SimpleAction.new("queue-filter", None)  # Ctrl+Shift+F: "Find in Play Queue"
        qfind.connect("activate", lambda *_: self.open_queue_filter())
        self.add_action(qfind)
        genre = Gio.SimpleAction.new("genre", None)  # Ctrl+G: "Filter On Genre"
        genre.connect("activate", lambda *_: self.focus_genre_filter())
        self.add_action(genre)
        find = Gio.SimpleAction.new("search", None)  # Ctrl+F: find artist (in Search: its entry)
        find.connect("activate", lambda *_: self.focus_search() if self.views.get_visible_child_name() == "search"
                     else self.open_artist_filter())
        self.add_action(find)
        for page in ("outputs", "partitions", "collections"):
            action = Gio.SimpleAction.new(page, None)
            action.connect("activate", lambda *_, page=page: self.open_server_dialog(page))
            self.add_action(action)
        info = Gio.SimpleAction.new("server-info", None)
        info.connect("activate", lambda *_: self.open_server_dialog("info"))
        self.add_action(info)
        self._qfilter = queuefilter.parse("")  # "Find in Play Queue" (Ctrl+Shift+F)
        self._all_artists = []      # every artist (of the genre); artist_store holds the shown ones
        self._artist_filter = ""    # text of the artist filter bar (Ctrl+F)
        self._current_song = {}   # MPD currentsong (for MPRIS and the tray)
        self._art_url = ""        # file:// URL of the current cover, for MPRIS
        self._quitting = False
        self.tray = None
        self._build_ui()
        self.mpris = mprislib.MprisServer(_MprisPlayer(self))
        self.mpris.start()
        if settings.tray_icon:
            self.start_tray()
        # Type-ahead in the library. Capture phase: the artist list itself takes the space
        # key (it selects the focused row), so a bubble-phase handler never saw spaces.
        # Text fields are skipped explicitly in _on_library_key.
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_library_key)
        self.add_controller(keys)
        self._update_title()
        self._connect_and_start()
        GLib.timeout_add(500, self._tick)
        if settings.window_maximized:
            self.maximize()
        GLib.timeout_add_seconds(1, self._poll_window)  # remembers size and maximized state
        self.connect("notify::maximized", self._on_maximized_changed)

    def _recompute_effective(self):
        self.eff, self.overrides = effective(self.settings, **self.cli)
        self.cfg = self.eff.connection()
        self.partition = self.eff.partition or None  # MPD partition this window works in
        self.music_dir = self.eff.music_dir or None

    def _new_cover_loader(self):
        """List covers (albums, queue headers): thumbnails, cached on disk."""
        return CoverFetcher(MPDClient(*self.cfg), _decode_texture, _on_main_thread,
                            music_dir=lambda: self.music_dir, thumbnail=_thumbnail_png,
                            disk_dir=os.path.join(GLib.get_user_cache_dir(), "toccata", "covers"))

    def _new_player_cover_loader(self):
        """The player bar's cover, full size, on its own connection: it never waits
        behind the covers of an artist with hundreds of albums."""
        return CoverFetcher(MPDClient(*self.cfg), _decode_texture, _on_main_thread, cache_size=8,
                            music_dir=lambda: self.music_dir)

    def open_settings(self):
        SettingsDialog(self).present(self)

    # -- UI ------------------------------------------------------------------
    def _build_ui(self):
        self.toast = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        self.toast.set_child(toolbar)
        self.set_content(self.toast)

        header = Adw.HeaderBar()
        self.title_widget = Adw.WindowTitle(title="Toccata", subtitle="")
        header.set_title_widget(self.title_widget)
        menu = Gio.Menu()
        server = Gio.Menu()
        server.append("Outputs\u2026", "win.outputs")
        server.append("Partitions\u2026", "win.partitions")
        server.append("Collections\u2026", "win.collections")
        server.append("Server Information\u2026", "win.server-info")
        menu.append_section(None, server)
        rest = Gio.Menu()
        rest.append("Preferences", "win.preferences")
        rest.append("Quit", "app.quit")
        menu.append_section(None, rest)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                       tooltip_text="Main menu"))
        # Info view (like Cantata's): artist / album / lyrics of the song in use, instead
        # of library and queue while it is on
        self.info_btn = Gtk.ToggleButton(icon_name="dialog-information-symbolic",
                                         tooltip_text="Information about the playing song (Ctrl+I)")
        self.info_btn.connect("toggled", lambda b: self.set_info_visible(b.get_active()))
        header.pack_end(self.info_btn)
        info_action = Gio.SimpleAction.new("info", None)
        info_action.connect("activate", lambda *_: self.info_btn.set_active(not self.info_btn.get_active()))
        self.add_action(info_action)
        toolbar.add_top_bar(header)

        # Queue page
        # store: Songs, and when grouped a QueueAlbum header item before each album
        # -> filter (hides tracks of collapsed albums, filter misses and their headers)
        self.queue_store = Gio.ListStore(item_type=GObject.Object)
        self._db_gate = DbReloadGate()  # library reloads wait for MPD's database update
        self._queue_rows = []      # what queue_store holds, in order
        self._album_items = {}     # head id -> QueueAlbum, reused across refreshes
        self._queue_filter = Gtk.CustomFilter.new(self._song_visible)
        filtered = Gtk.FilterListModel(model=self.queue_store, filter=self._queue_filter)
        self.collapsed_groups = set()   # head ids of collapsed albums
        self._queue_groups = []
        queue_view = make_queue_list(filtered, self,
                                     [("list-remove-symbolic", "Remove from queue", self.remove_from_queue)])
        self.queue_view = queue_view
        self._queue_select_after = None
        qkeys = Gtk.EventControllerKey()
        qkeys.connect("key-pressed", self._on_queue_key)
        queue_view.add_controller(qkeys)
        drop = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        drop.connect("drop", self._on_queue_drop)
        drop.connect("motion", self._on_queue_drag_motion)
        drop.connect("leave", lambda *_: self._queue_drop_line.set_visible(False))
        queue_view.add_controller(drop)
        self.queue_sel = queue_view.get_model()
        self._fixing_selection = False
        self.queue_sel.connect("selection-changed", self._drop_header_selection)
        qscroll = Gtk.ScrolledWindow(vexpand=True, min_content_width=300, child=queue_view)
        self._pin_queue_top(qscroll.get_vadjustment())
        _watch_user_scroll(qscroll, lambda: getattr(self, "_queue_pin", None))
        qpage = Adw.ToolbarView()
        qbar = Gtk.ActionBar()
        clear = Gtk.Button(label="Clear queue")
        clear.connect("clicked", lambda _b: self.change_queue(self.client.clear))
        qbar.pack_start(clear)
        save_pl = Gtk.Button(label="Save as playlist…", tooltip_text="Save the queue as a stored playlist")
        save_pl.connect("clicked", lambda _b: self.save_queue_as_playlist())
        qbar.pack_start(save_pl)
        qpage.add_bottom_bar(qbar)
        # the line that shows where a drag would land (Cantata draws one too)
        self._queue_drop_line = Gtk.Box(valign=Gtk.Align.START, can_target=False, visible=False)
        self._queue_drop_line.add_css_class("queue-drop-line")
        qoverlay = Gtk.Overlay(child=qscroll)
        qoverlay.add_overlay(self._queue_drop_line)
        self._queue_overlay = qoverlay
        # click and drag from the empty space below the last row selects rows
        self._queue_band = RubberBand(qscroll, qoverlay, lambda w: w is queue_view or w is qscroll,
                                      self._queue_band_start, self._queue_band_moved,
                                      lambda: getattr(self, "_queue_pin", None),
                                      on_click=self._queue_empty_click)
        qpage.set_content(qoverlay)
        # "Find in Play Queue": a search bar above the queue (Ctrl+Shift+F)
        self.qfilter_entry = Gtk.SearchEntry(hexpand=True, placeholder_text="Filter the queue\u2026")
        self.qfilter_entry.set_tooltip_text("Words must all appear in title, artist, album or composer; "
                                            "#1999 or #1990-1999 for years")
        self.qfilter_count = Gtk.Label()
        self.qfilter_count.add_css_class("dim-label")
        self.qfilter_count.add_css_class("numeric")
        fbox = Gtk.Box(spacing=8)
        fbox.append(self.qfilter_entry)
        fbox.append(self.qfilter_count)
        self.qfilter_bar = Gtk.SearchBar(child=fbox, show_close_button=True)
        self.qfilter_bar.connect_entry(self.qfilter_entry)
        self.qfilter_entry.connect("search-changed", lambda e: self._set_queue_filter(e.get_text()))
        self.qfilter_entry.connect("stop-search", lambda _e: self.qfilter_bar.set_search_mode(False))
        self.qfilter_bar.connect("notify::search-mode-enabled", self._on_qfilter_mode)
        qpage.add_top_bar(self.qfilter_bar)
        queue_pane = self._titled_pane("Queue", qpage)
        qmenu = Gio.Menu()
        qmenu.append("Group Albums", "win.queue-grouped")
        qmenu.append("Compact Rows", "win.queue-compact")
        fold = Gio.Menu()
        fold.append("Collapse All Albums", "win.queue-collapse-all")
        fold.append("Expand All Albums", "win.queue-expand-all")
        qmenu.append_section(None, fold)
        qmenu_button = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=qmenu,
                                      tooltip_text="Queue options", valign=Gtk.Align.CENTER)
        qmenu_button.add_css_class("flat")
        self.qfilter_toggle = Gtk.ToggleButton(icon_name="system-search-symbolic", valign=Gtk.Align.CENTER,
                                               tooltip_text="Find in queue (Ctrl+Shift+F)")
        self.qfilter_toggle.add_css_class("flat")
        self.qfilter_toggle.bind_property("active", self.qfilter_bar, "search-mode-enabled",
                                          GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
        qcenter = Gtk.Button(icon_name="find-location-symbolic", valign=Gtk.Align.CENTER,
                             action_name="win.queue-center", tooltip_text="Show the playing track (Ctrl+L)")
        qcenter.add_css_class("flat")
        queue_pane.title_box.append(qcenter)
        queue_pane.title_box.append(self.qfilter_toggle)
        queue_pane.title_box.append(qmenu_button)
        self._set_grouped(self.settings.queue_grouped, save=False)
        self._set_compact(self.settings.queue_compact, save=False)

        # Library: artists on the left, the selected artist's albums on the right.
        self.artist_store = Gtk.StringList()
        art_sel = Gtk.SingleSelection(model=self.artist_store)
        art_factory = Gtk.SignalListItemFactory()
        art_factory.connect("setup", self._setup_artist_row)
        art_factory.connect("bind", self._bind_artist_row)
        art_view = Gtk.ListView(model=art_sel, factory=art_factory)
        art_view.add_css_class("navigation-sidebar")
        art_sel.connect("selection-changed", self.on_artist_selected)
        self.art_sel = art_sel

        self.art_view = art_view
        # a scroll to the selected artist that came while the list was not shown
        # (another view, minimised) is done once it is mapped again
        self._artist_scroll_pending = False
        art_view.connect("map", self._on_art_view_map)
        self._debug_art_until = 0.0  # TOCCATA_DEBUG: log artist-list scrolling until then
        self.artist_stack = Gtk.Stack()
        # Type-ahead: typing in the library jumps to the matching artist; the typed text
        # is shown at the bottom of the artist list.
        self._typeahead = TypeAhead()
        self._artist_matcher = Matcher([])
        self._typeahead_hide = 0
        self._album_typeahead = TypeAhead()   # the album list has its own (focus decides which)
        self._album_matcher = Matcher([])
        self._album_typeahead_hide = 0
        artists_overlay = Gtk.Overlay(child=Gtk.ScrolledWindow(min_content_width=150, child=art_view))
        self.typeahead_label = Gtk.Label(halign=Gtk.Align.CENTER, valign=Gtk.Align.END, margin_bottom=12,
                                         visible=False, can_target=False, ellipsize=Pango.EllipsizeMode.END)
        self.typeahead_label.add_css_class("osd")
        self.typeahead_label.add_css_class("typeahead")
        artists_overlay.add_overlay(self.typeahead_label)
        self.artist_stack.add_named(artists_overlay, "list")
        self.artist_stack.add_named(_loading_box("Loading artists\u2026"), "loading")
        self.artist_stack.set_visible_child_name("loading")

        self.album_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, valign=Gtk.Align.START,
                                      margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        self.album_list.add_css_class("album-list")  # flat: no boxed-list card background
        # Selection is done by hand (ExpanderRows do not show ListBox selection):
        # Ctrl+click toggles an album, Shift+click selects a range, a plain click clears it.
        self._sel_albums, self._sel_anchor = [], None
        self._pending_click = None  # row whose plain click waits for the button release
        self._sel_tracks, self._track_anchor = [], None  # track rows selected inside opened albums
        # Blank space below the albums, only while it is needed: collapsing an album near
        # the end of the list would otherwise make GTK scroll up, and the list jumped.
        self._album_spacer = Gtk.Box()
        albums_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        # Positions in the album list are measured against this box (the scrolled
        # content), not against the ScrolledWindow's Viewport, which moves with scrolling.
        self._albums_box = albums_box
        albums_box.append(self.album_list)
        albums_box.append(self._album_spacer)
        self.album_scroll = Gtk.ScrolledWindow(vexpand=True, min_content_width=300, child=albums_box)
        # No "scroll to focus": with the focus on a button inside an open album (e.g. a
        # track's "+"), GTK kept scrolling back to it, and moved the view when it was hidden.
        self.album_scroll.get_child().set_scroll_to_focus(False)
        self.album_scroll.get_vadjustment().connect("value-changed", lambda _a: self._trim_album_spacer())
        self.album_scroll.get_vadjustment().connect("value-changed", self._schedule_album_covers)
        self.album_scroll.get_vadjustment().connect("changed", self._schedule_album_covers)
        # A click anywhere in the album pane (also on empty space) gives it the keyboard
        # focus, so type-ahead searches the albums.
        pane_click = Gtk.GestureClick()
        pane_click.connect("pressed", lambda *_: self.focus_album_list())
        self.album_scroll.add_controller(pane_click)
        self.lib_status = Adw.StatusPage(icon_name="folder-music-symbolic")
        self.album_stack = Gtk.Stack()
        album_overlay = Gtk.Overlay(child=self.album_scroll)
        self.album_typeahead_label = Gtk.Label(halign=Gtk.Align.CENTER, valign=Gtk.Align.END, margin_bottom=12,
                                               visible=False, can_target=False,
                                               ellipsize=Pango.EllipsizeMode.END)
        self.album_typeahead_label.add_css_class("osd")
        self.album_typeahead_label.add_css_class("typeahead")
        album_overlay.add_overlay(self.album_typeahead_label)
        # click and drag from empty space (margins, below the last album) selects albums,
        # or the tracks of opened albums when it touches no album header
        self._album_band = RubberBand(
            self.album_scroll, album_overlay,
            lambda w: w in (self.album_scroll, self.album_scroll.get_child(), albums_box,
                            self.album_list, self._album_spacer),
            self._album_band_start, self._album_band_moved,
            on_click=self._album_empty_click)
        self.album_stack.add_named(album_overlay, "albums")
        self.album_stack.add_named(self.lib_status, "status")
        self.album_stack.add_named(_loading_box("Loading albums\u2026"), "loading")
        self.album_stack.set_visible_child_name("loading")

        self.add_all = add_all = Gtk.Button(label="Add all to queue")
        add_all.connect("clicked", self.on_add_all)
        lbar = Gtk.ActionBar()
        lbar.pack_start(add_all)
        self.edit_all = edit_all = Gtk.Button(label="Edit tags for all")
        edit_all.connect("clicked", self.on_edit_all)
        lbar.pack_end(edit_all)
        to_pl = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add the selected albums (or all) to a playlist")
        to_pl.connect("clicked", lambda _b: self.add_to_playlist(self._library_targets()))
        lbar.pack_end(to_pl)
        lcontent = Adw.ToolbarView()
        lcontent.set_content(self.album_stack)
        lcontent.add_bottom_bar(lbar)

        # Library = artists | albums. Both panes are resizable and cannot
        # collapse below a usable width.
        library_split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL,
                                  position=divider_position(self.settings, "artist_split", self.settings.window_maximized, 190),
                                  resize_start_child=False, resize_end_child=True,
                                  shrink_start_child=False, shrink_end_child=False)
        self.library_split = library_split
        self.genre_model = Gtk.StringList.new(["All Genres"])
        self.genre_drop = Gtk.DropDown(model=self.genre_model, enable_search=True,
                                       expression=Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"),
                                       tooltip_text="Show only artists and albums of one genre (Ctrl+G)",
                                       margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        self._filling_genres = False
        self.genre_drop.connect("notify::selected", self._on_genre_selected)
        self.genre_drop.set_sensitive(False)  # until the genres are loaded
        self.genre_drop.set_hexpand(True)
        # "Find artist": a search bar above the artist list (Ctrl+F), the same rule as the
        # queue filter (queuefilter.filter_names). Closing it shows every artist again.
        self.afilter_entry = Gtk.SearchEntry(hexpand=True, placeholder_text="Filter artists\u2026")
        self.afilter_entry.set_tooltip_text("Words must all appear in the artist's name")
        self.afilter_count = Gtk.Label()
        self.afilter_count.add_css_class("dim-label")
        self.afilter_count.add_css_class("numeric")
        abox = Gtk.Box(spacing=8)
        abox.append(self.afilter_entry)
        abox.append(self.afilter_count)
        self.afilter_bar = Gtk.SearchBar(child=abox)  # no ✕: the pane is narrow; the toggle and Escape close it
        self.afilter_bar.connect_entry(self.afilter_entry)
        self.afilter_entry.connect("search-changed", lambda e: self._set_artist_filter(e.get_text()))
        self.afilter_entry.connect("stop-search", lambda _e: self.afilter_bar.set_search_mode(False))
        self.afilter_entry.connect("activate", lambda _e: self._focus_artist_list())
        self.afilter_bar.connect("notify::search-mode-enabled", self._on_afilter_mode)
        self.afilter_toggle = Gtk.ToggleButton(icon_name="system-search-symbolic", valign=Gtk.Align.CENTER,
                                               margin_end=6, tooltip_text="Find artist (Ctrl+F)")
        self.afilter_toggle.add_css_class("flat")
        self.afilter_toggle.bind_property("active", self.afilter_bar, "search-mode-enabled",
                                          GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
        genre_row = Gtk.Box()
        genre_row.append(self.genre_drop)
        genre_row.append(self.afilter_toggle)
        artists_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        artists_box.append(genre_row)
        artists_box.append(self.afilter_bar)
        self.artist_stack.set_vexpand(True)
        artists_box.append(self.artist_stack)
        library_split.set_start_child(artists_box)
        library_split.set_end_child(lcontent)
        # Left pane: Library / Playlists / Search.
        self.views = Adw.ViewStack(vexpand=True)
        self.views.add_titled_with_icon(library_split, "library", "Artists", "system-users-symbolic")
        self.views.add_titled_with_icon(self._build_albums_page(), "albums", "Albums", "media-optical-symbolic")
        self.views.add_titled_with_icon(self._build_playlists_page(), "playlists", "Playlists",
                                        "view-list-symbolic")
        self.views.add_titled_with_icon(self._build_search_page(), "search", "Search",
                                        "system-search-symbolic")
        if self.settings.view in ("library", "albums", "playlists", "search"):
            self.views.set_visible_child_name(self.settings.view)
        self.views.connect("notify::visible-child-name", self._on_view_changed)
        library_pane = Adw.ToolbarView()
        switcher = Adw.ViewSwitcher(stack=self.views, policy=Adw.ViewSwitcherPolicy.WIDE,
                                    margin_top=4, margin_bottom=4, margin_start=6, margin_end=6)
        library_pane.add_top_bar(switcher)
        library_pane.set_content(self.views)

        # Main layout, a split view: library left, queue right.
        main_split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL,
                               position=divider_position(self.settings, "main_split", self.settings.window_maximized, 640),
                               resize_start_child=True, resize_end_child=True,
                               shrink_start_child=False, shrink_end_child=False,
                               wide_handle=True)
        self.main_split = main_split
        self._watch_divider(main_split, "main_split")
        self._watch_divider(library_split, "artist_split")
        main_split.set_start_child(library_pane)
        main_split.set_end_child(queue_pane)
        self.info_view = InfoView(self, CoverArt(240))
        self.main_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.main_stack.add_named(main_split, "main")
        self.main_stack.add_named(self.info_view, "info")
        toolbar.set_content(self.main_stack)
        toolbar.add_bottom_bar(self._build_player_bar())

    def _on_view_changed(self, *_):
        name = self.views.get_visible_child_name()
        if name == "playlists":
            self.ensure_playlists()
        elif name == "albums":
            self.ensure_albums()
        elif name == "search":
            self.search_entry.grab_focus()
        if name and name != self.settings.view:
            self.settings.view = name
            self._schedule_flush()

    def _library_targets(self):
        tracks = self._selected_album_tracks()
        if tracks is None and self._shown_artist is not None:
            tracks = self._shown_artist.tracks()
        return tracks or []

    def _watch_divider(self, paned, field):
        """Remember where the user drags a divider (see UserDragFilter)."""
        drag = UserDragFilter()
        paned.connect("notify::position", lambda p, _spec: self._on_divider(p, field, drag))

    def _on_divider(self, paned, field, drag):
        if not drag.is_user_change((self.get_width(), self.get_height())):
            return  # GTK moved it (first layout or window resize), not the user
        if self._divider_restore:
            return  # the window state just changed; _restore_dividers sets the positions
        key = divider_key(field, self.is_maximized())
        position = paned.get_position()
        if position > 0 and position != getattr(self.settings, key):
            setattr(self.settings, key, position)
            self._schedule_flush()

    _DIVIDERS = (("main_split", "main_split", 640), ("library_split", "artist_split", 190))

    def _on_maximized_changed(self, *_):
        """Maximizing / un-maximizing switches to that state's remembered divider
        positions. They are set once the window has its new size: setting them
        earlier would let GTK's resize move them again."""
        if self._divider_restore:
            self.remove_tick_callback(self._divider_restore)
        size = (self.get_width(), self.get_height())
        frames = [0]

        def tick(_widget, _clock):
            frames[0] += 1
            if (self.get_width(), self.get_height()) == size and frames[0] < 30:
                return GLib.SOURCE_CONTINUE  # not resized yet (30 frames: give up waiting)
            self._divider_restore = 0
            maximized = self.is_maximized()
            for paned, field, default in self._DIVIDERS:
                getattr(self, paned).set_position(divider_position(self.settings, field, maximized, default))
            return GLib.SOURCE_REMOVE
        self._divider_restore = self.add_tick_callback(tick)

    @staticmethod
    def _titled_pane(title, content):
        pane = Adw.ToolbarView()
        label = Gtk.Label(label=title, xalign=0, hexpand=True, margin_top=8, margin_bottom=8,
                          margin_start=12, margin_end=12)
        label.add_css_class("heading")
        box = Gtk.Box(margin_end=6)
        box.append(label)
        pane.add_top_bar(box)
        pane.title_box = box
        pane.set_content(content)
        return pane

    def _build_player_bar(self):
        # Cover on the left; title, seek bar and buttons stacked next to it.
        bar = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=12, margin_end=12)
        self.player_bar = bar
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True,
                         valign=Gtk.Align.CENTER)
        bar.add_css_class("toolbar")
        self.now_title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.now_title.add_css_class("heading")
        self.now_sub = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.now_sub.add_css_class("dim-label")

        self.cover = CoverThumb(COVER_SIZE)
        self.cover.set_visible(self.show_covers)
        # Hovering the cover shows it big (whole image, uncropped) above the player bar.
        self._cover_texture = None
        self._preview_timer = 0
        self.cover_preview_art = PreviewArt()
        self.cover_preview = Gtk.Popover(autohide=False, has_arrow=True, position=Gtk.PositionType.TOP,
                                         can_target=False, child=self.cover_preview_art)
        self.cover_preview.add_css_class("cover-preview")
        self.cover_preview.set_parent(self.cover)
        hover = Gtk.EventControllerMotion()
        hover.connect("enter", lambda *_: self._schedule_cover_preview())
        hover.connect("leave", lambda *_: self._hide_cover_preview())
        self.cover.add_controller(hover)
        self.cover.set_tooltip_text(None)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                       hexpand=True, valign=Gtk.Align.CENTER)
        text.append(self.now_title)
        text.append(self.now_sub)
        # Shown while MPD rescans its database (status `updating_db`).
        self.db_update = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER, visible=False)
        self.db_update.add_css_class("dim-label")
        spinner_cls = getattr(Adw, "Spinner", None)  # libadwaita >= 1.6; Gtk.Spinner before
        self.db_update_spinner = spinner_cls() if spinner_cls else Gtk.Spinner()
        self.db_update_label = Gtk.Label()
        self.db_update_label.add_css_class("caption")
        self.db_update.append(self.db_update_spinner)
        self.db_update.append(self.db_update_label)
        info = Gtk.Box(spacing=12)
        info.append(text)
        info.append(self.db_update)
        self.cover.set_valign(Gtk.Align.CENTER)
        bar.append(self.cover)
        bar.append(column)
        column.append(info)

        self.seek = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 1)
        self.seek.set_draw_value(False)
        self.seek.connect("change-value", self.on_seek)
        column.append(self.seek)

        row = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        self.time_lbl = Gtk.Label(label="0:00 / 0:00")
        self.time_lbl.add_css_class("numeric")
        prev = Gtk.Button(icon_name="media-skip-backward-symbolic")
        prev.connect("clicked", lambda _b: self.safe(self.client.previous))
        self.play_btn = Gtk.Button(icon_name="media-playback-start-symbolic")
        self.play_btn.add_css_class("circular")
        self.play_btn.add_css_class("suggested-action")
        self.play_btn.connect("clicked", self.on_play_pause)
        nxt = Gtk.Button(icon_name="media-skip-forward-symbolic")
        nxt.connect("clicked", lambda _b: self.safe(self.client.next))
        stop = Gtk.Button(icon_name="media-playback-stop-symbolic")
        stop.connect("clicked", lambda _b: self.safe(self.client.stop))
        self.vol = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.vol.set_draw_value(False)
        self.vol.set_size_request(120, -1)
        self.vol.connect("value-changed", self.on_volume)
        for w in (prev, self.play_btn, nxt, stop, self.time_lbl, Gtk.Image(icon_name="audio-volume-high-symbolic"), self.vol):
            row.append(w)
        # Cantata's play options: MPD's repeat, single, random (shuffle) and consume
        row.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL, margin_start=6, margin_end=6))
        self._syncing_options = False
        self.option_buttons = {}
        for name, icon, tip in (
                ("repeat", "media-playlist-repeat-symbolic", "Repeat: start the queue again after the last song"),
                ("single", "media-playlist-repeat-song-symbolic",
                 "Single: stop after the current song (with Repeat: repeat it)"),
                ("random", "media-playlist-shuffle-symbolic", "Shuffle: play the queue in random order"),
                ("consume", "io.github.toccata-consume-symbolic", "Consume: remove each song from the queue once played")):
            b = Gtk.ToggleButton(icon_name=icon, tooltip_text=tip, valign=Gtk.Align.CENTER)
            b.add_css_class("flat")
            b.connect("toggled", self._on_option_toggled, name)
            self.option_buttons[name] = b
            row.append(b)
        column.append(row)
        return bar

    # -- MPD plumbing -----------------------------------------------------------
    def _connect_and_start(self):
        host, port, _pw = self.cfg
        try:
            self.client.connect()
        except (OSError, MPDError) as e:
            self.client.close()
            self.now_title.set_text("Not connected")
            message = describe_error(e, host, port)
            self.show_toast(f"Cannot connect: {message}")
            self._show_status("Not connected", message)
            return
        if self.client.partition_failed:  # deleted meanwhile, or the server has no partitions
            self.show_toast(f"Partition \u201c{self.partition}\u201d is not available. Using the default one.")
            self.partition = self.client.partition = None
            self.settings.partition = ""
            self._schedule_flush()
        self._update_title()
        self.refresh_music_dir()
        self._apply_saved_replaygain()
        self.refresh_all()
        self._db_gate.reset()  # everything is loaded fresh just below
        self.reload_library()
        self.refresh_search_categories()
        if self.views.get_visible_child_name() == "playlists":
            self.refresh_playlists()
        elif self.views.get_visible_child_name() == "albums":
            self.reload_albums()  # started with the Albums view open
        self._start_idle()

    def refresh_music_dir(self):
        if self.eff.music_dir:
            self.music_dir = self.eff.music_dir
            return
        try:
            self.music_dir = self.client.music_directory() if self.client.connected else None
        except (OSError, MPDError):
            self.music_dir = None

    def _apply_saved_replaygain(self):
        """MPD resets replay gain on restart, so optionally set it on every connect."""
        s = self.settings
        if s.apply_replaygain and s.replaygain:
            try:
                self.client.set_replay_gain_mode(s.replaygain)
            except (OSError, MPDError):
                pass  # unsupported server: not worth bothering the user

    def _start_idle(self):
        """Watch MPD for changes on a background connection."""
        self._stop_idle()
        batch = self._idle_batch = ChangeBatcher()  # one refresh for reports that piled up
        self._idle = IdleWatcher(self.cfg, lambda changed: batch.add(changed) and GLib.idle_add(self._on_idle_batch, batch),
                                 partition=self.partition)
        self._idle.start()

    def _stop_idle(self):
        if self._idle:
            self._idle.stop()
            self._idle = None

    def apply_settings(self):
        """Called after the user saved new settings."""
        previous = (self.cfg, self.partition)
        self._recompute_effective()
        if (self.cfg, self.partition) != previous or not self.client.connected:
            self.reconnect()
        else:
            self.refresh_music_dir()

    def reconnect(self):
        """Drop every connection and start over with the current settings."""
        self._stop_idle()
        self.client.close()
        self.cover_loader.shutdown()
        self.player_cover_loader.shutdown()
        self._recompute_effective()
        self.client = self._new_client()
        self.cover_loader = self._new_cover_loader()
        self.player_cover_loader = self._new_player_cover_loader()
        self._cover_key = None
        self._show_cover(None)
        self.status = {}
        self._current_song = {}
        self._art_url = ""
        self._show_db_update(None)
        self.queue_store.remove_all()
        self.queue_history.clear()  # the steps belong to the old server's queue
        self._update_undo_actions()
        self._queue_rows = []
        self._album_items = {}
        self._queue_songs = []
        self._queue_groups = []
        self.collapsed_groups = set()
        self._queue_by_id = {}
        self._now_marker.reset()
        self.library_fetcher.shutdown()
        self.library_fetcher = self._new_library_fetcher()
        self.search_fetcher.shutdown()
        self.search_fetcher = self._new_library_fetcher()
        self.album_fetcher.shutdown()
        self.album_fetcher = self._new_library_fetcher()
        self._albums_gen += 1
        self._albums_all = None
        self._albums_loaded = False
        self.albums_store.remove_all()
        self._lib_gen += 1  # any load still running belongs to the old connection
        self._album_gen += 1
        self._shown_artist = None
        self._set_artists([])
        self.album_list.remove_all()
        self.reset_search()
        self.reset_playlists()
        self._show_status("Connecting\u2026", "")
        self._connect_and_start()

    # -- server: collections, partitions, outputs ----------------------------------------
    def _new_client(self):
        return MPDClient(*self.cfg, partition=self.partition)

    def _update_title(self):
        # the collection is named only when there is a choice between several
        name = self.settings.collection if len(self.settings.collections) > 1 else ""
        self.title_widget.set_subtitle(window_subtitle(name, self.partition))

    def open_server_dialog(self, page):
        if self._server_dialog is not None:
            self._server_dialog.set_visible_page_name(page)
            self._server_dialog.refresh()
            return
        dialog = ServerDialog(self, page)
        self._server_dialog = dialog
        dialog.connect("closed", lambda *_: setattr(self, "_server_dialog", None))
        dialog.present(self)

    def save_settings_now(self):
        try:
            save_settings(self.settings)
        except OSError as e:
            self.show_toast(f"Could not save settings: {e}")

    def collections_changed(self):
        """A collection was added, edited or deleted."""
        self.save_settings_now()
        self.apply_settings()  # the active one may have been edited
        self._update_title()

    def switch_collection(self, name):
        if name == snapshot(self.settings).name:
            return
        err = activate(self.settings, name)
        if err:
            self.show_toast(err)
            return
        self.save_settings_now()
        self._selected_artist = self.settings.last_artist or None
        self.apply_settings()
        self._update_title()
        message = f"Switched to \u201c{name}\u201d"
        pinned = [f for f in ("host", "port", "password") if f in self.overrides]
        if pinned:
            message += f" (the {pinned[0]} is set by {self.overrides[pinned[0]]}, so the connection stays the same)"
        self.show_toast(message)

    def switch_partition(self, name):
        """Move this window to another MPD partition. Returns True on success."""
        if not self.safe(lambda: self.client.switch_partition(name) or True):
            return False
        self.partition = name
        self.queue_history.clear()  # another partition, another queue
        self._update_undo_actions()
        self.settings.partition = name  # remembered per collection
        self._schedule_flush()
        self._start_idle()   # watch the new partition's events
        self.refresh_all()   # its queue and playback state
        self._update_title()
        return True

    def _on_idle_batch(self, batch):
        changed = batch.take()
        if changed:
            self.on_idle(changed)
        return False

    def on_idle(self, changed):
        # Through safe(): MPD closes the UI connection after connection_timeout (60 s
        # by default) without a command, which is normal while a track plays. The
        # idle watcher has its own connection, so the next track change arrives here
        # on a dead connection; without a reconnect the queue highlight and progress
        # bar stayed on the old song (user report 2026-11-12).
        self.safe(self._handle_idle, changed)
        return False

    def _handle_idle(self, changed):
        if "playlist" in changed:
            self.refresh_queue()
        status_events = {"player", "mixer", "options", "playlist", "update"} & set(changed)
        if status_events:
            self.refresh_status()
        if "database" in changed:
            self._db_gate.changed()
            if not status_events:
                self.refresh_status()  # is the update still running?
        reloaded = self._reload_if_db_settled()
        if self._pl_loaded and "stored_playlist" in changed and not reloaded:
            self.refresh_playlists()
        if self._server_dialog is not None and {"output", "partition"} & set(changed):
            self._server_dialog.refresh()

    def _reload_if_db_settled(self):
        """Reload library, albums and playlists once MPD's database update is over
        (DbReloadGate): not on every `database` event while it still runs."""
        queue_too = self._db_gate.take(db_update_job(getattr(self, "status", None)))
        if queue_too is None:
            return False
        if queue_too:
            self.refresh_queue()  # the tags of queued songs may have changed
        self._show_update_newest()
        self.reload_library()
        self.invalidate_albums()
        if self._pl_loaded:
            self.refresh_playlists()
        return True

    def _show_update_newest(self):
        """After a database update the artist list shows the artist of the newest
        song it added (and selects that album), not the one shown before (user
        request 2026-11-20). Asked before the artist names on the same worker, so
        the answer is there when _names_loaded runs."""
        since = getattr(self, "_db_stamp", None)
        if since is None:
            return
        gen = self._lib_gen + 1  # the reload that follows

        def done(found, err):
            if err is None and found and gen == self._lib_gen:
                self._update_target = found
        self.library_fetcher.newest_since(since, albumslib.recent_field(self.client.protocol_version), done)

    # -- undo / redo of queue changes (queueundo.py) ----------------------------------
    def change_queue(self, fn):
        """safe(fn) for a change of the queue made in Toccata: records an undo step
        (the queue as shown, i.e. as of the last refresh). Changes by other clients,
        or by MPD itself (consume), are not steps of their own."""
        self.queue_history.record(s.uri for s in self._queue_songs)
        self._update_undo_actions()
        return self.safe(fn)

    def undo_queue(self, redo=False):
        """Ctrl+Z / Ctrl+Shift+Z: restore the queue of the previous / next step.
        Entries that stay keep their ids, so the playing song is not interrupted."""
        current = self.safe(self.client.queue)  # fresh: the shown queue may lag behind
        if current is None:
            return
        uris = [s.get("file", "") for s in current]
        target = self.queue_history.redo(uris) if redo else self.queue_history.undo(uris)
        self._update_undo_actions()
        if target is None:
            return
        keep = (getattr(self, "status", None) or {}).get("songid")
        commands = restore_commands([(s.get("id"), s.get("file", "")) for s in current], target, keep)

        def run():
            for i in range(0, len(commands), 500):  # (MPD limits the size of a command list)
                self.client.command_list(commands[i:i + 500])
        self.safe(run)

    def _update_undo_actions(self):
        self.lookup_action("queue-undo").set_enabled(self.queue_history.can_undo)
        self.lookup_action("queue-redo").set_enabled(self.queue_history.can_redo)

    def _on_undo_key(self, keyval, state):
        """Ctrl+Z, Ctrl+Shift+Z and Ctrl+Y (from the window's key handler). Text fields
        keep their own undo, and an open dialog (tag editor) gets no queue undo."""
        mods = state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK
                        | Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.SUPER_MASK)
        ctrl, shift = Gdk.ModifierType.CONTROL_MASK, Gdk.ModifierType.SHIFT_MASK
        if keyval in (Gdk.KEY_z, Gdk.KEY_Z) and mods == ctrl:
            redo = False
        elif (keyval in (Gdk.KEY_z, Gdk.KEY_Z) and mods == ctrl | shift) \
                or (keyval in (Gdk.KEY_y, Gdk.KEY_Y) and mods == ctrl):
            redo = True
        else:
            return False
        if isinstance(self.get_focus(), Gtk.Editable) or self.get_visible_dialog() is not None:
            return False
        self.undo_queue(redo)
        return True

    def safe(self, fn, *args):
        """Run an MPD command on the UI thread; show failures as toasts.
        A dropped connection is re-established once (see call_with_reconnect)."""
        try:
            return call_with_reconnect(self.client, fn, *args,
                                       on_reconnect=self._apply_saved_replaygain)
        except CommandError as e:  # the server understood us and said no
            self.show_toast(describe_error(e))
        except (OSError, MPDError) as e:
            self.client.close()  # protocol state is unknown: start clean next time
            self.show_toast(describe_error(e, *self.cfg[:2]))
        return None

    def show_toast(self, text):
        self.toast.add_toast(Adw.Toast(title=text, timeout=5))

    # -- state refresh -------------------------------------------------------------
    def refresh_all(self):
        self.safe(self.refresh_queue)
        self.safe(self.refresh_status)

    def refresh_queue(self):
        # unchanged entries keep their objects, so GTK keeps its scroll anchor (reuse_entries in playback.py)
        old = self._queue_songs
        songs = reuse_entries(old, self.client.queue(), Song)
        old_ids = set(map(id, old))
        before = {id(s): s.hidden for s in songs if id(s) in old_ids}  # kept entries' collapse state
        self._queue_songs = songs
        self._queue_by_id = {s.data.get("id"): s for s in songs}
        self._prepare_groups()  # before the splice, so the filter sees the right state
        self._queue_rows_changing()
        self._apply_queue_rows()
        # untouched entries may have a new album group, collapse state or texts
        if any(before.get(id(s), s.hidden) != s.hidden for s in songs):
            self._queue_filter.changed(Gtk.FilterChange.DIFFERENT)
        for song in songs:
            if song.row is not None:
                song.row.refresh_texts()
        self._apply_marks()  # the new song objects start unmarked
        self._update_qfilter_count()
        pos, self._queue_select_after = self._queue_select_after, None
        n = self.queue_sel.get_n_items()
        if pos is not None and n:
            pos = min(pos, n - 1)
            while pos < n - 1 and isinstance(self.queue_sel.get_item(pos), QueueAlbum):
                pos += 1  # an album header moved up into the gap: take its first track
            self.queue_view.scroll_to(pos, Gtk.ListScrollFlags.FOCUS | Gtk.ListScrollFlags.SELECT, None)
        if os.environ.get("TOCCATA_DEBUG"):
            self._debug_queue_view(f"refresh: {len(songs)} songs, {n} rows")
            frames = [0]

            def later(_w, _clock):  # what the next frames actually lay out
                frames[0] += 1
                self._debug_queue_view(f"  frame {frames[0]} after refresh")
                return frames[0] < 3
            self.queue_view.add_tick_callback(later)

    def _apply_queue_rows(self):
        """Put the rows for _queue_songs / _queue_groups into the store: the songs, and
        when grouped a QueueAlbum before each album. Header items are kept per album
        (first entry's id) and get the current group; only the changed part of the
        store is spliced (playback.changed_range), so GTK keeps the rows it shows."""
        rows, items = [], {}
        if getattr(self, "_grouped", False):
            for g in self._queue_groups:
                item = self._album_items.get(g.head_id) or QueueAlbum(g)
                item.group = g
                items[g.head_id] = item
                rows.append(item)
                rows.extend(self._queue_songs[g.start:g.end])
        else:
            rows = list(self._queue_songs)
        self._album_items = items
        start, n_remove, insert = changed_range(self._queue_rows, rows)
        self._queue_rows = rows
        if n_remove or insert:
            self.queue_store.splice(start, n_remove, insert)
        for item in items.values():  # headers on screen: the album may have new tracks / state
            if item.widget is not None:
                item.widget.bind(item.group)

    def _drop_header_selection(self, sel, position, n_items):
        """Album header rows are never selected (Ctrl+A, Shift ranges and select_range
        would include them); the album is selected through its tracks."""
        if self._fixing_selection:
            return
        self._fixing_selection = True
        try:
            for pos in range(position, min(position + n_items, sel.get_n_items())):
                if sel.is_selected(pos) and isinstance(sel.get_item(pos), QueueAlbum):
                    sel.unselect_item(pos)
        finally:
            self._fixing_selection = False

    def _row_position(self, obj):
        """Position of a queue item in the (filtered) list, or None."""
        sel = self.queue_sel
        return next((i for i in range(sel.get_n_items()) if sel.get_item(i) is obj), None)

    def _debug_queue_view(self, tag):
        """TOCCATA_DEBUG: scroll state of the queue and how many rows lie inside the view."""
        adj = self.queue_view.get_vadjustment()
        scroller = self.queue_view.get_parent()
        height, inside, child = scroller.get_height(), 0, self.queue_view.get_first_child()
        top = None  # y of the topmost laid-out row: 0 when the list is really at its top
        while child is not None:
            ok, r = child.compute_bounds(scroller)
            if ok and child.get_mapped() and r.get_height() > 0:
                top = r.get_y() if top is None else min(top, r.get_y())
                if r.get_y() + r.get_height() > 0 and r.get_y() < height:
                    inside += 1
            child = child.get_next_sibling()
        focus = self.get_focus()
        print(f"queue {tag}: scroll {adj.get_value():.0f}/{adj.get_upper():.0f} page {adj.get_page_size():.0f}, "
              f"rows inside view {inside}, top row at y={top if top is None else round(top)}, grouped {self._grouped}, "
              f"focus in queue {focus is not None and focus.is_ancestor(self.queue_view)}", file=sys.stderr)

    def _apply_marks(self):
        """Highlight the queue entry that is playing or paused (and only that one)."""
        if not self.highlight:
            return
        positions = {id(song): pos for pos, song in enumerate(self._queue_songs)}
        changes = self._now_marker.update(self._queue_by_id, self.status)
        if os.environ.get("TOCCATA_DEBUG"):
            print("highlight:", self.status.get("state"), self.status.get("songid"),
                  [(positions.get(id(s)), st) for s, st in changes], file=sys.stderr)
        for song, state in changes:
            song.playing = state
            if song.row is not None:  # visible: restyle it now; otherwise bind reads song.playing
                song.row._show_state(state)
            if song.group is not None:  # the album header shows it too (even when collapsed)
                song.group.playing = state
                header = getattr(song.group, "header", None)
                if header is not None:
                    header.refresh()

    def refresh_status(self):
        self.status = st = self.client.status()
        cur = self.client.currentsong() if st.get("state") in ("play", "pause") else {}
        jumped = self._position_jumped(cur, st)
        self._current_song = cur
        self.now_title.set_text(cur.get("title") or os.path.basename(cur.get("file", "")) or "Nothing playing")
        self.now_sub.set_text(" — ".join(p for p in (cur.get("artist"), cur.get("album")) if p))
        self.update_cover(cur.get("file"), cur)
        playing = st.get("state") == "play"
        self.play_btn.set_icon_name("media-playback-pause-symbolic" if playing else "media-playback-start-symbolic")
        self._show_db_update(db_update_job(st))
        self._show_play_options(st)
        dur = float(st.get("duration") or 0)
        self.seek.set_range(0, max(dur, 1))
        self._elapsed_at = (float(st.get("elapsed") or 0), time.monotonic())
        if "volume" in st and int(st["volume"]) >= 0:
            self.vol.handler_block_by_func(self.on_volume)
            self.vol.set_value(int(st["volume"]))
            self.vol.handler_unblock_by_func(self.on_volume)
        self._apply_marks()
        self._tick()
        self._notify_desktop()
        if self.main_stack.get_visible_child_name() == "info":
            self.info_view.show_song(self._info_song())
        if jumped:
            self.mpris.seeked(int(self.elapsed() * 1_000_000))

    # -- Info view ------------------------------------------------------------------------
    def set_info_visible(self, on):
        if self.info_btn.get_active() != on:
            self.info_btn.set_active(on)  # (calls back here)
            return
        self.main_stack.set_visible_child_name("info" if on else "main")
        if on:
            self.info_view.set_cover(self._cover_texture)
            self.info_view.show_song(self._info_song())

    def _info_song(self):
        """The song the Info view is about: the playing / paused one, else (stopped) the
        queue's current entry, else nothing."""
        if self._current_song:
            return self._current_song
        song = self._queue_by_id.get(self.status.get("songid")) if self.status else None
        return song.data if song is not None else {}

    def library_artist_names(self, shown=False):
        """Every artist of the library (of the genre), or only those the filter shows."""
        if not shown:
            return list(self._all_artists)
        return [self.artist_store.get_string(i) for i in range(self.artist_store.get_n_items())]

    def info_load_artist(self, name, on_done):
        self.library_fetcher.load_artist(name, on_done, wanted=lambda: True)

    def info_show_artist(self, name):
        """A similar artist from the Info view: show it in the Artists view."""
        i = self._artist_index(name)
        if i is None:
            self.show_toast(f"\u201c{name}\u201d is not in the artist list (genre filter?)")
            return
        self.set_info_visible(False)
        self.views.set_visible_child_name("library")
        self.art_sel.set_selected(i)
        GLib.timeout_add(300, self._scroll_to_artist, i)

    def info_play_track(self, track):
        """A track clicked in the Info view: play its queue entry, or add it and play it."""
        uri = track.get("file")
        if not uri:
            return
        pos = self.status.get("song") if self.status else None
        queue = [(s.data.get("id"), s.uri) for s in self._queue_songs]
        songid = entry_to_play(queue, uri, int(pos) if pos not in (None, "") else None)
        if songid is None:
            songid = self.change_queue(lambda: self.client.addid(uri))
            if songid is None:
                return  # (the error is shown as a toast)
            self.show_toast(f"Added \u201c{track.get('title') or os.path.basename(uri)}\u201d to the queue")
        self.safe(self.client.playid, songid)

    def info_show_song_album(self, track):
        self.set_info_visible(False)
        self.show_song_in_artists(track)

    def _show_db_update(self, job):
        label, tip = db_update_text(job)
        running = label is not None
        if running:
            self.db_update_label.set_text(label)
            self.db_update.set_tooltip_text(tip)
        if isinstance(self.db_update_spinner, Gtk.Spinner):
            self.db_update_spinner.set_spinning(running)  # Adw.Spinner spins while visible
        self.db_update.set_visible(running)

    # -- desktop integration: MPRIS and the tray --------------------------------------
    def elapsed(self):
        """Seconds into the current song, extrapolated while playing."""
        base, t0 = self._elapsed_at
        playing = self.status.get("state") == "play"
        elapsed = base + (time.monotonic() - t0 if playing else 0)
        dur = float(self.status.get("duration") or 0)
        return min(elapsed, dur) if dur else elapsed

    def _position_jumped(self, cur, st):
        """True if another client seeked (MPRIS wants a Seeked signal then)."""
        if not self._current_song or cur.get("id") != self._current_song.get("id"):
            return False
        try:
            reported = float(st.get("elapsed") or 0)
        except ValueError:
            return False
        return abs(reported - self.elapsed()) > 2.0

    def seek_to(self, seconds, announce=True):
        seconds = max(0.0, seconds)
        self.safe(self.client.seekcur, seconds)
        self._elapsed_at = (seconds, time.monotonic())
        if announce:
            self.mpris.seeked(int(seconds * 1_000_000))

    def _notify_desktop(self):
        self.mpris.update()
        if self.tray is not None:
            cur = self._current_song
            title = cur.get("title") or os.path.basename(cur.get("file", ""))
            artist = cur.get("artist", "")
            text = f"{title} \u2014 {artist}" if title and artist else (title or "Nothing playing")
            self.tray.set_tooltip("Toccata", text)
            self.tray.refresh()

    def _tray_state(self):
        cur = self._current_song
        title = cur.get("title") or os.path.basename(cur.get("file", ""))
        return {"title": title, "artist": cur.get("artist", "") if title else "",
                "playing": self.status.get("state") == "play", "window_visible": self.get_visible()}

    def start_tray(self):
        if self.tray is not None:
            return
        self.tray = Tray({
            "toggle_window": self.toggle_window,
            "play_pause": lambda: self.on_play_pause(None),
            "previous": lambda: self.safe(self.client.previous),
            "next": lambda: self.safe(self.client.next),
            "stop": lambda: self.safe(self.client.stop),
            "quit": self.request_quit,
            "volume_step": self._volume_step,
            "state": self._tray_state,
        }, ICON_DIR)
        self.tray.start()

    def stop_tray(self):
        if self.tray is not None:
            self.tray.stop()
            self.tray = None
        if not self.get_visible():
            self.show_window()  # never leave the window hidden without a tray to bring it back

    def _volume_step(self, direction):
        try:
            v = int(self.status.get("volume", -1))
        except ValueError:
            return
        if v >= 0:
            self.safe(self.client.setvol, max(0, min(100, v + 5 * direction)))

    def show_window(self):
        self.set_visible(True)
        self.present()
        if self.tray is not None:
            self.tray.refresh()

    def toggle_window(self):
        if self.get_visible():
            self._hide_to_tray()
        else:
            self.show_window()

    def _hide_to_tray(self):
        if WindowMemory.capture(self.settings, *self._window_geometry()):
            self._schedule_flush()
        if self._flush_scheduled:
            self._flush_settings()
        self._hide_cover_preview()
        self.set_visible(False)
        if self.tray is not None:
            self.tray.refresh()

    def request_quit(self):
        """Really quit (tray menu, Ctrl+Q, MPRIS Quit), even when closing hides to the tray."""
        self._quitting = True
        self.close()

    def _save_art(self, texture):
        """Write the shown cover to a file for MPRIS (mpris:artUrl); one file per album folder."""
        if texture is None or not self._cover_key:
            self._art_url = ""
            return
        folder = os.path.join(GLib.get_user_cache_dir(), "toccata", "mpris")
        path = os.path.join(folder, mprislib.art_file_name(self._cover_key))
        try:
            os.makedirs(folder, exist_ok=True)
            if not os.path.exists(path):
                for old in os.listdir(folder):  # keep only the current cover
                    if old.startswith("cover-") and old.endswith(".png"):
                        os.remove(os.path.join(folder, old))
                texture.save_to_png(path)
            self._art_url = GLib.filename_to_uri(path, None)
        except (OSError, GLib.Error):
            self._art_url = ""

    def _window_geometry(self):
        return (self.get_width(), self.get_height(), self.is_maximized(), self.is_fullscreen())

    def _poll_window(self):
        if self._closing:
            return False  # stop the timer
        if self._window_memory.observe(self.settings, *self._window_geometry()):
            self._schedule_flush()
        return True

    def _on_close_request(self, _win):
        if (not self._quitting and self.tray is not None and self.tray.registered
                and self.settings.close_to_tray):
            self._hide_to_tray()  # "minimise to tray"; Quit is in the tray menu
            return True
        if self.tray is not None:
            self.tray.stop()
        self.mpris.stop()
        self._closing = True
        self._hide_cover_preview()
        self.cover_preview.unparent()  # a popover must not outlive its parent widget
        if WindowMemory.capture(self.settings, *self._window_geometry()):
            self._schedule_flush()
        self._stop_idle()
        self.cover_loader.shutdown()
        self.player_cover_loader.shutdown()
        self.library_fetcher.shutdown()
        self.search_fetcher.shutdown()
        self.album_fetcher.shutdown()
        if self._flush_scheduled:
            self._flush_settings()  # do not lose the last artist or divider the user picked
        return False  # let the window close

    # -- cover ---------------------------------------------------------------------
    def update_cover(self, uri, song=None):
        if not self.show_covers:
            return
        # Streams have no folder art; also nothing to show when stopped.
        if not uri or "://" in uri:
            key = None
        else:
            key = CoverFetcher.key_for(uri)
        if key == self._cover_key and key is not None:
            return  # same album folder, cover already shown or being fetched
        if key is None and self._cover_key is None:
            return
        self._cover_key = key
        if key is None:
            self._show_cover(None)
            return
        self._show_cover(None)  # clear the old cover while the new one loads
        self.player_cover_loader.request(uri, lambda tex, k=key: self._on_cover(k, tex), song=song)

    def _on_cover(self, key, texture):
        if key == self._cover_key:  # ignore answers for a song we already left
            self._show_cover(texture)

    def _show_cover(self, texture):
        self._cover_texture = texture
        self.cover.set_texture(texture)
        if getattr(self, "info_view", None) is not None:
            self.info_view.set_cover(texture)
        self._save_art(texture)
        if getattr(self, "mpris", None) is not None:
            self.mpris.update()
        if self.cover_preview.get_visible():
            if texture is None:
                self._hide_cover_preview()
            else:
                self._show_cover_preview()  # the song changed while hovering: show the new cover

    def _schedule_cover_preview(self):
        if self._preview_timer:
            GLib.source_remove(self._preview_timer)
        self._preview_timer = GLib.timeout_add(500, self._preview_due)  # not on every pass of the mouse

    def _preview_due(self):
        self._preview_timer = 0
        self._show_cover_preview()
        return False

    def _show_cover_preview(self):
        tex = self._cover_texture
        if tex is None:
            return
        # as big as fits above the player bar, with some room to spare
        max_w = max(1, int(self.get_width() * 0.6))
        max_h = max(1, int((self.get_height() - self.player_bar.get_height()) * 0.85))
        w, h = preview_size(tex.get_width(), tex.get_height(), max_w, max_h)
        if not w:
            return
        self.cover_preview_art.show(tex, w, h)
        self.cover_preview.popup()

    def _hide_cover_preview(self):
        if self._preview_timer:
            GLib.source_remove(self._preview_timer)
            self._preview_timer = 0
        self.cover_preview.popdown()

    def _tick(self):
        st = self.status
        if not st:
            return True
        base, t0 = self._elapsed_at
        elapsed = base + (time.monotonic() - t0 if st.get("state") == "play" else 0)
        dur = float(st.get("duration") or 0)
        elapsed = min(elapsed, dur) if dur else elapsed
        if not self._seeking:
            self.seek.set_value(elapsed)
        self.time_lbl.set_text(f"{fmt_time(elapsed)} / {fmt_time(dur)}")
        return True

    # -- playback handlers --------------------------------------------------------------
    def on_play_pause(self, _b):
        state = self.status.get("state")
        self.safe(self.client.toggle_pause if state in ("play", "pause") else self.client.play)

    def _on_option_toggled(self, button, name):
        if self._syncing_options:
            return  # we are only showing MPD's state, not changing it
        self.safe(self.client.set_play_option, name, button.get_active())

    def _show_play_options(self, status):
        self._syncing_options = True
        try:
            for name, on in play_options(status).items():
                self.option_buttons[name].set_active(on)
        finally:
            self._syncing_options = False

    def on_volume(self, scale):
        self.safe(self.client.setvol, scale.get_value())

    def on_seek(self, scale, _scroll, value):
        # change-value fires for every pointer motion while dragging and may lie
        # outside the range; clamp it and send at most one seek per 150 ms.
        adj = scale.get_adjustment()
        value = max(adj.get_lower(), min(value, adj.get_upper()))
        self._seek_target = value
        self._seeking = True
        self._elapsed_at = (value, time.monotonic())
        if self._seek_source is None:
            self._seek_source = GLib.timeout_add(150, self._send_seek)
        return False

    def _send_seek(self):
        self._seek_source = None
        self._seeking = False
        if self._closing:
            return False
        self.seek_to(self._seek_target)
        return False

    def play_queue_song(self, song):
        self.safe(self.client.playid, song.data["id"])

    # -- drag and drop -----------------------------------------------------------------
    @staticmethod
    def _tag(text):
        """The drag's content: only a text, which GTK shows at the pointer (so a
        readable one, dnd.drag_label); what is dragged is in self._drag."""
        _drag_text[0] = text  # for the label beside the pointer (DragSource)
        value = GObject.Value()
        value.init(GObject.TYPE_STRING)
        value.set_string(text)
        return Gdk.ContentProvider.new_for_value(value)

    def _setup_artist_row(self, _factory, item):
        label = Gtk.Label(xalign=0, margin_top=8, margin_bottom=8, margin_start=12, margin_end=12,
                          ellipsize=Pango.EllipsizeMode.END)
        label.artist = None
        src = DragSource(actions=Gdk.DragAction.COPY)
        src.connect("prepare", lambda _s, _x, _y, lbl=label: self._drag_artist(lbl.artist))
        label.add_controller(src)
        _right_click(label, lambda x, y, lbl=label: self._artist_menu(lbl, x, y))
        item.set_child(label)

    # -- right-click menus (Cantata's library context menu) ------------------------------
    def _with_artist_tracks(self, name, fn):
        """fn(tracks) with the artist's tracks (as shown: genre filter applied)."""
        cached = self.library_fetcher.cached(name, self._genre)
        if cached is not None:
            fn(cached.tracks())
            return
        self.library_fetcher.load_artist(
            name, lambda artist, err: fn(artist.tracks()) if err is None
            else self.show_toast(describe_error(err, *self.cfg[:2])), wanted=lambda: True, genre=self._genre)

    def _track_menu_entries(self, get_tracks, name=None):
        """Add / Play / Add to Playlist / Edit Tags for tracks from get_tracks(fn)."""
        return [("Add to Queue", lambda: get_tracks(lambda t: self.add_tracks(t, name))),
                ("Play", lambda: get_tracks(self.play_tracks)),
                ("Add to Playlist\u2026", lambda: get_tracks(self.add_to_playlist)),
                None,
                ("Edit Tags\u2026", lambda: get_tracks(self.edit_tracks))]

    def _artist_menu(self, label, x, y):
        name = label.artist
        if not name:
            return
        _context_menu(label, x, y, self._track_menu_entries(lambda fn: self._with_artist_tracks(name, fn), name))

    def _album_menu(self, row, x, y):
        if row not in self._sel_albums:
            self.album_click(row, 1)  # right-click outside the selection selects that album
        tracks = self._selected_album_tracks() or row.album.tracks
        entries = self._track_menu_entries(lambda fn: fn(tracks),
                                           row.album.title if len(self._sel_albums) <= 1 else None)
        entries.insert(4, ("Set Cover\u2026", lambda: self.search_cover_online(row.album.tracks, row.album.title)))
        _context_menu(row.header or row, x, y, entries)

    def _track_row_menu(self, row, x, y):
        if row not in self._sel_tracks:
            self.track_click(row, 1)
        tracks = self._selected_album_tracks() or [row.track]
        _context_menu(row, x, y, self._track_menu_entries(lambda fn: fn(tracks)))

    # -- Set Cover... -----------------------------------------------------------------------
    def set_cover(self, tracks, title):
        """Choose an image and save it as cover.<ext> in the album's folder(s), as
        Cantata does; MPD then serves it like any other folder cover."""
        if not self.music_dir:
            self.show_toast("Set the music folder in Preferences to change covers")
            return
        uris = [t["file"] for t in tracks if t.get("file")]
        folders = album_folders(self.music_dir, uris)
        if not folders:
            self.show_toast("The album's folder is not in the music folder")
            return
        dialog = Gtk.FileDialog(title=f"Cover for \u201c{title}\u201d", modal=True)
        images = Gtk.FileFilter(name="Images")
        images.add_mime_type("image/*")
        filters = Gio.ListStore(item_type=Gtk.FileFilter)
        filters.append(images)
        dialog.set_filters(filters)
        dialog.set_default_filter(images)
        dialog.open(self, None, lambda d, res: self._cover_chosen(d, res, uris, folders, title))

    def search_cover_online(self, tracks, title):
        """"Set Cover...": search online (Cantata's cover dialog); "Choose File..." in
        the dialog opens the file chooser instead (set_cover)."""
        if not self.music_dir:
            self.show_toast("Set the music folder in Preferences to change covers")
            return
        uris = [t["file"] for t in tracks if t.get("file")]
        folders = album_folders(self.music_dir, uris)
        if not folders:
            self.show_toast("The album's folder is not in the music folder")
            return
        t = tracks[0] if tracks else {}
        artist = t.get("albumartist") or t.get("artist") or ""
        CoverSearchDialog(self, artist, title, lambda data: self._use_cover_data(data, uris, folders, title),
                          on_file=lambda: self.set_cover(tracks, title)).present()

    def _cover_chosen(self, dialog, result, uris, folders, title):
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return  # cancelled
        try:
            ok, data, _etag = gfile.load_contents(None)
            data = bytes(data)
        except GLib.Error as e:
            self.show_toast(f"Could not read the image: {e.message}")
            return
        self._use_cover_data(data, uris, folders, title)

    def _use_cover_data(self, data, uris, folders, title):
        if cover_extension(data) is None:  # e.g. GIF or BMP: store as PNG
            try:
                loader = GdkPixbuf.PixbufLoader()
                loader.write(data)
                loader.close()
                ok, png = loader.get_pixbuf().save_to_bufferv("png", [], [])
                data = bytes(png)
            except (GLib.Error, AttributeError):
                self.show_toast("That file is not an image Toccata can use")
                return
        old = [p for f in folders for p in existing_covers(f)]
        if old:
            names = ", ".join(sorted({os.path.basename(p) for p in old}))
            _confirm(self, "Replace the Cover?",
                     f"\u201c{title}\u201d already has {names}. It is kept as \u2026.old next to the new one.",
                     "Replace", lambda: self._write_cover(uris, folders, data, title))
        else:
            self._write_cover(uris, folders, data, title)

    def _write_cover(self, uris, folders, data, title):
        errors = []
        for folder in folders:
            try:
                write_cover(folder, data)
            except CoverWriteError as e:
                errors.append(str(e))
        if errors:
            self.show_toast(errors[0])
        if len(errors) < len(folders):
            self.covers_changed({CoverFetcher.key_for(u) for u in uris})
            self.show_toast(f"Cover set for \u201c{title}\u201d")

    def covers_changed(self, dirs):
        """Covers of these folders (MPD paths) changed: forget them and show the new ones."""
        for d in dirs:
            self.cover_loader.forget(d)
            self.player_cover_loader.forget(d)
        for row in self._album_rows():  # Artists view
            if row.album.cover_uri and CoverFetcher.key_for(row.album.cover_uri) in dirs:
                row.thumb.set_texture(None)
                row.cover_wanted = True
        self._schedule_album_covers()
        for i in range(self.albums_store.get_n_items()):  # Albums view (tiles on screen)
            item = self.albums_store.get_item(i)
            if item.tile is not None and item.entry.uri and CoverFetcher.key_for(item.entry.uri) in dirs:
                self._album_tile_cover(item)
        for g in self._queue_groups:  # queue and playlist album headers
            header = getattr(g, "header", None)
            uri = self.queue_song_at(g.start)
            if header is not None and uri and CoverFetcher.key_for(uri) in dirs:
                header.bind(g)
        for g in self._plg.groups:
            header = getattr(g, "header", None)
            uri = self._plg.queue_song_at(g.start)
            if header is not None and uri and CoverFetcher.key_for(uri) in dirs:
                header.bind(g)
        cur = self._current_song
        if cur.get("file") and CoverFetcher.key_for(cur["file"]) in dirs:  # player bar and MPRIS
            art = os.path.join(GLib.get_user_cache_dir(), "toccata", "mpris", mprislib.art_file_name(self._cover_key or ""))
            try:
                os.remove(art)
            except OSError:
                pass
            self._cover_key = None
            self.update_cover(cur["file"], cur)

    def _bind_artist_row(self, _factory, item):
        name = item.get_item().get_string()
        item.get_child().artist = name
        item.get_child().set_text(name)

    def _drag_artist(self, name):
        if not name:
            return None
        self._drag = ("artist", name)
        return self._tag(name)

    def _drag_album(self, row, x=None, y=None):
        """A drag that starts on an album row. If it started on one of its tracks
        (the album's drag source covers its opened tracks too, and GTK may pick it
        before the track's own), drag the track(s) instead of the whole album."""
        if x is not None and y is not None:
            w = row.pick(x, y, Gtk.PickFlags.DEFAULT)
            while w is not None and w is not row:
                if getattr(w, "track", None) is not None and w in getattr(row, "track_rows", []):
                    return self._drag_tracks(w)
                w = w.get_parent()
        self._pending_click = None  # a drag keeps the selection
        rows = self._sel_albums if row in self._sel_albums else [row]
        self._drag = ("tracks", [t for r in self._album_rows() if r in rows for t in r.album.tracks])
        return self._tag(drag_label([r.album.title for r in rows], "album"))

    def _drag_queue(self, song):
        if song is None:
            return None
        songs = self._songs_for_row(song)
        self._drag = ("queue", [s.data["id"] for s in songs])
        return self._tag(tracks_label([s.data for s in songs]))

    def _queue_insert_index(self, x, y):
        """Queue index a drop at (x, y) of the queue list means: before / after the row under it."""
        return self._queue_drop_target(x, y)[0]

    def _queue_drop_target(self, x, y):
        """(queue index, y of the marker line in queue list coordinates or None) for a
        drop at (x, y). The line is drawn from the same answer the drop uses."""
        n = len(self._queue_songs)
        # By height, not Gtk.Widget.pick: on the 1 px border between two rows pick hits
        # the list item itself, which looked like "below the last row" and made the
        # line jump to the bottom (user report 2026-11-12).
        widget = self._queue_row_at(y)
        if isinstance(widget, QueueHeader):
            if widget.group is None:
                return n, self._queue_end_y()
            return widget.group.start, self._row_edges(widget)[0]  # before that album
        if widget is None or widget._song is None:
            return n, self._queue_end_y()
        row = next((i for i, s in enumerate(self._queue_songs) if s is widget._song), None)
        ok, bounds = widget.compute_bounds(self.queue_view)
        if row is None or not ok:
            return (n, self._queue_end_y()) if row is None else (row, None)
        at = insert_index(row, y - bounds.get_y(), bounds.get_height(), n)
        at = queuegroups.drop_index(widget._song.group, row, at, self.collapsed_groups)
        top, bottom = self._row_edges(widget)
        return at, line_y(at, row, top, bottom)

    def _queue_row_at(self, y):
        """The SongRow or QueueHeader of the queue list item at height y (list
        coordinates); the nearest item when y is in a gap, None below the last one."""
        best, best_dist = None, None
        child = self.queue_view.get_first_child()
        while child is not None:
            box = child.get_first_child()
            if child.get_mapped() and hasattr(box, "song_row"):
                ok, b = child.compute_bounds(self.queue_view)
                if ok and b.get_height() > 0:
                    top, bottom = b.get_y(), b.get_y() + b.get_height()
                    dist = 0 if top <= y < bottom else min(abs(y - top), abs(y - bottom))
                    if best_dist is None or dist < best_dist:
                        best, best_dist = (box.header if box.header.get_visible() else box.song_row), dist
            child = child.get_next_sibling()
        if best is None or y >= self._queue_end_y():
            return None
        return best

    def _row_edges(self, widget):
        """Top and bottom of the list item holding `widget` (a row of the queue list)."""
        item = widget
        while item.get_parent() is not None and item.get_parent() is not self.queue_view:
            item = item.get_parent()
        ok, b = item.compute_bounds(self.queue_view)
        if not ok:
            ok, b = widget.compute_bounds(self.queue_view)
        return (b.get_y(), b.get_y() + b.get_height()) if ok else (0.0, 0.0)

    def _queue_end_y(self):
        """Bottom of the last row shown in the queue list (0 when it is empty)."""
        end, child = 0.0, self.queue_view.get_first_child()
        while child is not None:
            if child.get_mapped():
                ok, b = child.compute_bounds(self.queue_view)
                if ok and b.get_height() > 0:
                    end = max(end, b.get_y() + b.get_height())
            child = child.get_next_sibling()
        return end

    def _on_queue_drag_motion(self, target, x, y):
        line = self._queue_drop_line
        _at, ly = self._queue_drop_target(x, y)
        if ly is None:
            line.set_visible(False)
        else:
            ok, p = self.queue_view.compute_point(self._queue_overlay, Graphene.Point().init(0, ly))
            if ok:
                height = max(self._queue_overlay.get_height(), 2)
                line.set_margin_top(int(min(max(p.y - 1, 0), height - 2)))  # centred on the edge
                line.set_visible(True)
            else:
                line.set_visible(False)
        drop = target.get_current_drop()
        actions = drop.get_actions() if drop is not None else Gdk.DragAction.COPY
        return Gdk.DragAction.COPY if actions & Gdk.DragAction.COPY else Gdk.DragAction.MOVE

    def _on_queue_drop(self, _target, _value, x, y):
        self._queue_drop_line.set_visible(False)
        drag, self._drag = self._drag, None
        if not drag:
            return False
        kind, data = drag
        at = self._queue_insert_index(x, y)
        if kind == "queue":
            moves = plan_moves([s.data["id"] for s in self._queue_songs], data, at)
            if moves:
                self.change_queue(lambda: self.client.move_ids(moves) or True)
        elif kind == "tracks":
            self._insert_tracks(data, at)
        elif kind == "albums":
            self.insert_albums(data, at)
        elif kind == "playlist":
            pos = None if at >= len(self._queue_songs) else at  # appending works on every MPD
            if self.change_queue(lambda: self.client.load_playlist(data, position=pos) or True):
                self.show_toast(f"Added \u201c{data}\u201d")
        elif kind == "artist":
            genre = self._genre  # a dragged artist brings only the filtered tracks
            cached = self.library_fetcher.cached(data, genre)
            if cached is not None:
                self._insert_tracks(cached.tracks(), at)
            else:
                self.library_fetcher.load_artist(
                    data, lambda artist, err: self._insert_tracks(artist.tracks(), at) if err is None
                    else self.show_toast(describe_error(err, *self.cfg[:2])),
                    wanted=lambda: True, genre=genre)
        return True

    def _insert_tracks(self, tracks, at):
        uris = [t["file"] for t in tracks]
        if uris and self.change_queue(lambda: self.client.add_many(uris, position=at) or True):
            self.show_toast(f"Added {len(uris)} track" + ("" if len(uris) == 1 else "s"))

    # -- right-click menu of the queue ----------------------------------------------------
    def _queue_menu(self, song, widget, x, y):
        if song not in self._queue_selected():
            for i in range(self.queue_sel.get_n_items()):  # right-click outside the selection selects it
                if self.queue_sel.get_item(i) is song:
                    _select_like_click(self.queue_view, i)
                    break
        self._show_queue_menu(self._queue_selected() or self._entries_of(song), song, widget, x, y)

    def header_menu(self, group, widget, x, y):
        """Right-click on an album header of the grouped queue: the whole album."""
        self.select_group(group)
        songs = self._queue_songs[group.start:group.end]
        if songs:
            self._show_queue_menu(songs, songs[0], widget, x, y)

    def _show_queue_menu(self, songs, first, widget, x, y):
        n = len(songs)
        what = f"{n} tracks" if n > 1 else "track"
        _context_menu(widget, x, y, [
            ("Play", lambda: self.play_queue_song(first)),
            None,
            ("Edit Tags\u2026", lambda: self.edit_tags(songs)),
            ("Show in Artists", lambda: self.show_song_in_artists(first.data)),
            ("Show in Albums", lambda: self.show_song_in_albums(first.data)),
            ("Add to Playlist\u2026", lambda: self.add_to_playlist([s.data for s in songs])),
            None,
            (f"Remove {what}" if n > 1 else "Remove", lambda: self.remove_songs(songs))])

    def show_song_in_artists(self, song):
        """The song's album in the Artists view (artist selected, album selected)."""
        entry = albumslib.song_album(song)
        names = set(self._all_artists)
        if entry.artist not in names and song.get("composer") in names:
            entry.artist = song["composer"]  # listed under its composer (composer genres)
        self.locate_album(entry)

    def show_song_in_albums(self, song):
        """The song's album selected in the Albums view."""
        self._albums_pending = albumslib.song_album(song).key
        self.views.set_visible_child_name("albums")  # loads the view if needed
        if self.albums_store.get_n_items():  # else: _albums_show looks once albums arrive
            self._albums_check_pending(done=not self._albums_sort_key() == "added")

    def _albums_check_pending(self, done):
        """Select the album asked for by "Show in Albums" if it is loaded. "Recently
        added" is only loaded as far as scrolled: then sort by artist (has them all)."""
        if self._albums_select_pending():
            return
        if self._albums_sort_key() == "added":
            self.show_toast("Sorted by artist to show the album")
            self.albums_sort.set_selected([k for k, _l in albumslib.SORTS].index("artist"))
        elif done:
            self._albums_pending = None
            self.show_toast("That album is not in the Albums view")

    def _albums_select_pending(self):
        key = getattr(self, "_albums_pending", None)
        if key is None:
            return False
        for i in range(self.albums_store.get_n_items()):
            if self.albums_store.get_item(i).entry.key == key:
                self._albums_pending = None
                self.albums_grid.scroll_to(i, Gtk.ListScrollFlags.FOCUS | Gtk.ListScrollFlags.SELECT, None)
                return True
        return False

    def _queue_selected(self):
        """Selected queue entries; a collapsed album's row counts as all its tracks."""
        bits = self.queue_sel.get_selection()
        out = []
        for i in range(bits.get_size()):
            item = self.queue_sel.get_item(bits.get_nth(i))
            if isinstance(item, Song):  # (album header rows are not selectable)
                out.extend(self._entries_of(item))
        return out

    def _entries_of(self, song):
        if song.summary and song.group is not None:
            return self._queue_songs[song.group.start:song.group.end]
        return [song]

    # -- queue grouped by album ----------------------------------------------------------

    def _set_compact(self, on, save=True):
        """Compact queue rows (one line each). Only CSS and the rows' layout change,
        never the model, so GTK keeps its scroll anchor; tracks and album headers
        stay exactly as tall as each other (QueueAlbum)."""
        on = bool(on)
        self.lookup_action("queue-compact").set_state(GLib.Variant.new_boolean(on))
        if save and on != self.settings.queue_compact:
            self.settings.queue_compact = on
            self._schedule_flush()
        if on != getattr(self, "queue_compact", None) and self.queue_view.get_mapped():
            self._keep_queue_top_row()
        self.queue_compact = on
        (self.queue_view.add_css_class if on else self.queue_view.remove_css_class)("queue-compact")
        child = self.queue_view.get_first_child()
        while child is not None:  # the rows that exist now; new ones get it in setup
            box = child.get_first_child()
            if hasattr(box, "song_row"):
                box.song_row.set_compact(on)
                box.header.set_compact(on)
            child = child.get_next_sibling()

    def _keep_queue_top_row(self):
        """The row height is about to change: keep the row at the top of the view
        there. GTK keeps its own anchor row, which can be any row on screen, so the
        view moved by a few rows. All rows are equally tall (QueueAlbum), so the
        top row's index is the scroll value over the row pitch. Restored from a tick
        callback once the list has its new height (one frame may show GTK's
        position), never from inside the layout pass (DECISIONS)."""
        adj, n = self.queue_view.get_vadjustment(), self.queue_sel.get_n_items()
        if adj is None or not n or adj.get_upper() <= 0:
            return
        old_pitch = adj.get_upper() / n
        top = adj.get_value() / old_pitch
        frames = [0]

        def tick(_w, _clock):
            frames[0] += 1
            n_now = self.queue_sel.get_n_items()
            pitch = adj.get_upper() / n_now if n_now else old_pitch
            if n_now != n or frames[0] > 20:
                return GLib.SOURCE_REMOVE  # the queue changed meanwhile, or no re-layout
            if abs(pitch - old_pitch) < 0.5:
                return GLib.SOURCE_CONTINUE  # not laid out at the new height yet
            target = max(0.0, min(top * pitch, adj.get_upper() - adj.get_page_size()))
            if abs(adj.get_value() - target) >= 1:
                self.queue_user_scrolled()  # the top pin must not undo it
                adj.set_value(target)
            return GLib.SOURCE_REMOVE
        self.queue_view.add_tick_callback(tick)

    def _set_grouped(self, on, save=True):
        on = bool(on)
        action = self.lookup_action("queue-grouped")
        action.set_state(GLib.Variant.new_boolean(on))
        for name in ("queue-collapse-all", "queue-expand-all"):
            self.lookup_action(name).set_enabled(on)
        if save and on != self.settings.queue_grouped:
            self.settings.queue_grouped = on
            self._schedule_flush()
        self._grouped = on
        self._prepare_groups()
        self._regroup_visible()

    def _pin_queue_top(self, adj):
        # also ungrouped: at the top, added songs must not move the view (user report 2026-11-11)
        self._queue_pin = _pin_list_top(adj, lambda: True)

    def queue_user_scrolled(self):
        """The user scrolls the queue (from _watch_user_scroll; tests call it directly)."""
        _pin_user_input(getattr(self, "_queue_pin", None))

    def _queue_rows_changing(self):
        pin = getattr(self, "_queue_pin", None)
        _rows_changing(pin)
        # At the top: anchor GTK on the first row where it is now, so the change does
        # not move the view at all. GTK otherwise keeps whatever row it last anchored
        # on (after a Delete: the row that moved up, or a guess if that row was
        # removed), and an add then scrolled to keep that row in place, which the pin
        # could only undo afterwards (a hiccup; user: "just stay there and don't
        # scroll at all", 2026-11-11). scroll_to without flags on a visible row does
        # not scroll, it only moves the anchor.
        adj = self.queue_view.get_vadjustment()
        if (pin is not None and pin["pinned"] and adj is not None and adj.get_value() <= 0
                and self.queue_sel.get_n_items() and self.queue_view.get_mapped()):
            self.queue_view.scroll_to(0, Gtk.ListScrollFlags.NONE, None)
        elif self._queue_select_after is not None and adj is not None and self.queue_view.get_mapped():
            self._anchor_above_gap(adj, self._queue_select_after)

    def _anchor_above_gap(self, adj, first):
        """Rows are about to be removed from view row `first` on (remove_songs).
        The rows above the gap do not change, so they should not move; the rows
        after the gap move up into it. GTK anchors the view on a row of its own
        choice, often one below the gap, and then the rows above moved down instead
        (one row for a single track; 112 px removing a 25-track album with compact
        rows, 2026-11-13). Re-anchoring with scroll_to(top, NONE) before the change,
        and focusing the gap row without scrolling, did not change that. So: the top
        row on screen (if above the gap) and its y are remembered, and once GTK has
        laid out the shorter list the view is scrolled back by what that row moved,
        from a tick callback (not from the layout pass, see DECISIONS); one frame
        may show the shift. All rows are equally tall (QueueAlbum), so the top row
        is value / pitch."""
        n = self.queue_sel.get_n_items()
        if not n or adj.get_upper() <= 0:
            return
        pitch = adj.get_upper() / n
        top = int(adj.get_value() / pitch)  # the top row on screen (maybe partly)
        gap = first
        if gap > 0 and isinstance(self.queue_sel.get_item(gap - 1), QueueAlbum):
            gap -= 1  # the album header may go with its tracks
        if os.environ.get("TOCCATA_DEBUG"):
            print(f"queue remove: gap at row {gap}, top row {top} (scroll {adj.get_value():.0f}, pitch {pitch:.1f}),"
                  f" {'kept in place' if top < gap else 'not above the gap'}", file=sys.stderr)
        if not (top < gap and top * pitch < adj.get_value() + adj.get_page_size()):
            return
        item = self.queue_sel.get_item(top)
        widget = item.widget if isinstance(item, QueueAlbum) else getattr(item, "row", None)
        if widget is None or not widget.get_mapped():
            return
        ok, b = widget.compute_bounds(self.queue_view)
        if not ok:
            return
        old_y, old_upper, frames = b.get_y(), adj.get_upper(), [0]

        def tick(_w, _clock):
            frames[0] += 1
            if frames[0] > 20:
                return GLib.SOURCE_REMOVE
            if adj.get_upper() == old_upper:
                return GLib.SOURCE_CONTINUE  # not laid out without the removed rows yet
            w = item.widget if isinstance(item, QueueAlbum) else getattr(item, "row", None)
            if w is None or not w.get_mapped():
                return GLib.SOURCE_REMOVE  # (the queue changed again meanwhile)
            ok, now = w.compute_bounds(self.queue_view)
            delta = now.get_y() - old_y if ok else 0
            if abs(delta) >= 1:
                if os.environ.get("TOCCATA_DEBUG"):
                    print(f"queue remove: rows above the gap moved {delta:+.0f} px, scrolled back", file=sys.stderr)
                self.queue_user_scrolled()  # not GTK's move: the pin must not undo it
                adj.set_value(adj.get_value() + delta)
            return GLib.SOURCE_REMOVE
        self.queue_view.add_tick_callback(tick)

    def _prepare_groups(self):
        """Work out album groups and which rows are hidden / summaries."""
        songs = self._queue_songs
        if not getattr(self, "_grouped", False):
            self._queue_groups = []
            _ungroup(songs)
            return
        heads = queuegroups.group_runs([s.data for s in songs])
        self.collapsed_groups = queuegroups.prune_collapsed(self.collapsed_groups, heads)
        # while filtering, collapsed albums are open, so every match can be seen
        self._queue_groups = _apply_groups(songs, set() if self._qfilter.active else self.collapsed_groups)

    def _regroup_visible(self):
        """After a collapse/expand: re-filter, and restyle rows and headers on screen."""
        self._queue_rows_changing()
        self._apply_queue_rows()  # header items get the new groups (or come / go with grouping)
        self._queue_filter.changed(Gtk.FilterChange.DIFFERENT)
        for song in self._queue_songs:
            if song.row is not None:
                song.row.refresh_texts()
        for g in self._queue_groups:
            header = getattr(g, "header", None)
            if header is not None:
                header.refresh()

    def toggle_group(self, group):
        if group.head_id in self.collapsed_groups:
            self.collapsed_groups.discard(group.head_id)
        else:
            self.collapsed_groups.add(group.head_id)
        self._prepare_groups()
        self._regroup_visible()

    def collapse_all(self, closed):
        self.collapsed_groups = {g.head_id for g in self._queue_groups} if closed else set()
        self._prepare_groups()
        self._regroup_visible()

    def queue_tags_at(self, pos):
        return self._queue_songs[pos].data if 0 <= pos < len(self._queue_songs) else None

    def queue_song_at(self, pos):
        return self._queue_songs[pos].uri if 0 <= pos < len(self._queue_songs) else None

    def _song_visible(self, song):
        """Shown in the queue list: matches the filter (when filtering), else not
        inside a collapsed album. An album header: while filtering only if one of its
        tracks matches."""
        if isinstance(song, QueueAlbum):
            g = song.group
            return not self._qfilter.active or any(
                self._qfilter.matches(s.data) for s in self._queue_songs[g.start:g.end])
        if self._qfilter.active:
            return self._qfilter.matches(song.data)
        return not song.hidden

    # -- centre on the playing track -----------------------------------------------------
    def center_queue_on_playing(self):
        """Scroll the queue so the playing (or paused) track is in the middle; a
        collapsed album it is in is expanded first. Selection and focus stay as they are."""
        songid, _state = now_playing(self.status or {})
        song = self._queue_by_id.get(songid) if songid is not None else None
        if song is None:
            self.show_toast("Nothing is playing")
            return
        if (song.group is not None and song.group.head_id in self.collapsed_groups
                and not self._qfilter.active):  # (while filtering, albums are open anyway)
            self.toggle_group(song.group)  # open its album, so the track itself can be shown
        n = self.queue_sel.get_n_items()
        idx = queuegroups.shown_row([self.queue_sel.get_item(i) for i in range(n)], song)
        if idx is None:
            self.show_toast("The playing track is hidden by the queue filter")
            return
        if not (self.queue_view.get_mapped() and self.queue_view.get_height() > 0):
            return  # scrolling an unallocated ListView trips a GTK assertion
        self.queue_user_scrolled()  # the user's own move: the top pin must not undo it
        self.queue_view.scroll_to(idx, Gtk.ListScrollFlags.NONE, None)  # brings it to an edge
        # Centre it once the list has laid the row out there. That can take a few
        # frames after a long jump (the row widget does not exist yet, and GTK's
        # height estimate changes as rows get measured), so: every frame, measure
        # and correct, until it stays in the middle (one try in an idle left the row
        # at the edge on the user's machine, 2026-11-13).
        tries = {"frames": 0, "still": 0}
        token = self._center_queue_token = object()  # a newer click takes over

        def tick(_w, _clock):
            if token is not self._center_queue_token:
                return GLib.SOURCE_REMOVE
            tries["frames"] += 1
            moved = self._center_queue_row(idx)
            tries["still"] = 0 if moved is not False else tries["still"] + 1
            done = moved is False and tries["still"] >= 3  # centred for 3 frames in a row
            if os.environ.get("TOCCATA_DEBUG"):
                adj = self.queue_view.get_vadjustment()
                print(f"queue centre: frame {tries['frames']} row {idx} "
                      f"{ {None: 'not laid out', True: 'moved', False: 'centred'}[moved]}, "
                      f"scroll {adj.get_value():.0f}/{adj.get_upper():.0f} page {adj.get_page_size():.0f}",
                      file=sys.stderr)
            return GLib.SOURCE_REMOVE if done or tries["frames"] >= 30 else GLib.SOURCE_CONTINUE
        self.queue_view.add_tick_callback(tick)

    def _center_queue_row(self, idx):
        """Move the row laid out for item `idx` towards the middle (rows differ in
        height when grouped, so this measures the row's widget). Returns None when
        the row is not laid out yet, True when it moved the list, False when the
        row is already as central as the list allows."""
        adj = self.queue_view.get_vadjustment()
        item = self.queue_sel.get_item(idx) if idx < self.queue_sel.get_n_items() else None
        widget = item.widget if isinstance(item, QueueAlbum) else getattr(item, "row", None)
        if adj is None or widget is None or not widget.get_mapped():
            return None
        ok, bounds = widget.compute_bounds(self.queue_view)
        if not ok:
            return None
        mid = bounds.get_y() + bounds.get_height() / 2
        target = adj.get_value() + mid - adj.get_page_size() / 2
        target = max(adj.get_lower(), min(target, adj.get_upper() - adj.get_page_size()))
        if abs(target - adj.get_value()) < 1:
            return False
        self.queue_user_scrolled()
        adj.set_value(target)
        return True

    # -- "Find in Play Queue" ------------------------------------------------------------
    def open_queue_filter(self):
        self.qfilter_bar.set_search_mode(True)
        self.qfilter_entry.grab_focus()

    def _on_qfilter_mode(self, *_):
        if not self.qfilter_bar.get_search_mode():
            self.qfilter_entry.set_text("")  # closing the bar ends the filter
            self._set_queue_filter("")

    def _set_queue_filter(self, text):
        new = queuefilter.parse(text)
        if new == self._qfilter:
            self._update_qfilter_count()
            return
        self._qfilter = new
        self._prepare_groups()
        self._regroup_visible()
        self._update_qfilter_count()

    def _update_qfilter_count(self):
        if not self._qfilter.active:
            self.qfilter_count.set_text("")
            return
        shown = sum(1 for s in self._queue_songs if self._qfilter.matches(s.data))
        self.qfilter_count.set_text(queuefilter.count_text(shown, len(self._queue_songs)))

    def select_group(self, group, extend=False):
        """Click on an album header: select its tracks (Ctrl adds to the selection).
        Its visible tracks are the rows right below its header row."""
        item = self._album_items.get(group.head_id)
        header = self._row_position(item) if item is not None else None
        count = sum(1 for s in self._queue_songs[group.start:group.end] if self._song_visible(s))
        if header is not None and count:
            _select_like_click(self.queue_view, header + 1, count, extend)

    def _queue_band_start(self, ctrl, _x, _y):
        sel = self.queue_sel
        self._band_base = sel.get_selection().copy() if ctrl else Gtk.Bitset.new_empty()
        self._band_pos = {}  # item -> position, once per band (rows are found by their item)
        for i in range(sel.get_n_items()):
            self._band_pos[id(sel.get_item(i))] = i
        self.queue_view.grab_focus()  # Delete etc. act on what gets selected

    def _queue_empty_click(self, ctrl, shift):
        """A plain click on the empty space below the last row clears the selection
        (user request 2026-11-12, as in the album list); with Ctrl or Shift it is kept."""
        if not (ctrl or shift):
            self.queue_sel.unselect_all()

    def _queue_band_moved(self, anchor, current, _ctrl):
        """The band moved: select the rows from the one under its start (the empty
        space below the last row: n) to the one under the pointer."""
        sel, offset = self.queue_sel, self.queue_view.get_vadjustment().get_value()
        n = sel.get_n_items()

        def position(content_y):
            widget = self._queue_row_at(content_y - offset)
            if widget is None:
                return n
            if isinstance(widget, QueueHeader):
                item = self._album_items.get(widget.group.head_id) if widget.group else None
            else:
                item = widget._song
            return self._band_pos.get(id(item), n)
        # the pointer may be above or below the view while it scrolls: take the edge row
        height = self.queue_view.get_height()
        current = min(max(current, offset), offset + max(height - 1, 0))
        rows = selection.band_positions(position(anchor), position(current), n)
        chosen = self._band_base.copy()
        if rows:
            chosen.add_range(rows[0], len(rows))
        sel.set_selection(chosen, Gtk.Bitset.new_range(0, n))

    def drag_group(self, group):
        if group is None:
            return None
        self._drag = ("queue", [s.data["id"] for s in self._queue_songs[group.start:group.end]])
        return self._tag(group.title or tracks_label([s.data for s in self._queue_songs[group.start:group.end]]))

    def _songs_for_row(self, song):
        """A row's own buttons act on the whole selection if the row is part of it."""
        selected = self._queue_selected()
        return selected if song in selected else self._entries_of(song)

    def edit_queue_song(self, song):
        self.edit_tags(self._songs_for_row(song))

    def remove_from_queue(self, song):
        self.remove_songs(self._songs_for_row(song))

    def remove_songs(self, songs):
        """Remove songs from the queue (Delete, the menus' Remove, a row's - button).
        Afterwards the row that moves up into the gap is selected and focused (on the
        refresh, see _queue_select_after). Without that, removing the row that had the
        keyboard focus (e.g. a clicked album header) let GTK move the focus to the
        first row and scroll the queue to the top (user report 2026-11-11)."""
        ids = [s.data["id"] for s in songs]
        if not ids:
            return
        wanted = set(map(id, songs))
        sel = self.queue_sel
        first = next((i for i in range(sel.get_n_items()) if id(sel.get_item(i)) in wanted), None)
        if first is not None:
            self._queue_select_after = first
        # one command list: one round trip, and the queue is refreshed once
        self.change_queue(lambda: self.client.command_list([("deleteid", i) for i in ids]) and None)

    def _on_queue_key(self, _ctrl, keyval, _code, state):
        """Delete removes the selected tracks (a collapsed album: all of its tracks)."""
        if keyval not in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete):
            return False
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SHIFT_MASK):
            return False
        songs = self._queue_selected()
        if not songs:
            return False
        self.remove_songs(songs)  # it selects what moves up into the gap: Delete can be pressed again
        return True

    # -- library ---------------------------------------------------------------------
    # Only the artist names are loaded up front. An artist's songs are fetched
    # when that artist is shown, in small pieces (see library.py for why).
    def _new_library_fetcher(self):
        f = LibraryFetcher(MPDClient(*self.cfg), _on_main_thread)
        f.composer_genres = tuple(self.settings.composer_genres)  # classical music under its composer
        return f

    def reload_library(self, fresh=True):
        """(Re)load the artist list; the artist on screen is reloaded with it.
        fresh: the database may have changed (also reloads the genre list);
        False for a mere genre switch."""
        self._lib_gen += 1
        gen = self._lib_gen
        self.artist_stack.set_visible_child_name("loading")
        self.library_fetcher.load_names(lambda names, err: self._names_loaded(gen, names, err),
                                        genre=self._genre, fresh=fresh)
        if fresh:
            self.library_fetcher.run(fetch_db_stamp, lambda stamp, err: (
                setattr(self, "_db_stamp", stamp) if gen == self._lib_gen and err is None else None))
            self._genre_gen = getattr(self, "_genre_gen", 0) + 1
            ggen = self._genre_gen
            self.library_fetcher.run(fetch_genres, lambda genres, err: self._genres_loaded(ggen, genres, err))

    # -- genre filter ("All Genres" drop-down) ------------------------------------------
    def _genres_loaded(self, gen, genres, err):
        if gen != self._genre_gen:  # a newer database reload (or a reconnect) superseded it
            return
        if err is not None:
            return  # keep what we had; the artist list reports the connection problem
        genres = genres or []
        self._genres = genres
        self._filling_genres = True
        try:
            self.genre_model.splice(0, self.genre_model.get_n_items(), ["All Genres"] + genres)
            self.genre_drop.set_selected(genres.index(self._genre) + 1 if self._genre in genres else 0)
        finally:
            self._filling_genres = False
        self.genre_drop.set_sensitive(bool(genres))
        if self._genre is not None and self._genre not in genres:
            self._set_genre(None)  # that genre is gone from the database

    def _on_genre_selected(self, *_):
        if self._filling_genres:
            return
        i = self.genre_drop.get_selected()
        self._set_genre(self._genres[i - 1] if 1 <= i <= len(self._genres) else None)

    def _set_genre(self, genre):
        if genre == self._genre:
            return
        self._genre = genre
        self.settings.genre = genre or ""
        self._schedule_flush()
        self._set_album_selection([])
        self.reload_library(fresh=False)  # keeps the selected artist if it has this genre

    def focus_genre_filter(self):
        self.views.set_visible_child_name("library")
        self.genre_drop.grab_focus()
        self.genre_drop.activate()  # opens the list (GtkDropDown::activate)

    def _names_loaded(self, gen, names, err):
        if gen != self._lib_gen:  # a newer load or a reconnect superseded this answer
            return
        if err is not None:
            self._set_artists([])
            self._show_status("Could not load the library", describe_error(err, *self.cfg[:2]))
            return
        if not names:
            self._set_artists([])
            if self._genre is not None:
                self._show_status("No artists", f"Nothing in the genre \u201c{self._genre}\u201d. "
                                                "Choose \u201cAll Genres\u201d to see everything.")
            else:
                self._show_status("The library is empty", "MPD has no songs in its database yet.")
            return
        names = sort_names(names, self.settings.ignore_prefixes)  # "The Beatles" under B
        previous = self._selected_artist
        target, self._update_target = getattr(self, "_update_target", None), None
        if os.environ.get("TOCCATA_DEBUG"):
            self._debug_art_watch()
            self._debug_artists("names loaded", f"{len(names)} names, previous={previous!r} target={target!r}")
        self._updating_artists = True  # we load the artist ourselves, once
        try:
            shown = self._set_artists(names)
            if target is not None and target[0] in shown:  # what a database update added
                previous = target[0]
                self._pending_album, self._pending_album_quiet = target[1], True
                self._remember_artist(previous)
            idx = shown.index(previous) if previous in shown else 0
            if shown:
                self.art_sel.set_selected(idx)
        finally:
            self._updating_artists = False
        if shown:
            GLib.timeout_add(300, self._scroll_to_artist, idx)
            self._load_artist(shown[idx])
        else:  # the filter hides every artist: still show the remembered one
            self._load_artist(previous if previous in names else names[0])

    def _scroll_to_artist(self, idx, tries=10):
        # only once the list has a real size; scrolling an unallocated ListView trips a GTK assertion
        if idx >= self.art_sel.get_n_items():
            return False
        if not self.art_view.get_mapped():
            self._artist_scroll_pending = True  # not shown: done in _on_art_view_map
            self._debug_artists("scroll deferred", f"idx={idx}: list not mapped")
            return False
        if self.art_view.get_height() <= 0:
            if tries > 0:  # mapped, not laid out yet
                GLib.timeout_add(50, self._scroll_to_artist, idx, tries - 1)
            return False
        self._artist_scroll_pending = False
        self._debug_artists("scroll_to", f"idx={idx}")
        self.art_view.scroll_to(idx, Gtk.ListScrollFlags.NONE, None)
        GLib.idle_add(self._center_artist, idx)  # once the list has laid out the new position
        return False

    def _on_art_view_map(self, _view):
        if self._artist_scroll_pending:
            idx = self.art_sel.get_selected()  # the selection now, not when it was deferred
            if idx != Gtk.INVALID_LIST_POSITION:
                GLib.timeout_add(50, self._scroll_to_artist, idx)

    def _center_artist(self, idx):
        """Put artist `idx` in the middle of the list (scroll_to only brings it to an
        edge). The rows all have the same height, so it follows from the scroll range."""
        adj, n = self.art_view.get_vadjustment(), self.art_sel.get_n_items()
        if adj is not None and n and idx < n:
            row = adj.get_upper() / n
            target = (idx + 0.5) * row - adj.get_page_size() / 2
            adj.set_value(max(adj.get_lower(), min(target, adj.get_upper() - adj.get_page_size())))
            self._debug_artists("centred", f"idx={idx} row={row:.1f} target={target:.0f}")
        return False

    # TOCCATA_DEBUG: what moves the artist list after a reload (user report 2026-11-27:
    # "the artists list still sometimes jumps to the bottom" after adding an album)
    def _debug_artists(self, tag, extra=""):
        if not os.environ.get("TOCCATA_DEBUG"):
            return
        adj, n = self.art_view.get_vadjustment(), self.art_sel.get_n_items()
        sel = self.art_sel.get_selected()
        print(f"artists {tag}: {extra} | selected={self._selected_artist!r} sel_idx={sel} n={n} "
              f"value={adj.get_value():.0f} upper={adj.get_upper():.0f} page={adj.get_page_size():.0f} "
              f"mapped={self.art_view.get_mapped()} h={self.art_view.get_height()} "
              f"view={self.views.get_visible_child_name()} stack={self.artist_stack.get_visible_child_name()}",
              file=sys.stderr, flush=True)

    def _debug_art_watch(self):
        """Log every scroll of the artist list for 10 s after a reload, with the Python
        caller (none: GTK moved it by itself)."""
        import time
        self._debug_art_until = time.monotonic() + 10
        adj = self.art_view.get_vadjustment()
        if adj is None or getattr(self, "_debug_art_adj", None) is adj:
            return
        self._debug_art_adj = adj

        def changed(a):
            if time.monotonic() > self._debug_art_until:
                return
            import traceback
            caller = [f"{f.name}:{f.lineno}" for f in traceback.extract_stack()[:-1]
                      if f.filename.endswith("app.py")][-3:]
            self._debug_artists("value-changed", f"by {caller or 'GTK'}")
        adj.connect("value-changed", changed)
        adj.connect("changed", lambda a: changed(a))  # upper / page size

    def _set_artists(self, names):
        """Every artist (of the genre); the list shows those the filter lets through,
        which are returned."""
        self._all_artists = list(names)
        shown = queuefilter.filter_names(names, self._artist_filter)
        self._show_artists(shown)
        self.artist_stack.set_visible_child_name("list")
        return shown

    def _show_artists(self, shown):
        previous = self._updating_artists  # may already be set by the caller
        self._updating_artists = True
        try:
            self.artist_store.splice(0, self.artist_store.get_n_items(), shown)
        finally:
            self._updating_artists = previous
        self._artist_matcher = Matcher(shown)
        self._update_afilter_count()

    # -- artist filter (Ctrl+F) -----------------------------------------------------------
    def open_artist_filter(self):
        self.views.set_visible_child_name("library")
        self.afilter_bar.set_search_mode(True)
        self.afilter_entry.grab_focus()

    def _on_afilter_mode(self, *_):
        if not self.afilter_bar.get_search_mode():
            self.afilter_entry.set_text("")  # closing the bar shows every artist again
            self._set_artist_filter("")

    def _set_artist_filter(self, text):
        old = queuefilter.filter_names(self._all_artists, self._artist_filter)
        self._artist_filter = text
        shown = queuefilter.filter_names(self._all_artists, text)
        if shown == old:
            return
        current = self._selected_artist
        self._updating_artists = True  # the selection set here is handled below
        try:
            self._show_artists(shown)
            idx = shown.index(current) if current in shown else 0
            if shown:
                self.art_sel.set_selected(idx)
        finally:
            self._updating_artists = False
        if not shown:
            return  # nothing matches: the albums of the last artist stay
        GLib.timeout_add(50, self._scroll_to_artist, idx)
        if shown[idx] != current:  # the shown artist was filtered out: show the first match
            self._remember_artist(shown[idx])
            self._load_artist(shown[idx])

    def _update_afilter_count(self):
        if len(" ".join(self._artist_filter.split())) < 2:
            self.afilter_count.set_text("")
        else:
            self.afilter_count.set_text(queuefilter.count_text(self.artist_store.get_n_items(),
                                                               len(self._all_artists)))

    def _focus_artist_list(self):
        """Enter in the filter: on to the list, at the selected artist."""
        idx = self.art_sel.get_selected()
        if idx != Gtk.INVALID_LIST_POSITION:
            self.art_view.scroll_to(idx, Gtk.ListScrollFlags.FOCUS, None)
        else:
            self.art_view.grab_focus()

    def _artist_index(self, name):
        """Position of `name` in the shown artist list; an artist hidden by the filter
        closes the filter first (Locate / Show in Artists). None if not in the library."""
        if name not in self._all_artists:
            return None
        names = self.library_artist_names(shown=True)
        if name not in names:
            self.afilter_bar.set_search_mode(False)  # -> _set_artist_filter("")
            names = self.library_artist_names(shown=True)
        return names.index(name) if name in names else None

    # -- type-ahead in the library (typeahead.py) -----------------------------------------
    def _on_library_key(self, _ctrl, keyval, _code, state):
        """Window key handler, capture phase: sees keys before the focused widget, so
        it must leave text fields, shortcuts and non-text keys alone itself."""
        if self._on_undo_key(keyval, state):
            return True
        view = self.views.get_visible_child_name()
        if keyval in (Gdk.KEY_a, Gdk.KEY_A) and state & Gdk.ModifierType.CONTROL_MASK \
                and not state & (Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SHIFT_MASK):
            return self._select_all(view)
        if view not in ("library", "playlists", "albums"):
            return False
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK):
            return False
        focus = self.get_focus()
        pane = {"library": self.library_split, "playlists": self.pl_split, "albums": self.albums_page}[view]
        if focus is not None and (isinstance(focus, Gtk.Editable) or not focus.is_ancestor(pane)):
            return False  # typing into a field, or working in the queue / player bar
        if view == "albums":
            ta, jump = self._albums_typeahead, self._jump_to_album_tile
            show = lambda text, found=True: _flash_typeahead(self.albums_typeahead_label, text, found,
                                                             self._albums_typeahead.timeout, "No album matches")
        elif view == "playlists":
            songs = focus is not None and focus.is_ancestor(self.pl_songs_scroll)
            ta = self._plsong_typeahead if songs else self._pl_typeahead
            jump = self._jump_to_playlist_song if songs else self._jump_to_playlist
            label = self.plsong_typeahead_label if songs else self.pl_typeahead_label
            show = lambda text, found=True, ta=ta, label=label, what="track" if songs else "playlist": \
                _flash_typeahead(label, text, found, ta.timeout, f"No {what} matches")
        else:
            albums = focus is not None and focus.is_ancestor(self.album_scroll)
            ta = self._album_typeahead if albums else self._typeahead
            jump = self._jump_to_album if albums else self._jump_to_artist
            show = self._show_album_typeahead if albums else self._show_typeahead
        now = time.monotonic()
        active = ta.active(now)
        if keyval == Gdk.KEY_Escape and active:
            ta.cancel()
            show(None)
            return True
        if keyval == Gdk.KEY_BackSpace and active:
            text = ta.backspace(now)
            show(text, jump(text) if text else True)
            return True
        char = chr(Gdk.keyval_to_unicode(keyval) or 0)
        if not char.isprintable() or char == "\x00" or (char == " " and not active):
            return False  # a space alone still presses the focused button / opens the album
        text, step = ta.type(char, now)
        found = jump(text, step=step)
        show(ta.text, found)
        return True

    def focus_album_list(self):
        focus = self.get_focus()
        if focus is not None and focus.is_ancestor(self.album_scroll):
            return  # already there (e.g. the clicked album took it)
        rows = self._album_rows()
        target = self._sel_anchor if self._sel_anchor in rows else (rows[0] if rows else None)
        if target is not None:
            (getattr(target, "header", None) or target).grab_focus()

    def _jump_to_album(self, text, step=False):
        rows = self._album_rows()
        current = rows.index(self._sel_anchor) if self._sel_anchor in rows else -1
        idx = self._album_matcher.find(text, current if step else -1)
        if idx is None or idx >= len(rows):
            return False
        row = rows[idx]
        self._set_album_selection([row], row)
        header = getattr(row, "header", None)
        (header or row).grab_focus()  # further keys stay in the album list
        self._scroll_album_into_view(row)
        return True

    def _scroll_album_into_view(self, row):
        """Bring the album's header into view. Gtk.Viewport.scroll_to (GTK >= 4.12)
        works the position out at layout time; positions read from rows here could be
        outdated right after a big jump (rows with long titles are measured again)."""
        target = getattr(row, "header", None) or row
        viewport = self.album_scroll.get_child()
        if hasattr(viewport, "scroll_to"):
            viewport.scroll_to(target, None)

    def _show_album_typeahead(self, text, found=True):
        if self._album_typeahead_hide:
            GLib.source_remove(self._album_typeahead_hide)
            self._album_typeahead_hide = 0
        label = self.album_typeahead_label
        if not text:
            label.set_visible(False)
            return
        label.set_text(text)
        (label.remove_css_class if found else label.add_css_class)("error")
        label.set_tooltip_text(None if found else "No album matches")
        label.set_visible(True)

        def done():
            self._album_typeahead_hide = 0
            label.set_visible(False)
            return False
        self._album_typeahead_hide = GLib.timeout_add(int(self._album_typeahead.timeout * 1000) + 200, done)

    def _jump_to_artist(self, text, step=False):
        current = self.art_sel.get_selected()
        after = current if step and current != Gtk.INVALID_LIST_POSITION else -1
        idx = self._artist_matcher.find(text, after)
        if idx is None:
            return False
        if idx != current:
            self.art_sel.set_selected(idx)  # loads the artist (on_artist_selected)
        self.art_view.scroll_to(idx, Gtk.ListScrollFlags.FOCUS, None)
        return True

    def _show_typeahead(self, text, found=True):
        if self._typeahead_hide:
            GLib.source_remove(self._typeahead_hide)
            self._typeahead_hide = 0
        if not text:
            self.typeahead_label.set_visible(False)
            return
        self.typeahead_label.set_text(text)
        (self.typeahead_label.remove_css_class if found else self.typeahead_label.add_css_class)("error")
        self.typeahead_label.set_tooltip_text(None if found else "No artist matches")
        self.typeahead_label.set_visible(True)
        self._typeahead_hide = GLib.timeout_add(int(self._typeahead.timeout * 1000) + 200, self._typeahead_done)

    def _typeahead_done(self):
        self._typeahead_hide = 0
        self.typeahead_label.set_visible(False)
        return False

    def _show_status(self, title, description):
        self._shown_artist = None
        self.lib_status.set_title(title)
        self.lib_status.set_description(description)
        self.album_stack.set_visible_child_name("status")

    def on_artist_selected(self, sel, *_):
        if self._updating_artists:
            return
        item = sel.get_selected_item()
        if item is None:
            return
        name = item.get_string()
        self._remember_artist(name)  # only the user's own choices are remembered
        self._load_artist(name)

    def _remember_artist(self, name):
        if name == self.settings.last_artist:
            return
        self.settings.last_artist = name
        self._schedule_flush()

    def _schedule_flush(self):
        if not self._flush_scheduled:  # write once, a moment after the last change
            self._flush_scheduled = True
            GLib.timeout_add_seconds(1, self._flush_settings)

    def _flush_settings(self):
        self._flush_scheduled = False
        try:
            save_settings(self.settings)
        except OSError:
            pass  # losing the remembered layout is not worth interrupting the user
        return False

    def _load_artist(self, name):
        """Show an artist's albums: instantly if cached, else fetch them."""
        self._selected_artist = name
        self._shown_artist = None  # the buttons below do nothing until the albums are there
        self._album_gen += 1
        gen = self._album_gen
        cached = self.library_fetcher.cached(name, self._genre)
        if cached is not None:
            self._show_artist(cached)
            return
        self.album_stack.set_visible_child_name("loading")
        self.library_fetcher.load_artist(
            name, lambda artist, err: self._artist_loaded(gen, artist, err),
            wanted=lambda: gen == self._album_gen, genre=self._genre)

    def _artist_loaded(self, gen, artist, err):
        if gen != self._album_gen:  # the user has moved on to another artist
            return
        if err is not None:
            self._show_status("Could not load albums", describe_error(err, *self.cfg[:2]))
            return
        self._show_artist(artist)

    def _show_artist(self, artist):
        """Fill the right-hand pane with this artist's albums."""
        self._album_gen += 1  # invalidates cover requests of whatever was shown before
        gen = self._album_gen
        self.album_list.remove_all()
        self._album_spacer.set_size_request(-1, 0)
        self._sel_tracks, self._track_anchor = [], None
        self._set_album_selection([])
        if not artist.albums:
            self._show_status(artist.name, "No songs found for this artist.")
            return
        self._shown_artist = artist
        for album in artist.albums:
            self.album_list.append(self._make_album_row(album, gen))
        self._album_matcher = Matcher([a.title for a in artist.albums])
        self._albums_near = set()
        self.album_stack.set_visible_child_name("albums")
        self.album_scroll.get_vadjustment().set_value(0)
        self._schedule_album_covers()
        if getattr(self, "_pending_album", None):
            GLib.idle_add(lambda: (self._select_pending_album(), False)[1])

    # -- album covers: only for albums on screen ------------------------------------------
    def _schedule_album_covers(self, *_):
        """Ask for the covers of the albums in view (and a screen's worth around),
        a moment after scrolling or resizing settles."""
        if not getattr(self, "_album_cover_timer", 0):
            self._album_cover_timer = GLib.timeout_add(60, self._load_visible_album_covers)

    def _load_visible_album_covers(self):
        self._album_cover_timer = 0
        adj = self.album_scroll.get_vadjustment()
        page = adj.get_page_size() or self.album_scroll.get_height()
        if page <= 0:  # not laid out yet: try again shortly
            self._schedule_album_covers()
            return False
        top, bottom = adj.get_value() - page, adj.get_value() + 2 * page
        gen = self._album_gen
        near = set()  # rows near the view now; a queued request for any other row is dropped
        todo = []
        for row in self._album_rows():
            ok, p = row.compute_point(self._albums_box, Graphene.Point().init(0, 0))
            if not ok:
                continue
            if p.y > bottom:
                break  # rows are in order: the rest is further down
            if p.y + row.get_height() < top:
                continue
            near.add(id(row))
            if getattr(row, "cover_wanted", False):
                todo.append(row)
        self._albums_near = near  # replaced whole, so the cover thread can read it safely
        for row in todo:
            row.cover_wanted = False
            self.cover_loader.request(
                row.album.cover_uri,
                lambda tex, t=row.thumb: t.set_texture(tex) if gen == self._album_gen else None,
                wanted=lambda r=row: self._album_cover_still_wanted(r, gen),
                song=row.album.tracks[0], size=THUMB_SIZE)
        return False

    def _album_cover_still_wanted(self, row, gen):
        """Called on the cover thread just before fetching: skip albums that were
        scrolled away meanwhile (a fast scroll passes hundreds), and fetch them again
        when they come back into view. Reads only plain Python state."""
        if gen != self._album_gen:
            return False
        if id(row) in self._albums_near:
            return True
        row.cover_wanted = True  # not now; asked again when it is near the view
        return False

    def _make_album_row(self, album, gen):
        parts = [str(album.year) if album.year else "",
                 f"{album.n_tracks} track" + ("" if album.n_tracks == 1 else "s"),
                 fmt_time(album.duration) if album.duration else ""]
        row = Adw.ExpanderRow(title=GLib.markup_escape_text(album.title),
                              subtitle=GLib.markup_escape_text(" \u00b7 ".join(p for p in parts if p)))

        row.album = album  # the selection buttons read it back
        drag = DragSource(actions=Gdk.DragAction.COPY)
        drag.connect("prepare", lambda _s, x, y, r=row: self._drag_album(r, x, y))
        row.add_controller(drag)
        # A single click selects, a double click opens/closes the album. The ExpanderRow's
        # inner header row would open it on every click, so it is made non-activatable
        # and the gesture sits on the header only (not on the track rows below it).
        header = row.header = _expander_header(row)
        if header is not None:
            header.set_activatable(False)
            click = Gtk.GestureClick()
            click.connect("pressed", self._on_album_pressed, row)
            click.connect("released", self._on_album_released, row)
            header.add_controller(click)
            keys = Gtk.EventControllerKey()
            keys.connect("key-pressed", self._on_album_key, row)
            header.add_controller(keys)
            _right_click(header, lambda x, y, r=row: self._album_menu(r, x, y))
        thumb = CoverThumb(ALBUM_COVER_SIZE)
        row.add_prefix(thumb)
        row.thumb = thumb
        row.cover_wanted = bool(self.show_covers and album.cover_uri)  # fetched once it scrolls into view
        if not row.cover_wanted:
            thumb.set_visible(False)

        # Play / Add to Queue buttons, shown only while the pointer is over the album's
        # header (CSS .album-header:hover; user request 2026-11-16). Always-visible row
        # buttons were too busy (user request 2026-11-11). They act on this album only.
        # Opacity, not visibility: the title keeps its width when they appear.
        if header is not None:
            header.add_css_class("album-header")
        row.hover_buttons = []  # [Play, Add], left to right
        for icon, tip, action in (  # ExpanderRow.add_suffix puts each new one leftmost
                ("list-add-symbolic", "Add to queue", lambda: self.add_tracks(album.tracks, album.title)),
                ("media-playback-start-symbolic", "Play", lambda: self.play_tracks(album.tracks))):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip, valign=Gtk.Align.CENTER)
            b.add_css_class("flat")
            b.add_css_class("album-hover-btn")
            b.connect("clicked", lambda _b, action=action: action())
            row.add_suffix(b)
            row.hover_buttons.insert(0, b)

        # Track rows are only built when the album is first opened.
        state = {"filled": False}
        row.connect("notify::expanded", lambda r, _p: self._fill_album_row(r, album, state))
        row.connect("notify::expanded", lambda r, _p: None if r.get_expanded() else self._album_collapsing())
        row.connect("notify::expanded", lambda r, _p: None if r.get_expanded() else self._drop_hidden_tracks(r))
        return row

    def _album_collapsing(self):
        """Keep the view still while an album closes: give the list enough room
        below, so GTK does not have to scroll up. The space is only taken away
        again as the user scrolls (_trim_album_spacer), so nothing moves by itself."""
        page = int(self.album_scroll.get_vadjustment().get_page_size())
        current = self._album_spacer.get_size_request()[1]
        self._album_spacer.set_size_request(-1, max(page, current, 0))

    def _trim_album_spacer(self):
        """On scrolling: shrink the blank space to what keeps the current view in
        place (never grow it), so it disappears as the user scrolls up."""
        have = self._album_spacer.get_height()
        if have <= 0 and self._album_spacer.get_size_request()[1] <= 0:
            return
        adj = self.album_scroll.get_vadjustment()
        needed = int(adj.get_value() + adj.get_page_size() - self.album_list.get_height()
                     - self.album_list.get_margin_top() - self.album_list.get_margin_bottom())
        current = self._album_spacer.get_size_request()[1]
        new = max(0, min(current, needed))
        if new != current:
            self._album_spacer.set_size_request(-1, new)

    def _fill_album_row(self, row, album, state):
        if state["filled"] or not row.get_expanded():
            return
        state["filled"] = True
        multi_disc = len({int_prefix(t.get("disc")) for t in album.tracks}) > 1
        row.track_rows = []
        for track in album.tracks:
            track_row = self._make_track_row(track, album, multi_disc)
            row.track_rows.append(track_row)
            row.add_row(track_row)

    def _make_track_row(self, track, album, multi_disc):
        song = Song(track)
        artist = track.get("artist", "")
        row = Adw.ActionRow(title=GLib.markup_escape_text(song.title),
                            subtitle=GLib.markup_escape_text(artist if artist and artist != album.artist else ""))
        number = int_prefix(track.get("track"))
        label = (f"{int_prefix(track.get('disc'))}-{number:02d}" if multi_disc else f"{number}") if number else "\u2013"
        num = Gtk.Label(label=label, width_chars=4)
        num.add_css_class("dim-label")
        num.add_css_class("numeric")
        row.add_prefix(num)
        dur = Gtk.Label(label=song.duration)
        dur.add_css_class("dim-label")
        dur.add_css_class("numeric")
        row.add_suffix(dur)
        for icon, tip, action in (
                ("list-add-symbolic", "Add to queue", lambda: self.add_tracks([track], song.title)),
                ("document-edit-symbolic", "Edit tags", lambda: self.edit_tracks([track]))):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip, valign=Gtk.Align.CENTER)
            b.add_css_class("flat")
            b.connect("clicked", lambda _b, action=action: action())
            row.add_suffix(b)
        # selectable like the queue: click, Ctrl+click, Shift+click; double click adds
        row.track = track
        click = Gtk.GestureClick()
        click.connect("pressed", self._on_track_pressed, row)
        click.connect("released", self._on_track_released, row)
        row.add_controller(click)
        drag = DragSource(actions=Gdk.DragAction.COPY)
        drag.connect("prepare", lambda _s, _x, _y, r=row: self._drag_tracks(r))
        row.add_controller(drag)
        _right_click(row, lambda x, y, r=row: self._track_row_menu(r, x, y))
        return row

    # -- tracks selected inside opened albums --------------------------------------------
    def _visible_track_rows(self):
        return [t for r in self._album_rows() if r.get_expanded() for t in getattr(r, "track_rows", [])]

    def _on_track_pressed(self, gesture, n_press, x, y, row):
        picked = gesture.get_widget().pick(x, y, Gtk.PickFlags.DEFAULT)
        if picked is not None and (isinstance(picked, Gtk.Button) or picked.get_ancestor(Gtk.Button) is not None):
            return  # the row's own + and edit buttons
        mods = gesture.get_current_event_state()
        ctrl, shift = bool(mods & Gdk.ModifierType.CONTROL_MASK), bool(mods & Gdk.ModifierType.SHIFT_MASK)
        self._pending_click = None
        if n_press == 1 and selection.defer_to_release(self._sel_tracks, row, ctrl, shift):
            self._pending_click = row  # keep the selection for a drag; select on release
            return
        self.track_click(row, n_press, ctrl, shift)

    def _on_track_released(self, _gesture, _n_press, _x, _y, row):
        if self._pending_click is row:
            self._pending_click = None
            self.track_click(row, 1)

    def track_click(self, row, n_press, ctrl=False, shift=False):
        if n_press >= 2:
            if not (ctrl or shift):  # double click: add (the selection, if this row is in it)
                rows = self._sel_tracks if row in self._sel_tracks else [row]
                self.add_tracks([r.track for r in rows])
            return
        rows, anchor = selection.click(self._sel_tracks, self._track_anchor, row,
                                       self._visible_track_rows(), ctrl, shift)
        self._set_track_selection(rows, anchor)

    def _set_track_selection(self, rows, anchor=None):
        for r in self._sel_tracks:
            r.remove_css_class("track-selected")
        self._sel_tracks = list(rows)
        for r in self._sel_tracks:
            r.add_css_class("track-selected")
        self._track_anchor = anchor if rows else None
        if rows and self._sel_albums:
            self._set_album_selection([])  # one kind of selection at a time
        else:
            self._update_library_buttons()

    def _drop_hidden_tracks(self, album_row):
        """An album closed: its tracks cannot stay selected out of sight."""
        hidden = set(map(id, getattr(album_row, "track_rows", [])))
        if any(id(r) in hidden for r in self._sel_tracks):
            self._set_track_selection([r for r in self._sel_tracks if id(r) not in hidden], self._track_anchor)

    def _drag_tracks(self, row):
        self._pending_click = None  # a drag keeps the selection
        rows = [r for r in self._visible_track_rows() if r in self._sel_tracks] if row in self._sel_tracks else [row]
        self._drag = ("tracks", [r.track for r in rows])
        return self._tag(tracks_label(self._drag[1]))

    def _update_library_buttons(self):
        n = len(self._sel_tracks)
        if n:
            self.add_all.set_label(f"Add {n} tracks to queue" if n > 1 else "Add track to queue")
            self.edit_all.set_label(f"Edit tags of {n} tracks" if n > 1 else "Edit tags of track")
            return
        n = len(self._sel_albums)
        self.add_all.set_label(f"Add {n} albums to queue" if n > 1 else "Add album to queue" if n else "Add all to queue")
        self.edit_all.set_label(f"Edit tags of {n} albums" if n > 1 else "Edit tags of album" if n else "Edit tags for all")

    def add_tracks(self, tracks, name=None):
        uris = [t["file"] for t in tracks]
        if self.change_queue(lambda: self.client.add_many(uris) or True):
            n = len(uris)
            self.show_toast(f"Added \u201c{name}\u201d" if name
                            else f"Added {n} track" + ("" if n == 1 else "s"))

    def play_tracks(self, tracks):
        uris = [t["file"] for t in tracks]
        self.change_queue(lambda: self.client.replace_queue(uris, play=True))

    def edit_tracks(self, tracks):
        self.edit_tags([Song(t) for t in tracks])

    def _album_rows(self):
        rows, row = [], self.album_list.get_first_child()
        while row is not None:
            rows.append(row)
            row = row.get_next_sibling()
        return rows

    def _selected_album_tracks(self):
        """Selected tracks, else the tracks of the selected albums (in list order);
        None if nothing is selected."""
        if self._sel_tracks:
            return [r.track for r in self._visible_track_rows() if r in self._sel_tracks]
        if not self._sel_albums:
            return None
        return [t for r in self._album_rows() if r in self._sel_albums for t in r.album.tracks]

    def _select_all(self, view):
        """Ctrl+A: the whole queue, or every album of the artist in the Artists view."""
        focus = self.get_focus()
        target = selection.select_all_target(
            view, isinstance(focus, Gtk.Editable),
            focus is not None and (focus is self.queue_view or focus.is_ancestor(self.queue_view)),
            focus is None or focus.is_ancestor(self.library_split))
        if target == "queue":
            self.queue_sel.select_all()
            return True
        if target == "albums":
            rows = self._album_rows()
            if rows:
                self._set_album_selection(rows, rows[0])
            return True
        return False

    def _set_album_selection(self, rows, anchor=None):
        for r in self._sel_albums:
            r.remove_css_class("album-selected")
        self._sel_albums = list(rows)
        for r in self._sel_albums:
            r.add_css_class("album-selected")
        self._sel_anchor = anchor if rows else None
        if rows and self._sel_tracks:
            self._set_track_selection([])  # one kind of selection at a time (updates the buttons)
        else:
            self._update_library_buttons()

    def _on_album_pressed(self, gesture, n_press, x, y, row):
        picked = gesture.get_widget().pick(x, y, Gtk.PickFlags.DEFAULT)
        if picked is not None and (isinstance(picked, Gtk.Button) or picked.get_ancestor(Gtk.Button) is not None):
            return  # buttons handle their own clicks
        mods = gesture.get_current_event_state()
        ctrl, shift = bool(mods & Gdk.ModifierType.CONTROL_MASK), bool(mods & Gdk.ModifierType.SHIFT_MASK)
        self._pending_click = None
        if n_press == 1 and selection.defer_to_release(self._sel_albums, row, ctrl, shift):
            self._pending_click = row  # keep the selection for a drag; select on release
            return
        self.album_click(row, n_press, ctrl, shift)

    def _on_album_released(self, _gesture, _n_press, _x, _y, row):
        if self._pending_click is row:
            self._pending_click = None
            self.album_click(row, 1)

    def _album_band_start(self, ctrl, _x, _y):
        self._band_albums = list(self._sel_albums) if ctrl else []
        self._band_tracks = list(self._sel_tracks) if ctrl else []
        self._pending_click = None
        self.focus_album_list()

    def _album_empty_click(self, ctrl, shift):
        """A plain click on empty space of the album list clears the selection (user
        request 2026-11-12, like file managers); with Ctrl or Shift it is kept."""
        if ctrl or shift:
            return
        self._pending_click = None
        if self._sel_tracks:
            self._set_track_selection([])
        if self._sel_albums:
            self._set_album_selection([])

    def _album_band_moved(self, anchor, current, ctrl):
        """The band moved: select the album headers it touches, or else the tracks
        of opened albums. Positions are in the scrolled box's coordinates."""
        def span(widget):
            ok, b = widget.compute_bounds(self._albums_box)
            return (b.get_y(), b.get_y() + b.get_height()) if ok else (-1.0, -1.0)
        rows = self._album_rows()
        tracks = self._visible_track_rows()
        kind, hits = selection.band_targets([span(getattr(r, "header", None) or r) for r in rows],
                                            [span(t) for t in tracks], anchor, current)
        upward = current < anchor
        if kind == "albums":
            chosen = [rows[i] for i in hits]
            merged = selection.band_merge(self._band_albums, chosen, rows, ctrl)
            if merged != self._sel_albums:
                self._set_album_selection(merged, chosen[-1] if upward else chosen[0])
            return
        chosen = [tracks[i] for i in hits]
        merged = selection.band_merge(self._band_tracks, chosen, tracks, ctrl)
        if not merged:
            merged = selection.band_merge(self._band_albums, [], rows, ctrl)
            if merged != self._sel_albums or self._sel_tracks:
                self._set_track_selection([])
                self._set_album_selection(merged, merged[0] if merged else None)
            return
        if merged != self._sel_tracks:
            self._set_track_selection(merged, (chosen[-1] if upward else chosen[0]) if chosen else merged[0])

    def _on_album_key(self, _ctrl, keyval, _code, state, row):
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter, Gdk.KEY_space, Gdk.KEY_KP_Space) \
                and not state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            row.set_expanded(not row.get_expanded())  # the keyboard way to open/close
            return True
        return False

    def album_click(self, row, n_press, ctrl=False, shift=False):
        """Click on an album header: select (plain), toggle (Ctrl), range (Shift);
        the second click of a double click opens or closes the album."""
        if n_press >= 2:
            if not (ctrl or shift):
                row.set_expanded(not row.get_expanded())
            return
        if not (ctrl or shift):
            self._set_album_selection([row], row)
            return
        if shift and self._sel_anchor is not None:
            rows = self._album_rows()
            if self._sel_anchor in rows:
                i, j = rows.index(self._sel_anchor), rows.index(row)
                self._set_album_selection(rows[min(i, j):max(i, j) + 1], self._sel_anchor)
                return
        current = list(self._sel_albums)
        if row in current:
            current.remove(row)
        else:
            current.append(row)
        self._set_album_selection(current, row)

    def on_add_all(self, _btn):
        tracks = self._selected_album_tracks()
        if tracks is None and self._shown_artist is not None:
            tracks = self._shown_artist.tracks()
        if tracks:
            self.add_tracks(tracks)

    def on_edit_all(self, _btn):
        tracks = self._selected_album_tracks()
        if tracks is None and self._shown_artist is not None:
            tracks = self._shown_artist.tracks()
        if tracks:
            self.edit_tracks(tracks)

    # -- tag editing --------------------------------------------------------------------
    def edit_tags(self, songs):
        if not songs:
            return
        try:
            paths = [taglib.resolve_path(self.music_dir, s.uri) for s in songs]
            merged = taglib.merge_for_edit([taglib.read_tags(p) for p in paths])
        except taglib.TagError as e:
            self.show_toast(str(e))
            return
        TagEditor(self, songs, paths, merged).present(self)

    def after_tag_write(self, songs, changed, errors):
        if errors:
            self.show_toast(errors[0] + (f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""))
        if changed and len(errors) < len(songs):
            # Ask MPD to rescan so queue/library show the new tags. They are reloaded
            # when MPD reports the update finished (`update` idle event, see on_idle).
            started = [s for s in songs if self.safe(lambda u=s.uri: self.client.update(u) or True)]
            if started:
                self._db_gate.changed(queue_too=True)
            self.show_toast("Tags saved; MPD is rescanning")


class App(Adw.Application):
    def __init__(self, args):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.args = args
        self.saved = load_settings()

    def do_startup(self):
        Adw.Application.do_startup(self)
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self._quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Primary>q"])
        self.set_accels_for_action("win.preferences", ["<Primary>comma"])
        self.set_accels_for_action("win.search", ["<Primary>f"])
        self.set_accels_for_action("win.genre", ["<Primary>g"])
        self.set_accels_for_action("win.queue-filter", ["<Primary><Shift>f"])
        self.set_accels_for_action("win.queue-center", ["<Primary>l"])
        self.set_accels_for_action("win.info", ["<Primary>i"])
        # A left click on a slider (seek bar, volume) jumps to the clicked position,
        # even when ~/.config/gtk-4.0/settings.ini says otherwise (there a click only
        # pages 10 s towards the pointer). Affects this app only.
        Gtk.Settings.get_default().set_property("gtk-primary-button-warps-slider", True)
        # The app and tray icons ship in data/icons, so they work without installing.
        Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(ICON_DIR)
        Gtk.Window.set_default_icon_name(APP_ID)
        css = Gtk.CssProvider()
        css.load_from_string((".song-row { padding: 8px 12px; }"
                              # queue track rows and album header rows: the same height, so
                              # GTK's estimate of the list's height is exact (QueueAlbum)
                              " .queue-row { min-height: 40px; }"
                              # compact queue: one line per row, small buttons; still one
                              # height for tracks and album headers (COMPACT_COVER_SIZE covers)
                              " .queue-compact .queue-row { min-height: 20px; padding-top: 3px; padding-bottom: 3px; }"
                              " .queue-compact .queue-row button { min-height: 18px; min-width: 18px;"
                              " padding: 0 4px; margin: 0; }"
                              " .compact-cover { border-radius: 4px; }"
                              # the text beside the pointer while dragging (DragSource)
                              " .drag-label { padding: 4px 10px; border-radius: 8px;"
                              " background-color: @popover_bg_color; color: @popover_fg_color;"
                              " box-shadow: 0 1px 4px alpha(black, 0.3); }"
                              " .queue-drop-line { min-height: 2px; background-color: @accent_bg_color; }"
                              " .info-lyrics { line-height: 1.4; }"  # Info view: synced lyrics
                              " button.info-track { font-weight: normal; padding: 2px 6px; }"
                             " .song-row.now-playing { background-color: alpha(@accent_bg_color, 0.18); }"
                             " .album-list, .album-list > row { background: none; }"
                             " .album-list > row.album-selected { background-color: alpha(@accent_bg_color, 0.30); }"
                             " .album-list row.track-selected { background-color: alpha(@accent_bg_color, 0.30); }"
                             " .typeahead { padding: 6px 14px; border-radius: 9px; }"
                             # Artists view: an album's Play / Add buttons only on hover
                             " .album-hover-btn { opacity: 0; }"
                             " .album-header:hover .album-hover-btn,"
                             # keyboard focus on one of the buttons shows both; focus on the
                             # header itself does not (a click on the pane's empty space
                             # focuses the first header for type-ahead, user report 2026-11-27)
                             " .album-header :focus-within > .album-hover-btn,"
                             # Albums view: the same on a tile's cover
                             " .album-tile:hover .album-hover-btn,"
                             # Playlists view: the same on a grouped album's header
                             " .queue-header:hover .album-hover-btn,"
                             " .queue-header:focus-within .album-hover-btn,"
                             " .album-tile:focus-within .album-hover-btn { opacity: 1; }"))
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _quit(self):
        windows = self.get_windows()
        if windows and isinstance(windows[0], Window):
            windows[0].request_quit()  # closes properly: settings are saved, D-Bus names released
        else:
            self.quit()

    def do_activate(self):
        a = self.args
        cli = dict(cli_host=a.host, cli_port=a.port, cli_password=a.password, cli_music_dir=a.music_dir)
        existing = self.get_windows()  # may be hidden in the tray
        if existing and isinstance(existing[0], Window):
            existing[0].show_window()
            return
        win = Window(self, self.saved, cli, show_covers=not a.no_covers,
                                                       highlight=not a.no_highlight)
        win.present()


def main(argv=None):
    p = argparse.ArgumentParser(
        description="GTK4 MPD client with tag editing. Connection details are normally set in "
                    "Preferences; the options below override them for this run.")
    p.add_argument("--host", help="MPD host or socket path (also: MPD_HOST, may be 'password@host')")
    p.add_argument("--port", type=int, help="MPD port (also: MPD_PORT)")
    p.add_argument("--password", help="MPD password (visible to other users in the process list; "
                                      "prefer Preferences or MPD_HOST)")
    p.add_argument("--music-dir", help="MPD music directory (also: TOCCATA_MUSIC_DIR)")
    p.add_argument("--no-covers", action="store_true", help="do not fetch or show cover art")
    p.add_argument("--no-highlight", action="store_true", help="do not highlight the playing queue entry")
    args, rest = p.parse_known_args(argv if argv is not None else sys.argv[1:])
    return App(args).run([sys.argv[0]] + rest)


if __name__ == "__main__":
    sys.exit(main())
