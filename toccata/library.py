"""Library model: artists -> albums -> tracks, built from MPD's song list.

Grouping and sorting:
  * the artist of a song is its *album artist*, falling back to its artist
    when that tag is empty;
  * artists sort by the AlbumArtistSort tag if present, else by name;
  * an album is identified by its MusicBrainz album id, else its name,
    within one artist;
  * the album's year comes from its first track (lowest disc/track);
  * comparisons are case-insensitive, ignore punctuation and spaces, treat
    digit runs as numbers ("Band 2" < "Band 10");
  * albums with no year sort last; accents are ignored at the first
    comparison level, approximating ICU collation.

Songs are plain dicts with lowercase MPD tag names ('albumartist', 'track', ...).

Loading is lazy. Asking MPD for every song at once (`listallinfo`) overruns
MPD's output buffer (max_output_buffer_size, 8 MiB by default) on big
libraries. So only the artist *names* are fetched up front (a few hundred KB);
one artist's songs are fetched when that artist is shown, in windows of
CHUNK songs so no single answer gets large. See fetch_artist_names/fetch_artist.
"""
import os
import re
import time
import unicodedata
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .mpd_client import CommandError, MPDError

UNKNOWN = "Unknown"

# Tags kept per song when loading; everything else MPD sends is dropped.
KEEP = frozenset({"title", "artist", "albumartist", "album", "track", "disc", "date",
                  "genre", "composer", "duration", "time", "albumartistsort",
                  "albumsort", "musicbrainz_albumid"})

_NUMBER = re.compile(r"([0-9]+)")
_YEAR = re.compile(r"(?<![0-9])([0-9]{4})(?![0-9])")
_LEADING_INT = re.compile(r"\s*([0-9]+)")


def _tokens(text):
    """Split into comparable tokens: numbers as numbers, text as text."""
    out = []
    for part in _NUMBER.split(text):
        if part == "":
            continue
        out.append((0, int(part), "") if _NUMBER.fullmatch(part) else (1, 0, part))
    return tuple(out)


def collate_key(text):
    """Sort key approximating a locale collator (numeric, ignore punctuation,
    case-insensitive). Accents only matter when everything else is equal."""
    text = text or ""
    folded = text.casefold()
    primary = "".join(
        ch for ch in unicodedata.normalize("NFKD", folded)
        if unicodedata.category(ch)[0] not in ("P", "Z", "C") and unicodedata.category(ch) != "Mn")
    return (_tokens(primary), _tokens(folded), text)


DEFAULT_PREFIXES = ("The",)
MAX_PREFIXES = 20


def strip_prefix(text, prefixes):
    """"The Beatles" -> "Beatles, The" for sorting, if "The" is one of `prefixes`
    (a prefix counts only as a whole word followed by a space; case does not matter)."""
    text = text or ""
    for p in prefixes:
        n = len(p)
        if p and len(text) > n + 1 and text[n] == " " and text[:n].casefold() == p.casefold():
            return f"{text[n + 1:]}, {text[:n]}"
    return text


def name_sort_key(text, prefixes=()):
    return collate_key(strip_prefix(text, prefixes))


def sort_names(names, prefixes=()):
    """Artist names in display order, prefixes like "The" ignored."""
    return sorted(names, key=lambda n: name_sort_key(n, prefixes))


def _parse_list(text, max_len, max_items):
    """A comma-separated Preferences field -> tuple: trimmed, no empty entries,
    no duplicates (ignoring case), entries longer than max_len dropped."""
    out = []
    for part in (text or "").split(","):
        p = " ".join(part.split())
        if p and len(p) <= max_len and p.casefold() not in (x.casefold() for x in out):
            out.append(p)
    return tuple(out[:max_items])


def parse_prefixes(text):
    """The Preferences field ("The, Die, Les") -> a tuple of prefixes (short words)."""
    return _parse_list(text, 30, MAX_PREFIXES)


