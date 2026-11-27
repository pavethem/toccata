"""Pure tag-editing operations, ported from Cantata's Song class
(mpd-interface/song.cpp: capitalize, capitalise, setAlbumArtist,
fixVariousArtists, revertVariousArtists) and TagEditor::adjustTrackNumbers.

Songs are plain dicts with the keys in SONG_KEYS (track/disc/year are ints,
the rest are strings). Every function returns a *new* dict plus a "changed"
flag and never touches its input, so the editor can keep original/edited
copies and diff them.

Original: Copyright (c) 2011-2022 Craig Drummond, GPL-2.0-or-later.

Deliberate deviations from the C++ (see tests):
  * fix_various_artists writes "Artist - Title". The C++ builds the title with
    Song::artistSong(), which is "Title • Artist" (U+2022 separator), while its
    own dialog text promises "TrackArtist - TrackTitle" and revertVariousArtists
    only recognises " - ", so the original's "revert" cannot undo its "apply".
  * adjust_track_numbers clamps at 0 instead of allowing negative track numbers.
  * capitalize uses simple (1:1) case mapping like QChar::toUpper, so 'ß' stays
    'ß' instead of becoming 'SS'.
"""

SONG_KEYS = ("title", "artist", "album", "albumartist", "composer", "comment",
             "genre", "track", "disc", "year")
VARIOUS_ARTISTS = "Various Artists"
VA_SEP = " - "


def _copy(song):
    return dict(song)


def _upper1(ch):
    u = ch.upper()
    return u if len(u) == 1 else ch


def capitalize(s):
    """Upper-case the first letter of each space-separated word.

    Leading '(', '[' and '{' are skipped so "(live)" becomes "(Live)". The rest
    of each word is left alone ("McDonald" stays), and runs of spaces collapse
    to one, as in the original (it splits with SkipEmptyParts).
    """
    if not s:
        return s
    words = []
    for word in s.split(" "):
        if word == "":
            continue
        j = 0
        while j < len(word) and word[j] in "([{":
            j += 1
        if j < len(word):
            word = word[:j] + _upper1(word[j]) + word[j + 1:]
        words.append(word)
    return " ".join(words)


def capitalise_song(song, text_keys=("artist", "albumartist", "album", "title", "composer")):
    out = _copy(song)
    for k in text_keys:
        out[k] = capitalize(out.get(k, ""))
    changed = any(out.get(k, "") != song.get(k, "") for k in text_keys)
    return out, changed


def set_album_artist(song):
    """Copy artist -> albumartist, but only when albumartist is empty."""
    if song.get("artist") and not song.get("albumartist"):
        out = _copy(song)
        out["albumartist"] = out["artist"]
        return out, True
    return song, False


def is_various_artists(song, extra_names=()):
    names = {VARIOUS_ARTISTS, *extra_names}
    return song.get("albumartist", "") in names


def fix_various_artists(song, extra_names=()):
    """Compilation workaround: albumartist stays 'Various Artists', the real
    track artist moves into the title as 'Artist - Title', artist := albumartist.
    Only applies when albumartist is 'Various Artists'."""
    if not is_various_artists(song, extra_names):
        return song, False
    out = _copy(song)
    real_artist = out.get("artist", "").replace(VA_SEP, ", ")
    out["title"] = f"{real_artist}{VA_SEP}{out.get('title', '')}"
    out["artist"] = out.get("albumartist", "")
    return out, True


def revert_various_artists(song):
    """Inverse of fix_various_artists: when artist == albumartist and the title
    is 'Artist - Title', split it back. Splits on the first ' - ' and requires
    non-empty text on both sides (same guard as the C++)."""
    title = song.get("title", "")
    if song.get("artist", "") == song.get("albumartist", ""):
        pos = title.find(VA_SEP)
        if 0 < pos < len(title) - 3:
            out = _copy(song)
            out["artist"] = title[:pos]
            out["title"] = title[pos + len(VA_SEP):]
            return out, True
    return song, False


def adjust_track_number(song, delta):
    if delta == 0:
        return song, False
    out = _copy(song)
    out["track"] = max(0, int(out.get("track", 0)) + delta)
    return out, out["track"] != song.get("track", 0)


def apply_to_all(songs, op, *args, **kwargs):
    """Run a per-song op over a list. Returns (new_list, indexes_changed)."""
    new, changed = [], []
    for i, s in enumerate(songs):
        out, did = op(s, *args, **kwargs)
        new.append(out)
        if did:
            changed.append(i)
    return new, changed
