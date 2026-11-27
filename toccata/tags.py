"""Tag reading/writing for local files, via mutagen's "easy" interface.

Easy mode maps one set of simple field names onto ID3 (mp3), Vorbis comments
(flac/ogg/opus) and MP4 atoms, so the editor works across formats.
"""
import os

# (key, label) in display order. Keys are mutagen "easy" field names.
FIELDS = [
    ("title", "Title"),
    ("artist", "Artist"),
    ("album", "Album"),
    ("albumartist", "Album artist"),
    ("tracknumber", "Track"),
    ("discnumber", "Disc"),
    ("date", "Year / date"),
    ("genre", "Genre"),
    ("composer", "Composer"),
]


class TagError(Exception):
    pass


def _open(path):
    try:
        import mutagen
    except ImportError as e:
        raise TagError("Tag editing needs the 'mutagen' Python package "
                       "(Arch: python-mutagen).") from e
    try:
        audio = mutagen.File(path, easy=True)
    except Exception as e:  # mutagen raises many format-specific errors
        raise TagError(f"Cannot read {os.path.basename(path)}: {e}") from e
    if audio is None:
        raise TagError(f"Unsupported file type: {os.path.basename(path)}")
    return audio


def read_tags(path):
    """Return {field: first value or ''} for every field in FIELDS."""
    audio = _open(path)
    tags = audio.tags or {}
    out = {}
    for key, _ in FIELDS:
        try:
            vals = tags.get(key) or []
        except Exception:
            vals = []
        out[key] = vals[0] if vals else ""
    return out


def write_tags(path, changes):
    """Write only the fields in `changes`. An empty string deletes the field."""
    if not changes:
        return
    audio = _open(path)
    if audio.tags is None:
        try:
            audio.add_tags()
        except Exception as e:
            raise TagError(f"Cannot add tags to {os.path.basename(path)}: {e}") from e
    for key, value in changes.items():
        if value == "":
            try:
                del audio[key]
            except KeyError:
                pass
        else:
            audio[key] = [value]
    try:
        audio.save()
    except Exception as e:
        raise TagError(f"Cannot save {os.path.basename(path)}: {e}") from e


def merge_for_edit(per_file):
    """Combine several files' tags for a multi-file edit.

    Returns {field: (value, mixed)}. If files disagree, value is '' and
    mixed is True, so the UI can show "(multiple values)" and only write
    the field if the user actually changes it.
    """
    result = {}
    for key, _ in FIELDS:
        values = {t[key] for t in per_file}
        if len(values) == 1:
            result[key] = (values.pop(), False)
        else:
            result[key] = ("", True)
    return result


def resolve_path(music_dir, uri):
    """Map an MPD-relative URI to a real file path, refusing path escapes."""
    if not music_dir:
        raise TagError("Music directory unknown. Pass --music-dir (MPD only "
                       "reports it over a local socket connection).")
    base = os.path.realpath(os.path.expanduser(music_dir))
    full = os.path.realpath(os.path.join(base, uri))
    if os.path.commonpath([base, full]) != base:
        raise TagError(f"Refusing path outside music directory: {uri}")
    if not os.path.isfile(full):
        raise TagError(f"File not found: {full}")
    return full


class MultiEdit:
    """Editing several files at once, as Cantata's tag editor: "All tracks" or each
    track on its own, switched freely; nothing is written until changes() is used.

    page None is "All tracks": a field typed there is set for every file; a field
    that differs between the files ("multiple values", shown empty) and is left
    empty keeps each file's own value. A page number is one file's own tags."""

    def __init__(self, per_file):
        self.original = [dict(t) for t in per_file]
        self.current = [dict(t) for t in per_file]

    def __len__(self):
        return len(self.current)

    def view(self, page):
        """{field: (value, mixed)} to show on a page."""
        if page is None:
            return merge_for_edit(self.current)
        return {key: (self.current[page].get(key, ""), False) for key, _ in FIELDS}

    def commit(self, page, values):
        """Take what the user typed on `page` ({field: text})."""
        shown = self.view(page)
        for key, text in values.items():
            if key not in shown:
                continue
            text = text.strip()
            if text == shown[key][0]:
                continue  # untouched (a "multiple values" field is shown empty: left empty = keep each)
            for t in (self.current if page is None else [self.current[page]]):
                t[key] = text

    def edited(self, i):
        return self.changes_for(i) != {}

    def changes_for(self, i):
        return {key: self.current[i].get(key, "") for key, _ in FIELDS
                if self.current[i].get(key, "") != self.original[i].get(key, "")}

    def changes(self):
        """[(file index, {field: new value})] for the files that changed."""
        return [(i, c) for i in range(len(self)) if (c := self.changes_for(i))]

    def label(self, i):
        """The track's entry in the track list: "3. Title" (or its number / position)."""
        t = self.current[i]
        num = (t.get("tracknumber") or "").split("/")[0].strip()
        title = t.get("title") or f"Track {i + 1}"
        return (f"{num}. {title}" if num else title) + (" •" if self.edited(i) else "")


# -- ReplayGain (shown read-only in the tag editor) ---------------------------------------

