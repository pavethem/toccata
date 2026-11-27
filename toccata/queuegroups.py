"""Album grouping for the play queue (the "grouped" queue view).

A group is a run of *consecutive* queue entries with the same album key
(album artist, else artist, plus MusicBrainz album id, else album name); the
same album queued twice in different places gives two groups.
Streams form "Streams" groups. A group can be collapsed: only its first entry
stays visible and stands for the whole album ("12 Tracks"); selecting it
selects all its tracks.

No GTK here. Songs are MPD dicts with lowercase keys.
"""
import os
from dataclasses import dataclass

from .library import int_prefix, parse_year
from .search import format_duration


def is_stream(song):
    return "://" in song.get("file", "")


def album_key(song):
    if is_stream(song):
        return ("stream",)
    artist = song.get("albumartist") or song.get("artist") or ""
    return ("album", artist, song.get("musicbrainz_albumid") or song.get("album") or "")


def _seconds(song):
    try:
        return float(song.get("duration") or song.get("time") or 0)
    except ValueError:
        return 0.0


@dataclass(eq=False)
class Group:
    index: int      # 0, 1, 2 ... in queue order (sections are sorted by it)
    start: int      # queue position of the first entry
    end: int        # one past the last entry
    key: tuple
    title: str      # "Album Artist – Album (Year)"
    artist: str     # album artist (else artist) shared by the group
    head_id: str    # MPD song id of the first entry: identifies the group for collapsing
    duration: float

    @property
    def n(self):
        return self.end - self.start

    def summary(self):
        """'12 Tracks (48:00)': the header subtitle and the collapsed row."""
        n = self.n
        text = f"{n} Track{'s' if n != 1 else ''}"
        return f"{text} ({format_duration(self.duration)})" if self.duration else text


def header_title(song):
    """Album header text for a group starting with `song`."""
    if is_stream(song):
        return "Streams"
    artist = song.get("albumartist") or song.get("artist") or ""
    album = song.get("album") or ""
    if not album:
        return artist or "Unknown"
    year = parse_year(song.get("date"))
    shown = f"{album} ({year})" if year else album
    return f"{artist} – {shown}" if artist else shown


def group_runs(songs):
    """Groups of consecutive songs with the same album key, in queue order."""
    groups, i = [], 0
    while i < len(songs):
        key = album_key(songs[i])
        j = i + 1
        while j < len(songs) and album_key(songs[j]) == key:
            j += 1
        first = songs[i]
        groups.append(Group(
            index=len(groups), start=i, end=j, key=key, title=header_title(first),
            artist="" if is_stream(first) else first.get("albumartist") or first.get("artist") or "",
            head_id=str(first.get("id", "")),
            duration=sum(_seconds(s) for s in songs[i:j])))
        i = j
    return groups


def track_title(song):
    """A track's line inside an album: '03  Title'. The artist is not repeated
    here: the row's second line shows it."""
    title = song.get("title") or song.get("name") or os.path.basename(song.get("file", ""))
    number = int_prefix(song.get("track"))
    return f"{number:02d}  {title}" if number else title


def prune_collapsed(collapsed, groups):
    """Keep only the collapsed marks of groups that still exist."""
    heads = {g.head_id for g in groups}
    return {h for h in collapsed if h in heads}


def layout(groups, collapsed):
    """Per queue position: (hidden, is_summary) for the given collapsed head ids."""
    out = []
    for g in groups:
        closed = g.head_id in collapsed
        for pos in range(g.start, g.end):
            out.append((closed and pos > g.start, closed and pos == g.start))
    return out


def drop_index(group, row_index, insert_at, collapsed):
    """Queue index for a drop on the visible row `row_index`: dnd.insert_index
    gave `insert_at` (row_index or row_index + 1). A collapsed group's row
    stands for the whole album, so its lower half means after the album."""
    if group is None or group.head_id not in collapsed or row_index != group.start:
        return insert_at
    return group.start if insert_at == row_index else group.end


def shown_row(rows, entry):
    """Index in `rows` (what the queue list shows: entries and album header items,
    both with a `group` attribute, None when ungrouped) where `entry` can be seen:
    its own row; inside a collapsed album the album's summary row, else its header;
    None when it is not shown at all (filtered out)."""
    fallback = None
    group = getattr(entry, "group", None)
    for i, row in enumerate(rows):
        if row is entry:
            return i
        if group is not None and getattr(row, "group", None) is group:
            if getattr(row, "summary", False):
                return i
            if fallback is None:
                fallback = i
    return fallback
