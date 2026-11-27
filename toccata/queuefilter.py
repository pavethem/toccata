"""Filter for the play queue ("Find in Play Queue", Ctrl+Shift+F), and the same
rule for the artist list (Ctrl+F, `filter_names`).

The text is split into words; a song matches when *every* word occurs (case- and
accent-insensitive) in one of: album artist (else artist), artist, composer,
title, album. A word "#1999" keeps songs of that year, "#1990-1999" a range.
Text shorter than 2 characters does not filter. No GTK here.
"""
import re
import unicodedata
from dataclasses import dataclass, field

from .library import parse_year

MIN_YEAR, MAX_YEAR = 1500, 2500
_YEAR = re.compile(r"^#(\d{4})(?:-(\d{4}))?$")


def fold(text):
    """Lower case without accents: "Mötley" -> "motley"."""
    text = unicodedata.normalize("NFKD", text or "").casefold()
    return "".join(ch for ch in text if not unicodedata.combining(ch))


@dataclass(frozen=True)
class QueueFilter:
    words: tuple = ()
    years: tuple = None  # (from, to) or None
    text: str = ""

    @property
    def active(self):
        return bool(self.words or self.years)

    def matches(self, song):
        if not self.active:
            return True
        if self.years is not None:
            year = parse_year(song.get("date") or song.get("originaldate"))
            if not year or not self.years[0] <= year <= self.years[1]:
                return False
        if not self.words:
            return True
        fields = fold(" \x00 ".join(song_fields(song)))
        return all(w in fields for w in self.words)


def song_fields(song):
    artist = song.get("artist", "")
    album_artist = song.get("albumartist") or artist
    out = [album_artist]
    if artist and artist != album_artist:
        out.append(artist)
    composer = song.get("composer", "")
    if composer and composer not in (artist, album_artist):
        out.append(composer)
    out += [song.get("title") or song.get("name") or song.get("file", "").rsplit("/", 1)[-1],
            song.get("album", "")]
    return out


def parse(text):
    """QueueFilter for what the user typed."""
    text = " ".join((text or "").split())
    if len(text) < 2:
        return QueueFilter(text=text)
    words, years = [], None
    for part in text.split(" "):
        m = _YEAR.match(part)
        if m:
            lo = int(m.group(1))
            hi = int(m.group(2) or lo)
            if MIN_YEAR <= lo <= MAX_YEAR and MIN_YEAR <= hi <= MAX_YEAR:
                years = (min(lo, hi), max(lo, hi))
                continue
        words.append(fold(part))
    return QueueFilter(words=tuple(words), years=years, text=text)


def count_text(shown, total):
    return f"{shown} of {total}"


def filter_names(names, text):
    """The names containing every word of `text` (case and accents ignored), in their
    order. Like the queue filter, text shorter than 2 characters does not filter;
    "#year" words mean nothing here and are matched as plain text."""
    text = " ".join((text or "").split())
    if len(text) < 2:
        return list(names)
    words = [fold(w) for w in text.split(" ")]
    return [n for n in names if all(w in fold(n) for w in words)]