RG_FIELDS = ("track_gain", "track_peak", "album_gain", "album_peak")


def _rg_key(key):
    """'REPLAYGAIN_TRACK_GAIN', 'TXXX:replaygain_track_gain', '----:com.apple.iTunes:...' ->
    'replaygain_track_gain'; Opus 'R128_TRACK_GAIN' -> 'r128_track_gain'."""
    k = str(key).lower()
    for prefix in ("txxx:", "----:com.apple.itunes:"):
        if k.startswith(prefix):
            k = k[len(prefix):]
    return k


def _first_text(value):
    if hasattr(value, "text"):  # ID3 frame
        value = value.text
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    if isinstance(value, bytes):  # MP4 freeform
        value = value.decode("utf-8", "replace")
    return str(value).strip()


def _number(text):
    """The number at the start of '-7.23 dB' / '0.988' / '+1,5 dB', or None."""
    import re
    m = re.match(r"\s*([+-]?\d+(?:[.,]\d+)?)", text or "")
    return float(m.group(1).replace(",", ".")) if m else None


def replaygain_from_items(items):
    """{track_gain, track_peak, album_gain, album_peak: float or None} from a file's tag
    items [(key, value)]. Opus' R128 gains (Q7.8 integers relative to -23 LUFS) are
    converted to ReplayGain's -18 LUFS reference (+5 dB) when there is no ReplayGain tag."""
    found = {}
    for key, value in items:
        found.setdefault(_rg_key(key), _first_text(value))
    out = {f: _number(found.get("replaygain_" + f)) for f in RG_FIELDS}
    for which in ("track", "album"):
        r128 = _number(found.get(f"r128_{which}_gain"))
        if out[f"{which}_gain"] is None and r128 is not None:
            out[f"{which}_gain"] = round(r128 / 256 + 5, 2)
    return out


def _rva2_items(tags):
    """Old-style ID3 RVA2 frames ('RVA2:track' / 'RVA2:album') as ReplayGain items."""
    out = []
    for frame in getattr(tags, "getall", lambda _k: [])("RVA2"):
        which = (getattr(frame, "desc", "") or "").lower()
        if which in ("track", "album") and getattr(frame, "channel", 1) == 1:
            out += [(f"replaygain_{which}_gain", f"{frame.gain:.2f} dB"),
                    (f"replaygain_{which}_peak", f"{frame.peak:.6f}")]
    return out


def read_replaygain(path):
    """ReplayGain of one file (see replaygain_from_items)."""
    try:
        import mutagen
        audio = mutagen.File(path)  # not "easy": ReplayGain lives outside its field set
    except ImportError as e:
        raise TagError("Tag editing needs the 'mutagen' Python package (Arch: python-mutagen).") from e
    except Exception as e:
        raise TagError(f"Cannot read {os.path.basename(path)}: {e}") from e
    if audio is None:
        raise TagError(f"Unsupported file type: {os.path.basename(path)}")
    tags = audio.tags
    if tags is None:
        return {f: None for f in RG_FIELDS}
    try:
        items = list(tags.items())
    except Exception:
        items = []
    return replaygain_from_items(items + _rva2_items(tags))


def format_gain(value):
    return "–" if value is None else f"{value:+.2f} dB"


def format_peak(value):
    """'0.988 (-0.10 dBFS)'."""
    import math
    if value is None:
        return "–"
    return f"{value:.3f}" + (f" ({20 * math.log10(value):.2f} dBFS)" if value > 0 else "")


# -- lyrics in the file's own tags (Info view) -----------------------------------------

LYRICS_KEYS = ("lyrics", "unsyncedlyrics", "unsynced lyrics", "uslt", "©lyr", "wm/lyrics")


def lyrics_from_items(items):
    """Lyrics text from (key, value) tag items of any format, or "". ID3's USLT
    frames come as ("USLT::eng", frame); synced SYLT frames are turned into LRC
    lines, so the Info view can follow them."""
    synced = ""
    for key, value in items:
        base = str(key).split(":")[0].strip().lower()
        if base == "sylt" and not synced:
            pairs = getattr(value, "text", None) or []
            lines = []
            for text, ms in pairs:  # mutagen: [(text, time)], time in ms with format 2
                if isinstance(ms, (int, float)):
                    lines.append(f"[{int(ms // 60000):02d}:{(ms % 60000) / 1000:05.2f}]{str(text).strip()}")
            synced = "\n".join(lines)
            continue
        if base not in LYRICS_KEYS:
            continue
        text = getattr(value, "text", value)  # ID3 USLT: frame.text is the lyrics string
        text = _first_text(text) if not isinstance(text, str) else text
        text = str(text or "").strip()
        if text:
            return text
    return synced


def read_lyrics(path):
    """Lyrics stored in the audio file, or "" (also when the file cannot be read:
    the Info view then looks elsewhere)."""
    try:
        import mutagen
        audio = mutagen.File(path)
    except Exception:
        return ""
    if audio is None or audio.tags is None:
        return ""
    try:
        items = list(audio.tags.items())
    except Exception:
        return ""
    return lyrics_from_items(items)
