"""The Info view (like Cantata's context view): three columns about the song in use.

Left: the album artist (Wikipedia, similar artists via Last.fm, the artist's albums in
the library). Middle: the album (cover, Wikipedia, its tracks). Right: lyrics, synced
ones follow the song. Lookups run on worker threads (info.Lookup) and are only made
while the view is shown; results come back through GLib.idle_add and are dropped if
the song changed meanwhile.
"""
import os
import threading
import time
import urllib.parse

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Graphene, Gtk, Pango  # noqa: E402

from . import info  # noqa: E402
from . import lastfm  # noqa: E402
from . import tags as taglib  # noqa: E402

CACHE_DIR = os.path.join(GLib.get_user_cache_dir(), "toccata", "info")
FOLLOW_PAUSE = 5.0  # s: after the user scrolls the lyrics, they are not moved for this long


def _label(text="", css=(), wrap=True, selectable=False, xalign=0.0):
    lbl = Gtk.Label(label=text, xalign=xalign, wrap=wrap, selectable=selectable)
    if wrap:
        lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    for c in css:
        lbl.add_css_class(c)
    return lbl


def _accent_hex():
    """The desktop's accent colour as #rrggbb (libadwaita >= 1.6), or None."""
    try:
        c = Adw.StyleManager.get_default().get_accent_color_rgba()
    except (AttributeError, TypeError):
        return None
    return "#{:02x}{:02x}{:02x}".format(*(round(v * 255) for v in (c.red, c.green, c.blue)))


def _clear(box):
    child = box.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


def _fmt_time(seconds):
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    return f"{s // 60}:{s % 60:02d}"


class Column(Gtk.ScrolledWindow):
    """One scrolling column with a vertical box inside."""

    def __init__(self):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER, hexpand=True, vexpand=True)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                           margin_top=18, margin_bottom=18, margin_start=18, margin_end=18)
        self.set_child(self.box)


