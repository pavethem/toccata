"""The Albums view: every album, sortable, without loading the whole library.

Two sources, both gentle on MPD (measured on a 186,000-song server):

* Artist / Title / Year: `list album group albumartist group date` (one answer,
  ~870 KB for 16,000 albums, 0.2 s), plus the same for songs without an album
  artist (grouped by artist).
* Recently added: MPD's `Added` time (MPD >= 0.24; older servers: Last-Modified).
  Sorting the whole library by it costs MPD ~1 s per request, so it is never
  done: the library is walked in time slices (last 30 days, 30-90 days ago, ...),
  each fetched sorted with `find (slice) sort -Added window ...`, CHUNK songs at a
  time, only as far as the user scrolls. Each request costs MPD tens of ms.

An album is (artist, album): album artist, else artist, as in the library.
No GTK here.
"""
import datetime
from dataclasses import dataclass

from .library import KEEP, UNKNOWN, collate_key, filter_quote, int_prefix, name_sort_key, parse_year
from .mpd_client import CommandError

CHUNK = 1000  # songs per request while walking "recently added"
# slice boundaries in days ago; after the last one everything older follows
SLICES = (30, 90, 180, 365, 730, 1095, 1460, 1825, 2555, 3650, 5475, 7300)
RECENT_KEEP = frozenset({"file", "album", "albumartist", "artist", "date", "added", "last-modified",
                         "musicbrainz_albumid"})

SORTS = (("added", "Recently added"), ("artist", "Artist"), ("title", "Title"), ("year", "Year"))


@dataclass(eq=False)
class AlbumEntry:
    artist: str          # album artist, else artist ("Unknown" if neither)
    album: str
    year: int = 0
    added: str = ""      # ISO time the album's newest track was added ("" if unknown)
    uri: str = ""        # one of its songs (for the cover); "" until known
    untagged: bool = False  # the songs have no album artist (artist used instead)

    @property
    def key(self):
        return (self.artist, self.album)


# -- all albums (for sorting by artist / title / year) -------------------------------

def parse_album_list(pairs, untagged=False):
    """Entries from `list album group <artist tag> group date`. MPD prints a
    group value only when it changes, so the current values are carried along."""
    out, artist, date = [], "", ""
    for k, v in pairs:
        k = k.lower()
        if k == "date":
            date = v
        elif k in ("albumartist", "artist"):
            artist = v
        elif k == "album" and v:
            out.append(AlbumEntry(artist or UNKNOWN, v, parse_year(date), untagged=untagged))
    return out


def merge_albums(entries):
    """One entry per (artist, album); an album listed under several dates keeps
    the earliest year."""
    by_key = {}
    for e in entries:
        old = by_key.get(e.key)
        if old is None:
            by_key[e.key] = e
        elif e.year and (not old.year or e.year < old.year):
            old.year = e.year
    return list(by_key.values())


def fetch_all_albums(client):
    entries = parse_album_list(client.command("list", "album", "group", "albumartist", "group", "date"))
    # albums of songs without an album artist: grouped by their artist (MPD >= 0.21)
    entries = [e for e in entries if e.artist != UNKNOWN]
    try:
        entries += parse_album_list(client.command("list", "album", "(AlbumArtist == '')",
                                                   "group", "artist", "group", "date"), untagged=True)
    except CommandError:
        pass
    return merge_albums(entries)


def sort_albums(entries, how, prefixes=()):
    """prefixes: words like "The" that are ignored at the start of artist and album
    names ("The Beatles" sorts as "Beatles, The")."""
    artist = lambda e: name_sort_key(e.artist, prefixes)
    album = lambda e: name_sort_key(e.album, prefixes)
    if how == "title":
        key = lambda e: (album(e), artist(e))
    elif how == "year":  # newest first; unknown year last
        key = lambda e: (-e.year if e.year else 1, artist(e), album(e))
    elif how == "added":
        return sorted(entries, key=lambda e: e.added, reverse=True)
    else:  # artist
        key = lambda e: (artist(e), e.year or 9999, album(e))
    return sorted(entries, key=key)


