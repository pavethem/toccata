"""Stored playlists.

GTK-free: name checks, sorting, and the "Remove Duplicates" / "Remove Invalid
Tracks" rules. The MPD commands live in mpd_client.MPDClient.
"""
from .library import collate_key


def sorted_playlists(entries):
    """[(name, last_modified)] sorted by name like the library (by name,
    playlists by name, case-insensitively)."""
    return sorted(entries, key=lambda e: collate_key(e[0]))


def name_problem(name, existing=(), current=None):
    """Why `name` cannot be used for a playlist, or None if it can.
    `existing` names are not a problem here (the caller asks to overwrite);
    use is_overwrite() for that. `current` is the name being renamed."""
    if not name.strip():
        return "Enter a name"
    if "/" in name or "\n" in name or "\r" in name:
        return "A playlist name cannot contain “/” or line breaks"
    if current is not None and name == current:
        return "That is already its name"
    return None


def is_overwrite(name, existing, current=None):
    return name != current and name in set(existing)


def _album_key(song):
    """Album key: album artist (else artist) and album id."""
    artist = song.get("albumartist") or song.get("artist") or ""
    album_id = song.get("musicbrainz_albumid") or song.get("album") or ""
    return f"{artist}:{album_id}"


def duplicate_key(song):
    """Key for "Remove Duplicates": album key, title, artist, track."""
    return (_album_key(song), song.get("title", ""), song.get("artist", ""), song.get("track", ""))


def duplicate_positions(songs):
    """Positions of every repeat of an earlier song (the first one stays)."""
    seen, out = set(), []
    for i, s in enumerate(songs):
        k = duplicate_key(s)
        if k in seen:
            out.append(i)
        else:
            seen.add(k)
    return out


def is_invalid(song):
    """A playlist entry whose file is not in the database: MPD then sends only
    its file name, without duration or tags.
    Streams are never invalid."""
    if "://" in song.get("file", ""):
        return False
    return not any(song.get(k) for k in ("duration", "time", "title", "artist", "album", "name", "genre"))


def invalid_positions(songs):
    return [i for i, s in enumerate(songs) if is_invalid(s)]


def total_duration(songs):
    total = 0.0
    for s in songs:
        try:
            total += float(s.get("duration") or s.get("time") or 0)
        except ValueError:
            pass
    return total


def suggest(names, typed, limit=50):
    """Existing playlists for the "Save as playlist" field: all of them when nothing
    is typed, else those starting with the text, then those containing it (case
    ignored), each group in name order."""
    names = [n for n, _ in sorted_playlists([(n, "") for n in names])]
    t = (typed or "").strip().casefold()
    if not t:
        return names[:limit]
    starts = [n for n in names if n.casefold().startswith(t)]
    contains = [n for n in names if t in n.casefold() and n not in starts]
    return (starts + contains)[:limit]