def parse_genres(text):
    """The composer-genres field -> a tuple of genre names. Genre names can be long
    ("Contemporary Avant-Garde Classical"), so only absurd lengths are dropped."""
    return _parse_list(text, 500, 100)


def int_prefix(value):
    """'3' -> 3, '3/12' -> 3, '' / 'x' -> 0."""
    m = _LEADING_INT.match(value or "")
    return int(m.group(1)) if m else 0


def parse_year(date):
    """'2019-05-03' -> 2019, '(1999)' -> 1999, '' -> 0."""
    m = _YEAR.search(date or "")
    return int(m.group(1)) if m else 0


def _duration(song):
    """Length in seconds: 'duration' (MPD >= 0.20) else the older 'time'."""
    for key in ("duration", "time"):
        try:
            value = float(song.get(key) or 0)
        except ValueError:
            continue
        if value > 0:
            return value
    return 0.0


def track_key(song):
    title = song.get("title") or os.path.basename(song.get("file", ""))
    return (int_prefix(song.get("disc")), int_prefix(song.get("track")),
            collate_key(title), song.get("file", ""))


@dataclass
class Album:
    artist: str
    title: str
    album_id: str
    year: int
    sort_title: str
    tracks: list = field(default_factory=list)  # song dicts, in track order

    @property
    def cover_uri(self):
        return self.tracks[0]["file"] if self.tracks else None

    @property
    def n_tracks(self):
        return len(self.tracks)

    @property
    def duration(self):
        return sum(_duration(t) for t in self.tracks)

    @property
    def sort_key(self):
        return (self.year == 0, self.year, collate_key(self.sort_title), self.album_id)


@dataclass
class Artist:
    name: str
    sort_name: str
    albums: list = field(default_factory=list)  # sorted

    @property
    def sort_key(self):
        return (collate_key(self.sort_name or self.name), self.name)

    def tracks(self):
        """Every track, in album order then track order."""
        return [t for album in self.albums for t in album.tracks]


class Library:
    def __init__(self, artists, n_songs):
        self.artists = artists  # sorted
        self.n_songs = n_songs
        self._by_name = {a.name: a for a in artists}

    def artist(self, name):
        return self._by_name.get(name)

    @classmethod
    def from_songs(cls, songs, group_as=None):
        """Index songs. group_as: file every song under this artist name
        regardless of its tags (used when the server already selected them)."""
        # name -> {"sort": str, "albums": {album_id: {"title", "sort", "songs"}}}
        groups, n = {}, 0
        for song in songs:
            n += 1
            name = group_as or song.get("albumartist") or song.get("artist") or UNKNOWN
            g = groups.setdefault(name, {"sort": "", "albums": {}})
            if not g["sort"]:
                g["sort"] = song.get("albumartistsort", "")
            album_name = song.get("album", "")
            album_id = song.get("musicbrainz_albumid") or album_name
            a = g["albums"].setdefault(album_id, {"title": "", "sort": "", "songs": []})
            if not a["title"] and album_name:
                a["title"] = album_name
            if not a["sort"]:
                a["sort"] = song.get("albumsort", "")
            a["songs"].append(song)

        artists = []
        for name, g in groups.items():
            albums = []
            for album_id, a in g["albums"].items():
                tracks = sorted(a["songs"], key=track_key)
                year = next((y for y in (parse_year(t.get("date")) for t in tracks) if y), 0)
                title = a["title"] or UNKNOWN
                albums.append(Album(name, title, album_id, year, a["sort"] or title, tracks))
            albums.sort(key=lambda al: al.sort_key)
            artists.append(Artist(name, g["sort"], albums))
        artists.sort(key=lambda ar: ar.sort_key)
        return cls(artists, n)


CHUNK = 1000  # songs per `find` answer: ~350 KB, far below MPD's 8 MiB output buffer

# MPD filter expressions (MPD >= 0.21). Comparing with '' matches songs that
# lack the tag.
NO_ALBUM_ARTIST = "(AlbumArtist == '')"
NO_ARTIST_AT_ALL = "((AlbumArtist == '') AND (Artist == ''))"