# -- one album's songs ------------------------------------------------------------------

def album_filter(entry):
    """MPD filter for the album's songs. For an untagged entry it also matches
    the artist's songs that do have an album artist: drop those with
    `_own_songs`. (MPD 0.24 matches a missing AlbumArtist against the Artist,
    so "(AlbumArtist == '')" finds no song that has an artist.)"""
    if entry.untagged:
        who = f"(Artist == {filter_quote(entry.artist)})"
    elif entry.artist == UNKNOWN:
        who = "(AlbumArtist == '') AND (Artist == '')"
    else:
        who = f"(AlbumArtist == {filter_quote(entry.artist)})"
    return f"({who} AND (Album == {filter_quote(entry.album)}))"


def _own_songs(entry, songs):
    return [s for s in songs if not s.get("albumartist")] if entry.untagged else songs


def fetch_album_uri(client, entry):
    """One song of the album (for its cover), or "". One small request."""
    if entry.untagged:  # the first match may belong to a compilation: look at them all
        songs = list(client.iter_songs("find", album_filter(entry), keep={"file", "albumartist"}))
    else:
        songs = list(client.iter_songs("find", album_filter(entry), "window", "0:1", keep={"file"}))
    songs = _own_songs(entry, songs)
    return songs[0]["file"] if songs else ""


def fetch_album_tracks(client, entry):
    songs = _own_songs(entry, list(client.iter_songs("find", album_filter(entry), keep=KEEP)))
    songs.sort(key=lambda s: (int_prefix(s.get("disc")), int_prefix(s.get("track")), s.get("file", "")))
    return songs


# -- recently added -------------------------------------------------------------------------

def _iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def slice_filters(field, now):
    """[(filter, label)] newest first: one filter per time slice, then the rest."""
    since = "added-since" if field == "added" else "modified-since"
    bounds = [_iso(now - datetime.timedelta(days=d)) for d in SLICES]
    out, newer = [], None
    for b in bounds:
        part = f"({since} {filter_quote(b)})"
        out.append(part if newer is None else f"({part} AND (!({since} {filter_quote(newer)})))")
        newer = b
    out.append(f"(!({since} {filter_quote(newer)}))")
    return out


def song_album(song):
    artist = song.get("albumartist") or song.get("artist") or UNKNOWN
    return AlbumEntry(artist, song.get("album") or "", parse_year(song.get("date")),
                      song.get("added") or song.get("last-modified") or "", song.get("file", ""),
                      untagged=not song.get("albumartist") and bool(song.get("artist")))


class RecentAlbums:
    """Walks the library newest first, one request per load_more() call.
    field: "added" (MPD >= 0.24) or "modified" (Last-Modified, older servers)."""

    def __init__(self, field="added", now=None, chunk=CHUNK):
        self.field = field
        self.chunk = chunk
        self.filters = slice_filters(field, now or datetime.datetime.now(datetime.timezone.utc))
        self.slice = 0
        self.offset = 0
        self.seen = set()
        self.done = False

    def load_more(self, client):
        """New albums (newest first) from the next window of songs; [] and
        `done` when the whole library has been walked."""
        sort = "-Added" if self.field == "added" else "-Last-Modified"
        while not self.done:
            songs = list(client.iter_songs("find", self.filters[self.slice], "sort", sort, "window",
                                           f"{self.offset}:{self.offset + self.chunk}", keep=RECENT_KEEP))
            if len(songs) < self.chunk:  # this slice is finished
                self.slice += 1
                self.offset = 0
                self.done = self.slice >= len(self.filters)
            else:
                self.offset += self.chunk
            new = []
            for s in songs:
                e = song_album(s)
                if e.album and e.key not in self.seen:
                    self.seen.add(e.key)
                    new.append(e)
            if new:
                return new
        return []


def recent_field(protocol_version):
    """Which time to sort "recently added" by on this server."""
    from .search import parse_version
    return "added" if parse_version(protocol_version) >= (0, 24, 0) else "modified"