class InfoView(Gtk.Box):
    def __init__(self, window, cover_widget):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, homogeneous=True)
        self.add_css_class("info-view")
        self._win = window
        self._song = {}
        self._keys = {"artist": None, "album": None, "lyrics": None}
        self._gen = {"artist": 0, "similar": 0, "album": 0, "lyrics": 0}
        self._synced = []          # [(seconds, line)] of the shown lyrics, if synced
        self.lyrics_label = None   # synced lyrics: one selectable label, current line in markup
        self._lyrics_dragging = False
        self._scroll_pending = False  # the current line still has to be scrolled into view
        self._current_line = -2
        self._user_scrolled = 0.0
        self._follow_id = 0
        self._cache = info.Cache(CACHE_DIR)
        self.get = info.get_text  # network access (tests put canned answers here)
        self.sleep = time.sleep   # pauses between MusicBrainz retries (tests skip them)

        # -- left: the artist
        self.artist_col = Column()
        b = self.artist_col.box
        self.artist_title = _label(css=("title-1",))
        self.artist_wiki = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.similar_head = _label("Similar artists", css=("heading",))
        # one line of text: "A, B, C"; artists in the library are links (activate-link)
        self.similar_box = _label()
        self.similar_box.connect("activate-link", self._on_similar_link)
        self.similar_note = _label(css=("dim-label", "caption"))
        self.albums_head = _label("Albums in your library", css=("heading",))
        self.albums_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for w in (self.artist_title, self.artist_wiki, self.similar_head, self.similar_box, self.similar_note,
                  self.albums_head, self.albums_box):
            b.append(w)

        # -- middle: the album
        self.album_col = Column()
        b = self.album_col.box
        self.cover = cover_widget
        self.cover.set_halign(Gtk.Align.CENTER)
        self.album_title = _label(css=("title-2",), xalign=0.5)
        self.album_sub = _label(css=("dim-label",), xalign=0.5)
        self.album_wiki = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.tracks_head = _label("Tracks", css=("heading",))
        self.tracks_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        for w in (self.cover, self.album_title, self.album_sub, self.album_wiki, self.tracks_head, self.tracks_box):
            b.append(w)

        # -- right: lyrics
        self.lyrics_col = Column()
        b = self.lyrics_col.box
        self.lyrics_title = _label(css=("title-2",))
        self.lyrics_source = _label(css=("dim-label", "caption"))
        self.lyrics_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for w in (self.lyrics_title, self.lyrics_source, self.lyrics_box):
            b.append(w)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_user_scroll)
        self.lyrics_col.add_controller(scroll)
        drag = Gtk.GestureDrag()  # dragging the scrollbar counts as the user's own scrolling
        drag.connect("drag-begin", lambda *_: self._on_user_scroll())
        self.lyrics_col.get_vscrollbar().add_controller(drag)

        for col in (self.artist_col, self.album_col, self.lyrics_col):
            self.append(col)
        self.connect("map", lambda *_: self.refresh())
        self.connect("unmap", lambda *_: self._stop_follow())

    # -- entry points (main thread) ------------------------------------------------------
    def show_song(self, song):
        """The song in use changed (or the status was refreshed)."""
        self._song = dict(song or {})
        if self.get_mapped():
            self.refresh()

    def set_cover(self, texture):
        self.cover.set_texture(texture)
        self.cover.set_visible(texture is not None)

    def refresh(self):
        song = self._song
        artist = info.song_artist(song)
        album_key = (artist, song.get("album", ""))
        if not song:
            self._show_nothing()
            return
        if self._keys["artist"] != artist:
            self._keys["artist"] = artist
            self._load_artist(song)
        if self._keys["album"] != album_key:
            self._keys["album"] = album_key
            self._load_album(song)
        else:
            self._mark_current_track()
        if self._keys["lyrics"] != song.get("file"):
            self._keys["lyrics"] = song.get("file")
            self._load_lyrics(song)
        self._start_follow()

    def _show_nothing(self):
        self._keys = {"artist": None, "album": None, "lyrics": None}
        for k in self._gen:
            self._gen[k] += 1
        self.artist_title.set_text("Nothing playing")
        for box in (self.artist_wiki, self.albums_box, self.album_wiki, self.tracks_box, self.lyrics_box):
            _clear(box)
        self.similar_box.set_text("")
        for w in (self.similar_head, self.similar_box, self.similar_note, self.albums_head, self.tracks_head):
            w.set_visible(False)
        self.album_title.set_text("")
        self.album_sub.set_text("")
        self.lyrics_title.set_text("")
        self.lyrics_source.set_text("")
        self._synced = []
        self._stop_follow()

    # -- worker plumbing ---------------------------------------------------------------
    def _lookup(self):
        s = self._win.settings
        return info.Lookup(get=self.get, cache=self._cache, lastfm_key=lastfm.effective_key(s.lastfm_api_key),  # English Wikipedia
                           sleep=self.sleep)

    def _run(self, part, fn, done):
        """fn() on a worker thread, done(result, error) on the main thread, dropped if
        the part was asked for again meanwhile."""
        self._gen[part] += 1
        gen = self._gen[part]

        def work():
            try:
                result, error = fn(), None
            except Exception as e:  # network down, bad answer, ...: shown, not raised
                result, error = None, e
            GLib.idle_add(lambda: (self._gen[part] == gen and done(result, error), False)[1])
        threading.Thread(target=work, daemon=True, name=f"info-{part}").start()

    # -- artist ------------------------------------------------------------------------
    def _load_artist(self, song):
        artist = info.song_artist(song)
        self.artist_title.set_text(artist or "Unknown artist")
        self._wiki_status(self.artist_wiki, "Looking for an article on Wikipedia…")
        self.similar_box.set_text("")
        self.similar_note.set_text("")
        _clear(self.albums_box)
        for w in (self.similar_head, self.similar_box, self.similar_note, self.albums_head):
            w.set_visible(True)  # (hidden while nothing was playing)
        look = self._lookup()
        song = dict(song)

        def fn():
            return look.artist_wiki(song) if artist else {"wiki": None, "mbid": None}
        self._run("artist", fn, self._artist_done)
        # Last.fm on its own thread: a busy MusicBrainz (retries, then an error) must
        # neither delay nor hide the similar artists
        self._run("similar", lambda: look.similar(artist) if artist else None, self._show_similar)
        self._load_library_albums(artist)

    def _artist_done(self, result, error):
        if error is not None:
            self._wiki_error(self.artist_wiki, error, lambda: self._load_artist(self._song))
            return
        self._song_artist_mbid = result.get("mbid")
        self._show_wiki(self.artist_wiki, result.get("wiki"), "No Wikipedia article found for this artist.")

    def _show_similar(self, names, error):
        self.similar_box.set_text("")
        if error is not None:
            # Limited says itself that it is Last.fm and when it is asked again
            self.similar_note.set_text(str(error) if isinstance(error, lastfm.Limited) else f"Last.fm: {error}")
            return
        if names is None:
            self.similar_note.set_text("Similar artists come from Last.fm and need your Last.fm API key "
                                       "(Preferences → Cover Search).")
            return
        if not names:
            self.similar_note.set_text("Last.fm knows no similar artists.")
            return
        self.similar_box.set_markup(info.similar_markup(names, self._win.library_artist_names()))
        self.similar_note.set_text("From Last.fm. Artists in your library are links.")

    def _on_similar_link(self, _label, uri):
        if not uri.startswith(info.SIMILAR_LINK):
            return False  # (not ours: let GTK open it)
        self._win.info_show_artist(urllib.parse.unquote(uri[len(info.SIMILAR_LINK):]))
        return True

    def _load_library_albums(self, artist):
        _clear(self.albums_box)
        if not artist:
            return
        self._win.info_load_artist(artist, lambda art, err, a=artist: self._library_albums_done(a, art, err))

    def _library_albums_done(self, artist, art, err):
        if artist != self._keys["artist"]:
            return
        _clear(self.albums_box)
        if err is not None or art is None or not art.albums:
            self.albums_box.append(_label("None found in the library.", css=("dim-label",)))
            self._library_artist = None
        else:
            self._library_artist = art
            for album in art.albums:
                row = Gtk.Button(halign=Gtk.Align.FILL, tooltip_text="Show in Artists")
                row.add_css_class("flat")
                text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                text.append(_label(album.title, wrap=True))
                bits = [str(album.year) if album.year else "", f"{album.n_tracks} tracks"]
                text.append(_label(" · ".join(b for b in bits if b), css=("dim-label", "caption")))
                row.set_child(text)
                row.connect("clicked", lambda _b, t=album.tracks[0]: self._win.info_show_song_album(t))
                self.albums_box.append(row)
        self._fill_tracks()  # the middle column takes the album's tracks from here

    # -- album -------------------------------------------------------------------------
    def _load_album(self, song):
        self.album_title.set_text(song.get("album") or "Unknown album")
        bits = [info.song_artist(song), (song.get("date") or "")[:4]]
        self.album_sub.set_text(" · ".join(b for b in bits if b))
        self._wiki_status(self.album_wiki, "Looking for an article on Wikipedia…")
        self.tracks_head.set_visible(True)
        self._fill_tracks()
        look = self._lookup()
        song = dict(song)
        if not song.get("album"):
            self._show_wiki(self.album_wiki, None, "No album name in the tags.")
            return

        def fn():
            return look.album_wiki(song, getattr(self, "_song_artist_mbid", None))
        self._run("album", fn, self._album_done)

    def _album_done(self, result, error):
        if error is not None:
            self._wiki_error(self.album_wiki, error, lambda: self._load_album(self._song))
            return
        self._show_wiki(self.album_wiki, result.get("wiki"), "No Wikipedia article found for this album.")

    def _fill_tracks(self):
        _clear(self.tracks_box)
        self._track_rows = []
        art = getattr(self, "_library_artist", None)
        album = self._song.get("album", "")
        tracks = []
        if art is not None and info.song_artist(self._song) == self._keys["artist"]:
            for a in art.albums:
                if a.title == album:
                    tracks = a.tracks
                    break
        if not tracks:
            self.tracks_box.append(_label("Loading…" if art is None else "Not found in the library.",
                                          css=("dim-label",)))
            return
        for t in tracks:
            row = Gtk.Box(spacing=10, margin_top=3, margin_bottom=3)
            num = _label(str(t.get("track", "")).split("/")[0], css=("dim-label", "numeric"), wrap=False, xalign=1.0)
            num.set_width_chars(3)
            title = _label(t.get("title") or os.path.basename(t.get("file", "")), wrap=True)
            title.set_hexpand(True)
            dur = _label(_fmt_time(t.get("duration") or t.get("time")), css=("dim-label", "numeric"), wrap=False)
            for w in (num, title, dur):
                row.append(w)
            # a click plays the track, adding it to the queue first if needed (as in Cantata)
            btn = Gtk.Button(child=row, tooltip_text="Play (added to the queue if it is not in it)")
            btn.add_css_class("flat")
            btn.add_css_class("info-track")  # (normal weight: only the playing track is bold)
            btn.connect("clicked", lambda _b, track=t: self._win.info_play_track(track))
            btn.file = t.get("file")
            btn.title_label = title
            self._track_rows.append(btn)
            self.tracks_box.append(btn)
        self._mark_current_track()

    def _mark_current_track(self):
        current = self._song.get("file")
        for row in getattr(self, "_track_rows", []):
            on = row.file == current
            for css in ("accent", "heading"):
                (row.title_label.add_css_class if on else row.title_label.remove_css_class)(css)

    # -- wiki text ---------------------------------------------------------------------
    def _wiki_status(self, box, text):
        _clear(box)
        box.append(_label(text, css=("dim-label",)))

    def _wiki_error(self, box, error, retry):
        """A lookup failed: say why, and offer to try again."""
        if isinstance(error, info.Busy):
            text = "MusicBrainz (used to find the Wikipedia article) is busy right now."
        else:
            text = f"Could not reach MusicBrainz / Wikipedia: {error}"
        self._wiki_status(box, text)
        btn = Gtk.Button(label="Retry", halign=Gtk.Align.START)
        btn.connect("clicked", lambda _b: retry())
        box.append(btn)

    def _show_wiki(self, box, wiki, missing):
        _clear(box)
        if not wiki:
            box.append(_label(missing, css=("dim-label",)))
            return
        # the article's lead section in one label: selectable across its paragraphs
        box.append(_label("\n\n".join(wiki["paragraphs"]), selectable=True))
        link = Gtk.LinkButton(uri=wiki["url"], label=f"Read more on Wikipedia: {wiki['title']}",
                              halign=Gtk.Align.START)
        box.append(link)

    # -- lyrics ------------------------------------------------------------------------
    def _load_lyrics(self, song):
        self.lyrics_title.set_text(song.get("title") or os.path.basename(song.get("file", "")))
        self.lyrics_source.set_text("Looking for lyrics…")
        _clear(self.lyrics_box)
        self._synced, self.lyrics_label, self._current_line = [], None, -2
        look = self._lookup()
        song = dict(song)
        music_dir = self._win.music_dir

        def fn():
            uri = song.get("file", "")
            path = taglib.resolve_path(music_dir, uri) if music_dir and uri and "://" not in uri else None
            if path and os.path.exists(path):
                text = taglib.read_lyrics(path)
                if text:
                    return info.lyrics_result(text, "the file's tags")
                text, name = info.sidecar_lyrics(path)
                if text:
                    return info.lyrics_result(text, name)
            return look.online_lyrics(song)
        self._run("lyrics", fn, self._lyrics_done)

    def _lyrics_done(self, result, error):
        _clear(self.lyrics_box)
        if error is not None:
            self.lyrics_source.set_text(f"Could not reach LRCLIB: {error}")
            return
        if not result:
            self.lyrics_source.set_text("No lyrics found (file tags, .lrc next to the file, LRCLIB).")
            return
        synced = [tuple(x) for x in result.get("synced") or []]
        self.lyrics_source.set_text(f"From {result['source']}" + (" · synced" if synced else ""))
        if synced:
            self._synced = synced
            self._lines = [line or "♪" for _t, line in synced]
            self.lyrics_label = _label("\n".join(self._lines), selectable=True)
            self.lyrics_label.add_css_class("info-lyrics")
            drag = Gtk.GestureDrag()  # while the user drags out a selection, the markup stays put
            drag.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            drag.connect("drag-begin", lambda *_: setattr(self, "_lyrics_dragging", True))
            drag.connect("drag-end", lambda *_: setattr(self, "_lyrics_dragging", False))
            self.lyrics_label.add_controller(drag)
            self.lyrics_box.append(self.lyrics_label)
            self._current_line = -2
            self._follow()
        else:  # plain lyrics: one label, selectable as a whole
            self.lyrics_box.append(_label(result["plain"], selectable=True))

    # following synced lyrics
    def _start_follow(self):
        if not self._follow_id:
            self._follow_id = GLib.timeout_add(250, self._follow)

    def _stop_follow(self):
        if self._follow_id:
            GLib.source_remove(self._follow_id)
            self._follow_id = 0

    def _on_user_scroll(self, *_):
        self._user_scrolled = time.monotonic()
        return False

    def _follow(self):
        if not self._synced or not self.get_mapped():
            return True
        idx = info.current_line(self._synced, self._win.elapsed())
        if idx != self._current_line:
            if self._lyrics_dragging:
                return True  # the user is selecting: change the highlight after the drag
            self._current_line = idx
            self._render_lyrics()
            self._scroll_pending = 0 <= idx < len(self._lines)
        # (also retried on later ticks: right after the lyrics arrive the label is not laid
        # out yet, and when the view opens mid-song the line would otherwise stay out of view)
        if self._scroll_pending and time.monotonic() - self._user_scrolled > FOLLOW_PAUSE:
            self._scroll_pending = not self._scroll_lyrics_to(self._current_line)
        return True

    def _render_lyrics(self):
        """The lyrics with the current line bold in the accent colour. The user's
        selection must survive the new markup: GTK 4.24 keeps it (same text); it is put
        back here too (same offsets) in case a GTK version clears it."""
        lbl, idx = self.lyrics_label, self._current_line
        got = lbl.get_selection_bounds()
        sel = got[1:] if len(got) == 3 and got[0] else (got if len(got) == 2 else None)
        colour = _accent_hex()
        parts = []
        for i, line in enumerate(self._lines):
            text = GLib.markup_escape_text(line)
            if i == idx:
                attrs = ' weight="bold"' + (f' foreground="{colour}"' if colour else "")
                text = f"<span{attrs}>{text}</span>"
            parts.append(text)
        lbl.set_markup("\n".join(parts))
        if sel and sel[0] != sel[1]:
            lbl.select_region(*sel)

    def current_lyric(self):
        """The highlighted lyrics line (for tests), or None."""
        return self._lines[self._current_line] if self.lyrics_label and 0 <= self._current_line < len(self._lines) else None

    def _scroll_lyrics_to(self, idx):
        """Put lyrics line `idx` in the middle of the lyrics column. False if that cannot
        be done yet (not laid out)."""
        lbl = self.lyrics_label
        if lbl is None or lbl.get_height() <= 0:
            return False
        byte = sum(len(line.encode("utf-8")) + 1 for line in self._lines[:idx])
        pos = lbl.get_layout().index_to_pos(byte)
        ox, oy = lbl.get_layout_offsets()
        y = oy + pos.y / Pango.SCALE
        ok, p = lbl.compute_point(self.lyrics_col.box, Graphene.Point().init(0, y))
        if not ok:
            return False
        adj = self.lyrics_col.get_vadjustment()
        if adj.get_upper() < self.lyrics_col.box.get_height():
            return False  # the scroll range does not know the lyrics' height yet
        target = p.y + pos.height / Pango.SCALE / 2 - adj.get_page_size() / 2
        adj.set_value(max(adj.get_lower(), min(target, adj.get_upper() - adj.get_page_size())))
        return True