def filter_quote(text):
    """Quote a string for use inside an MPD filter expression."""
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def untagged_album_artist_filter(artist):
    return f"((AlbumArtist == '') AND (Artist == {filter_quote(artist)}))"


def count_songs(client, expression):
    """Number of songs matching a filter, or None if the server cannot say
    (e.g. MPD older than 0.21, which has no filter expressions)."""
    try:
        return int(client.command_dict("count", expression).get("songs", 0))
    except CommandError:
        return None


def genre_filter(genre):
    """MPD filter for one genre; matches songs that have it among several genres."""
    return f"(Genre == {filter_quote(genre)})"


def _and(*expressions):
    """Combine filter expressions: '(A)', '(B)' -> '((A) AND (B))' (MPD >= 0.21)."""
    return expressions[0] if len(expressions) == 1 else "(" + " AND ".join(expressions) + ")"


def fetch_genres(client):
    """All genres (the "All Genres" filter), sorted; empty values left out."""
    return sorted({v for k, v in client.command("list", "genre") if k.lower() == "genre" and v.strip()},
                  key=collate_key)


# -- composer genres (Cantata's "albumArtistOrComposer") -----------------------------
# For songs of these genres that have a composer, the library artist is the composer
# instead of the album artist (classical music filed under Bach, not the orchestra).

def resolve_genres(available, wanted):
    """The server's spelling of the wanted genres (case-insensitive), in its order."""
    want = {w.casefold() for w in wanted}
    return tuple(g for g in available if g.casefold() in want)


def composer_song_filter(genre):
    return f"((Genre == {filter_quote(genre)}) AND (Composer != ''))"


def not_composer_songs(genres):
    """Songs filed under their album artist: in none of the composer genres with a
    composer. MPD filters have no OR, so: NOT(g1 AND c) AND NOT(g2 AND c) ..."""
    return _and(*[f"(!{composer_song_filter(g)})" for g in genres])


def _fetch_artist_names_composers(client, genre, composer_genres):
    extra = () if genre is None else (genre_filter(genre),)
    rest = not_composer_songs(composer_genres)
    names = {v for k, v in client.command("list", "albumartist", _and(rest, *extra))
             if k.lower() == "albumartist" and v}
    names.update(v for k, v in client.command("list", "artist", _and(NO_ALBUM_ARTIST, rest, *extra))
                 if k.lower() == "artist" and v)
    if count_songs(client, _and(NO_ARTIST_AT_ALL, rest, *extra)):
        names.add(UNKNOWN)
    for g in composer_genres:
        names.update(v for k, v in client.command("list", "composer", _and(composer_song_filter(g), *extra))
                     if k.lower() == "composer" and v)
    return sorted(names, key=collate_key)


def fetch_artist_names(client, genre=None, composer_genres=()):
    """All library artists (album artist, else artist), sorted;
    with `genre`, only those that have songs of that genre.

    Union of three small queries, so it is right whether or not this MPD falls
    back from AlbumArtist to Artist on its own:
      1. every album artist;
      2. artists of songs that have no album artist (needs MPD >= 0.21);
      3. 'Unknown', if some songs have neither (same requirement).
    Queries 2 and 3 are skipped quietly on servers that reject them. With a
    genre, query 1 uses a filter expression, or the old `genre X` syntax on
    MPD < 0.21. With composer_genres (the server's spelling, see
    resolve_genres) those genres' songs with a composer count under the
    composer instead (needs MPD >= 0.21).
    """
    if composer_genres:
        return _fetch_artist_names_composers(client, genre, composer_genres)
    if genre is None:
        reply = client.command("list", "albumartist")
    else:
        try:
            reply = client.command("list", "albumartist", genre_filter(genre))
        except CommandError:
            reply = client.command("list", "albumartist", "genre", genre)  # MPD < 0.21
    names = {v for k, v in reply if k.lower() == "albumartist" and v}
    extra = () if genre is None else (genre_filter(genre),)
    try:
        names.update(v for k, v in client.command("list", "artist", _and(NO_ALBUM_ARTIST, *extra))
                     if k.lower() == "artist" and v)
    except CommandError:
        pass
    if count_songs(client, _and(NO_ARTIST_AT_ALL, *extra)):
        names.add(UNKNOWN)
    return sorted(names, key=collate_key)


