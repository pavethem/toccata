"""Search the MPD database by category.

Composer, Performer, Comment and Original Date only when the server knows that
tag (`tagtypes`), Modified only on MPD >= 0.19.

  * a normal category runs `search <tag> <text>` (case-insensitive substring);
  * "any" also runs `search file <text>` and merges the results, because MPD's
    `any` ignores file names and paths;
  * "modified" takes a number of days ("7" = the last 7 days, counted from
    today 00:00) or a date YYYY/MM/DD or YYYY-MM-DD, and runs
    `find modified-since <epoch>`.

Results are
fetched in one `window` (MPD >= 0.20) capped at LIMIT songs; only if that
overruns MPD's output buffer are they fetched again in windows of CHUNK songs
(see _windowed). For "any", the file-name pass is skipped when the tag pass
alone already exceeds LIMIT.
"""
import re
import time
from dataclasses import dataclass

from .library import KEEP, collate_key, int_prefix
from .mpd_client import CommandError, ProtocolError

CHUNK = 1000
LIMIT = 5000  # more is not useful in a list, and costs memory and time

MODIFIED = "modified-since"


@dataclass(frozen=True)
class Category:
    key: str        # MPD tag / pseudo tag used in the query
    label: str
    help: str
    tag: str = ""   # tagtypes entry the server must support ("" = always)


CATEGORIES = (
    Category("artist", "Artist", "Search for songs by artist."),
    Category("composer", "Composer", "Search for songs by composer.", tag="Composer"),
    Category("performer", "Performer", "Search for songs by performer.", tag="Performer"),
    Category("album", "Album", "Search for songs by album."),
    Category("title", "Title", "Search for songs by title."),
    Category("genre", "Genre", "Search for songs by genre."),
    Category("comment", "Comment", "Search for songs containing comment.", tag="Comment"),
    Category("date", "Date", "Find songs by searching the 'Date' tag. Usually just entering the year should suffice."),
    Category("originaldate", "Original Date",
             "Find songs by searching the 'Original Date' tag. Usually just entering the year should suffice.",
             tag="OriginalDate"),
    Category(MODIFIED, "Modified",
             "Enter a date (YYYY/MM/DD, e.g. 2015/01/31) to find files modified since that date, "
             "or a number of days to find files modified in the previous number of days."),
    Category("file", "File", "Search for songs by filename or path."),
    Category("any", "Any", "Search for songs by any matching metadata."),
)

DEFAULT = "any"


def parse_version(text):
    """'0.23.5' -> (0, 23, 5); unknown -> (0, 0, 0)."""
    parts = re.findall(r"\d+", text or "")[:3]
    return tuple(int(p) for p in parts) + (0,) * (3 - len(parts)) if parts else (0, 0, 0)


def available_categories(tagtypes, version):
    """Categories this server can answer, in CATEGORIES order.
    tagtypes: names from `tagtypes` (any case); version: (major, minor, patch)."""
    known = {t.casefold() for t in tagtypes}
    out = []
    for c in CATEGORIES:
        if c.tag and c.tag.casefold() not in known:
            continue
        if c.key == MODIFIED and tuple(version) < (0, 19, 0):
            continue
        out.append(c)
    return out


_DATE = re.compile(r"^((?:19|20)\d\d)[-/](0[1-9]|1[012])[-/](0[1-9]|[12][0-9]|3[01])$")


def parse_modified(text, now=None):
    """Epoch seconds for the Modified category, or None if `text` is neither a
    day count nor a date. Both are counted from local midnight."""
    text = text.strip()
    now = time.time() if now is None else now
    if text.isdigit():
        today = time.localtime(now)
        midnight = time.mktime((today.tm_year, today.tm_mon, today.tm_mday, 0, 0, 0, 0, 0, -1))
        return int(midnight) - int(text) * 24 * 60 * 60
    m = _DATE.match(text)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        try:
            return int(time.mktime((y, mo, d, 0, 0, 0, 0, 0, -1)))
        except (OverflowError, ValueError):
            return None
    return None


def song_sort_key(song):
    """Result order: album artist (else artist), album, album id,
    disc, track, then title and file."""
    return (collate_key(song.get("albumartist") or song.get("artist") or ""),
            collate_key(song.get("album", "")),
            song.get("musicbrainz_albumid", ""),
            int_prefix(song.get("disc")) or 0,
            int_prefix(song.get("track")) or 0,
            collate_key(song.get("title", "")),
            song.get("file", ""))


def _windowed(client, cmd, args, limit, chunk):
    """Songs of one search/find (at most `limit`).

    MPD scans its whole database for every window, so one window is much
    faster than several (about 4 s per window on a big library). It is tried
    first; if that answer overruns MPD's output buffer, MPD drops the
    connection, and the search is repeated on a fresh connection in windows
    of `chunk` songs. Plain call on MPD < 0.20 (no `window`)."""
    try:
        return list(client.iter_songs(cmd, *args, "window", f"0:{limit}", keep=SEARCH_KEEP))
    except CommandError:
        return list(client.iter_songs(cmd, *args, keep=SEARCH_KEEP))[:limit]  # no `window` yet
    except (ProtocolError, OSError):
        client.close()
        client.connect()  # the answer was too big: start clean and go in small steps
    return _chunked(client, cmd, args, limit, chunk)


def _chunked(client, cmd, args, limit, chunk):
    songs, start = [], 0
    while len(songs) < limit:
        end = min(start + chunk, limit)
        batch = list(client.iter_songs(cmd, *args, "window", f"{start}:{end}", keep=SEARCH_KEEP))
        songs.extend(batch)
        if len(batch) < end - start:
            break
        start = end
    return songs


SEARCH_KEEP = KEEP | {"performer", "comment", "originaldate", "last-modified", "name"}


class SearchError(ValueError):
    pass


@dataclass
class Result:
    songs: list
    truncated: bool  # LIMIT was reached; there may be more

    @property
    def duration(self):
        total = 0.0
        for s in self.songs:
            try:
                total += float(s.get("duration") or s.get("time") or 0)
            except ValueError:
                pass
        return total


def run_search(client, key, text, now=None, limit=LIMIT, chunk=CHUNK):
    """Run one search. Raises SearchError for unusable input."""
    text = text.strip()
    if not text:
        return Result([], False)
    if key == MODIFIED:
        since = parse_modified(text, now)
        if since is None:
            raise SearchError("Enter a number of days or a date like 2015/01/31")
        songs = _windowed(client, "find", (MODIFIED, str(since)), limit + 1, chunk)
    else:
        songs = _windowed(client, "search", (key, text), limit + 1, chunk)
        if key == "any" and len(songs) <= limit:  # already over the cap: the extra pass changes nothing
            seen = {s["file"] for s in songs}
            for s in _windowed(client, "search", ("file", text), limit + 1, chunk):
                if s["file"] not in seen:
                    seen.add(s["file"])
                    songs.append(s)
    songs.sort(key=song_sort_key)
    truncated = len(songs) > limit
    return Result(songs[:limit], truncated)


def format_duration(seconds):
    """Total time: '1:02:03', or '4:05' under an hour."""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def stats_text(result):
    n = len(result.songs)
    if not n:
        return "No tracks found."
    more = "+" if result.truncated else ""
    return f"{n}{more} Track{'s' if n != 1 else ''} ({format_duration(result.duration)})"
