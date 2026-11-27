# SPDX-License-Identifier: GPL-2.0-or-later
"""Last.fm access shared by the cover search and "Similar artists".

Toccata ships its own API key (like Cantata, gui/apikeys.cpp); the user can put
their own in Preferences, which then wins. Only unsigned read calls are made
(album.search, artist.getsimilar), so no shared secret is needed.

The built-in key is shared by every Toccata user, so it has to be treated
gently (Cantata does the same: ApiKeys::setLimitReached / isLimitReached):
- requests are spaced out: at most 4 per second over all threads (Last.fm's terms
  allow 5 per second per IP, averaged over five minutes);
- when Last.fm says "rate limit exceeded" (error 29 or HTTP 429) the key is left
  alone for a while instead of retrying, likewise after "key suspended" or
  "invalid key"; meanwhile calls fail at once with `Limited`, without a request.
The pause belongs to one key: entering another key in Preferences is not blocked.

No GTK. `get(url) -> bytes` is injected, as in coversearch and info.
"""
import json
import threading
import time
import urllib.error

# Toccata's own key (https://www.last.fm/api/account/create). It is public by
# nature (the source is open); see docs/DECISIONS.md "Last.fm API key".
DEFAULT_API_KEY = "85ba5c06e3f9de6a9a0725c57cfb6548"

MIN_INTERVAL = 0.25  # seconds between two requests
# Last.fm error code -> seconds to leave the key alone
BACKOFF = {
    29: 5 * 60,    # rate limit exceeded
    26: 60 * 60,   # suspended API key
    10: 60 * 60,   # invalid API key
    11: 60,        # service offline
    16: 60,        # temporarily unavailable
}
HTTP_429 = 29  # an HTTP 429 counts as error 29


def valid_key(key):
    """A user key as stored in the settings: Last.fm keys are 32 hex digits;
    anything that is not alphanumeric is rejected (it goes into URLs)."""
    key = (key or "").strip()
    return key if key.isalnum() and len(key) <= 100 else ""


def effective_key(user_key):
    """The key to use: the user's own, else Toccata's ("" = Last.fm not used)."""
    return valid_key(user_key) or DEFAULT_API_KEY


def using_default(user_key):
    return not valid_key(user_key)


class Limited(Exception):
    """Last.fm is not asked right now (rate limit, bad key); `seconds` until it is."""

    def __init__(self, message, seconds):
        super().__init__(message)
        self.seconds = seconds


class LastfmError(ValueError):
    """Last.fm answered with an error document ({"error": 6, "message": ...})."""

    def __init__(self, code, message):
        super().__init__(message or f"Last.fm error {code}")
        self.code = code


def check(data):
    """The decoded JSON document; LastfmError when it is an error answer."""
    doc = json.loads(data)
    if isinstance(doc, dict) and "error" in doc:
        code = doc.get("error")
        raise LastfmError(code if isinstance(code, int) else None, doc.get("message"))
    return doc


def _wait_text(seconds):
    minutes = round(seconds / 60)
    return f"{minutes} min" if minutes >= 1 else f"{max(1, round(seconds))} s"


class Guard:
    """Request spacing and the per-key pause. Thread-safe."""

    def __init__(self, interval=MIN_INTERVAL, backoff=BACKOFF, clock=time.monotonic, sleep=time.sleep):
        self.interval, self.backoff, self.clock, self.sleep = interval, dict(backoff), clock, sleep
        self._lock = threading.Lock()
        self._next = None      # earliest time of the next request
        self._paused = {}      # key -> (until, reason)

    def paused(self, key):
        """(seconds left, reason) while `key` is paused, else None."""
        with self._lock:
            entry = self._paused.get(key)
            if entry is None:
                return None
            left = entry[0] - self.clock()
            if left <= 0:
                del self._paused[key]
                return None
            return left, entry[1]

    def before(self, key):
        """Call before each request: raises Limited while the key is paused, else
        waits for its turn (the slot is reserved under the lock, the sleep is not)."""
        p = self.paused(key)
        if p is not None:
            left, reason = p
            raise Limited(f"{reason} - Last.fm is asked again in {_wait_text(left)}", left)
        with self._lock:
            now = self.clock()
            start = now if self._next is None else max(now, self._next)
            self._next = start + self.interval
        if start > now:
            self.sleep(start - now)

    def failed(self, key, code, reason):
        """Last.fm answered `code`: pause the key if the code calls for it.
        Returns the pause in seconds (0 = none)."""
        seconds = self.backoff.get(code, 0)
        if seconds:
            with self._lock:
                self._paused[key] = (self.clock() + seconds, reason)
        return seconds


GUARD = Guard()


def fetch(get, url, key, guard=GUARD):
    """One Last.fm request through the guard: the raw bytes of the answer.
    Error answers that call for a pause are recorded and raised as Limited;
    other error answers come back as they are (the caller's parser reports them)."""
    guard.before(key)
    try:
        data = get(url)
    except urllib.error.HTTPError as e:
        if e.code != 429:
            raise
        seconds = guard.failed(key, HTTP_429, "Last.fm rate limit reached")
        raise Limited(f"Last.fm rate limit reached - asked again in {_wait_text(seconds)}", seconds) from e
    try:
        check(data or b"{}")
    except LastfmError as e:
        reason = {29: "Last.fm rate limit reached", 26: "This Last.fm API key is suspended",
                  10: "This Last.fm API key is invalid"}.get(e.code, f"Last.fm: {e}")
        seconds = guard.failed(key, e.code, reason)
        if seconds:
            raise Limited(f"{reason} - asked again in {_wait_text(seconds)}", seconds) from e
    except ValueError:
        pass  # not JSON: the caller's parser says so
    return data