def _find_all(client, *args, chunk=CHUNK):
    """Run `find` in windows of `chunk` songs and return every song.
    Servers without `window` support (MPD < 0.20) get one plain call."""
    songs, start = [], 0
    while True:
        try:
            batch = list(client.iter_songs("find", *args, "window", f"{start}:{start + chunk}", keep=KEEP))
        except CommandError:
            if start == 0:
                return list(client.iter_songs("find", *args, keep=KEEP))
            raise
        songs.extend(batch)
        if len(batch) < chunk:
            return songs
        start += chunk


def fetch_artist(client, name, chunk=CHUNK, genre=None, composer_genres=()):
    """All songs of one artist, indexed into albums. Returns an Artist (with
    no albums if the server has nothing for that name). With `genre`, only
    that genre's songs (the genre filter: albums show only those
    tracks, albums without any are left out)."""
    extra = () if genre is None else (genre_filter(genre),)
    if composer_genres:
        # their own songs except composer songs, plus the songs they composed in those genres
        rest = (not_composer_songs(composer_genres),)
        queries = [(_and(f"(AlbumArtist == {filter_quote(name)})", *rest, *extra),)]
    elif genre is None:
        rest = ()
        queries = [("albumartist", name)]  # old-style syntax: works on every MPD version
    else:
        rest = ()
        # old-style pairs are ANDed too, so this works before MPD 0.21 as well
        queries = [("albumartist", name, "genre", genre)]
    if name == UNKNOWN:
        queries.append((_and(NO_ARTIST_AT_ALL, *rest, *extra),))
    else:
        queries.append((_and(untagged_album_artist_filter(name), *rest, *extra),))
    for g in composer_genres:
        queries.append((_and(f"(Composer == {filter_quote(name)})", composer_song_filter(g), *extra),))
    songs, seen = [], set()
    for i, args in enumerate(queries):
        try:
            batch = _find_all(client, *args, chunk=chunk)
        except CommandError:
            if i == 0:
                raise
            continue  # the optional fallback query is not supported here
        for song in batch:
            if song["file"] not in seen:
                seen.add(song["file"])
                songs.append(song)
    artist = Library.from_songs(songs, group_as=name).artist(name)
    return artist if artist is not None else Artist(name, "")


# -- after a database update: the artist it added ---------------------------------------

UPDATE_KEEP = frozenset({"file", "album", "albumartist", "artist", "composer"})


def fetch_db_stamp(client):
    """When MPD's database was last updated (`stats` db_update, Unix time), or None."""
    try:
        stats = client.command_dict("stats")
        return int(stats.get("db_update", ""))
    except (CommandError, ValueError):
        return None


def library_artist_of(client, song, composer_genres=()):
    """The artist list entry a song is filed under (see fetch_artist): its composer
    if it has one and is of a composer genre, else album artist, artist, UNKNOWN.
    The genre is asked of MPD (a song can have several; iter_songs keeps one)."""
    composer = song.get("composer") or ""
    for g in composer_genres if composer else ():
        expr = _and(f"(file == {filter_quote(song['file'])})", composer_song_filter(g))
        if list(client.iter_songs("find", expr, "window", "0:1", keep={"file"})):
            return composer
    return song.get("albumartist") or song.get("artist") or UNKNOWN


