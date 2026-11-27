"""Type-ahead search for the library: typing jumps to the first matching
artist, like GtkTreeView's interactive search or a file manager. No GTK here.

Matching ignores case, accents and punctuation ("guns n" finds "Guns N'
Roses"). A name that *starts* with the typed text wins; otherwise one with a
later word starting with it ("beatles" finds "The Beatles"). Typing the same
letter again steps to the next name starting with that letter.
"""
import re
import unicodedata

TIMEOUT = 1.0  # seconds without typing after which a new search starts

_NON_WORD = re.compile(r"[^\w\s]+")
_SPACES = re.compile(r"\s+")


def normalize(text):
    """Lower case, no accents, no punctuation, single spaces."""
    text = unicodedata.normalize("NFKD", text or "").casefold()  # "ö" -> "o" + combining mark
    text = _NON_WORD.sub("", text)  # drops punctuation, and the combining marks with it
    return _SPACES.sub(" ", text).strip()


class Matcher:
    """Normalized names, prepared once per artist list."""

    def __init__(self, names):
        self.names = list(names)
        self._norm = [normalize(n) for n in self.names]
        self._words = [n.split(" ") for n in self._norm]

    def find(self, typed, after=-1):
        """Index of the best match for `typed`, or None.
        after: start looking behind this index (wraps around); used to step on."""
        query = normalize(typed)
        if not query or not self.names:
            return None
        n = len(self.names)
        order = [(after + 1 + i) % n for i in range(n)]
        for i in order:
            if self._norm[i].startswith(query):
                return i
        for i in order:
            if any(w.startswith(query) for w in self._words[i][1:]):
                return i
        return None


class TypeAhead:
    """The typed text, with the timeout and "same letter again" stepping."""

    def __init__(self, timeout=TIMEOUT):
        self.timeout = timeout
        self.text = ""
        self._last = None

    def _expired(self, now):
        return self._last is None or now - self._last > self.timeout

    def type(self, char, now):
        """Add a character. Returns (text_to_search, step): step is True when the
        same single letter was typed again, meaning "go to the next match"."""
        if self._expired(now):
            self.text = ""
        self._last = now
        if self.text and set(self.text) == {char} and len(char) == 1:
            self.text += char
            return char, True
        self.text += char
        return self.text, False

    def backspace(self, now):
        if self._expired(now):
            self.text = ""
            return ""
        self._last = now
        self.text = self.text[:-1]
        return self.text

    def cancel(self):
        self.text = ""
        self._last = None

    def active(self, now):
        return bool(self.text) and not self._expired(now)