def fetch_newest_since(client, since, field="added", composer_genres=()):
    """(artist, album title) of the newest song added (MPD >= 0.24; field "modified":
    file modified) at or after Unix time `since`, or None if there is none or the
    server cannot say. Used to show what a database update brought."""
    if since is None:
        return None
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since))
    expr = f"({'added-since' if field == 'added' else 'modified-since'} {filter_quote(iso)})"
    order = "-Added" if field == "added" else "-Last-Modified"
    try:
        songs = list(client.iter_songs("find", expr, "sort", order, "window", "0:1", keep=UPDATE_KEEP))
    except CommandError:
        return None
    if not songs:
        return None
    return library_artist_of(client, songs[0], composer_genres), songs[0].get("album") or UNKNOWN


class LibraryFetcher:
    """Runs library queries on one background connection, with an LRU cache of
    artists. No GTK: the UI injects `dispatch` (run a function on the main
    thread). Callbacks are always called on the main thread via `dispatch`;
    `cached()`/`load_*` must be called from the main thread.
    """

    def __init__(self, client, dispatch, cache_size=32):
        self._client = client  # touched only from the worker thread
        self._dispatch = dispatch
        self._cache_size = cache_size
        self._cache = OrderedDict()  # name -> Artist (main thread only)
        self._epoch = 0              # bumped when the database may have changed
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="library")
        self.composer_genres = ()  # as typed in Preferences; set from the main thread
        self._resolved = (None, ())  # ((epoch, typed), server spelling): worker thread only

    def _composer_genres(self, client):  # worker thread
        typed = self.composer_genres
        if not typed:
            return ()
        key = (self._epoch, typed)
        if self._resolved[0] != key:
            self._resolved = (key, resolve_genres(fetch_genres(client), typed))
        return self._resolved[1]

    def cached(self, name, genre=None):
        key = (name, genre)
        artist = self._cache.get(key)
        if artist is not None:
            self._cache.move_to_end(key)
        return artist

    def load_names(self, on_done, genre=None, fresh=True):
        """on_done(names_or_None, error_or_None). With fresh (the database has
        probably changed) the artist cache is dropped; a mere genre switch
        keeps it."""
        if fresh:
            self._epoch += 1
            self._cache.clear()
        self._pool.submit(self._run, lambda client: fetch_artist_names(
            client, genre, self._composer_genres(client)), None, on_done, None, self._epoch)

    def load_artist(self, name, on_done, wanted=None, genre=None):
        """on_done(artist_or_None, error_or_None). If `wanted()` is False by the
        time the request reaches the front of the queue it is skipped and
        on_done is never called."""
        self._pool.submit(self._run, lambda client: fetch_artist(
            client, name, genre=genre, composer_genres=self._composer_genres(client)), (name, genre),
            on_done, wanted, self._epoch)

    def newest_since(self, since, field, on_done):
        """on_done((artist, album) or None, error_or_None): see fetch_newest_since."""
        self._pool.submit(self._run, lambda client: fetch_newest_since(
            client, since, field, self._composer_genres(client)), None, on_done, None, self._epoch)

    def run(self, fn, on_done, wanted=None):
        """Run fn(client) on the background connection (e.g. a search);
        on_done(result_or_None, error_or_None) on the main thread. Not cached."""
        self._pool.submit(self._run, fn, None, on_done, wanted, self._epoch)

    def _run(self, fn, cache_key, on_done, wanted, epoch):  # worker thread
        if wanted is not None and not wanted():
            return
        result, error = None, None
        for _attempt in (1, 2):  # the second try covers a connection MPD dropped
            try:
                if not self._client.connected:
                    self._client.connect()
                result, error = fn(self._client), None
                break
            except CommandError as e:  # server said no: connection is still in sync
                error = e
                break
            except (OSError, MPDError) as e:
                error = e
                self._client.close()  # mid-response: start clean
        self._dispatch(self._finish, result, error, cache_key, on_done, epoch)

    def _finish(self, result, error, cache_key, on_done, epoch):  # main thread
        if error is None and cache_key is not None and epoch == self._epoch:
            self._cache[cache_key] = result
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        on_done(result, error)

    def shutdown(self):
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._client.close()
