"""Search covers online ("Search Cover Online...", as Cantata's cover dialog).

Providers that need no account or key: MusicBrainz + Cover Art Archive, Deezer,
iTunes. Only the search text is sent; requests identify as "Toccata", nothing
about the user. MusicBrainz asks for at most one request per second.

No GTK. `get(url) -> bytes` is injected (urllib in the app, canned data in tests).
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import lastfm

USER_AGENT = "Toccata/1.0 (GTK client for MPD)"
TIMEOUT = 15
MAX_BYTES = 20 * 1024 * 1024
LIMIT = 8  # results per provider


@dataclass
class Candidate:
    provider: str
    artist: str
    album: str
    thumb_url: str   # small image for the result list
    full_url: str    # what "Use this cover" downloads
    size: str = ""   # e.g. "1000 px", when the provider says

    @property
    def label(self):
        return f"{self.artist} – {self.album}" if self.artist else self.album


def http_get(url, timeout=TIMEOUT, max_bytes=MAX_BYTES):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("image too large")
    return data


def default_query(artist, album):
    return " ".join(p for p in (artist, album) if p and p != "Unknown").strip()


# -- iTunes -----------------------------------------------------------------------------

def itunes_url(query):
    return "https://itunes.apple.com/search?" + urllib.parse.urlencode(
        {"term": query, "entity": "album", "limit": LIMIT})


def parse_itunes(data):
    out = []
    for r in json.loads(data).get("results", []):
        art = r.get("artworkUrl100") or ""
        if not art:
            continue
        out.append(Candidate("iTunes", r.get("artistName", ""), r.get("collectionName", ""),
                             art.replace("100x100bb", "250x250bb"), art.replace("100x100bb", "1200x1200bb"),
                             "1200 px"))
    return out


# -- Deezer -----------------------------------------------------------------------------

def deezer_url(query):
    return "https://api.deezer.com/search/album?" + urllib.parse.urlencode({"q": query, "limit": LIMIT})


def parse_deezer(data):
    out = []
    for r in json.loads(data).get("data", []):
        if not r.get("cover_xl"):
            continue
        out.append(Candidate("Deezer", (r.get("artist") or {}).get("name", ""), r.get("title", ""),
                             r.get("cover_medium") or r["cover_xl"], r["cover_xl"], "1000 px"))
    return out


# -- Last.fm (Toccata's key or the user's own; requests go through lastfm.fetch) ------------

LASTFM_PLACEHOLDER = "2a96cbd8b46e442fc41c2b86b821562f"  # Last.fm's grey-star "no image"


def lastfm_url(query, api_key):
    return "https://ws.audioscrobbler.com/2.0/?" + urllib.parse.urlencode(
        {"method": "album.search", "album": query, "api_key": api_key, "format": "json", "limit": LIMIT})


def parse_lastfm(data):
    doc = json.loads(data)
    if "error" in doc:  # e.g. {"error": 10, "message": "Invalid API key ..."}
        raise ValueError(doc.get("message") or f"error {doc['error']}")
    albums = ((doc.get("results") or {}).get("albummatches") or {}).get("album") or []
    if isinstance(albums, dict):  # a single match comes as an object
        albums = [albums]
    out = []
    for a in albums:
        images = {i.get("size"): i.get("#text") for i in a.get("image", []) if isinstance(i, dict)}
        full = images.get("extralarge") or images.get("large")
        thumb = images.get("large") or images.get("medium") or full
        if not full or LASTFM_PLACEHOLDER in full:
            continue
        out.append(Candidate("Last.fm", a.get("artist", ""), a.get("name", ""), thumb, full, "300 px"))
    return out


# -- MusicBrainz + Cover Art Archive ------------------------------------------------------------

def _lucene(text):
    return "".join("\\" + ch if ch in '+-&|!(){}[]^"~*?:\\/' else ch for ch in text)


def musicbrainz_url(query, artist="", album=""):
    if artist and album:
        q = f'releasegroup:"{_lucene(album)}" AND artist:"{_lucene(artist)}"'
    else:
        q = _lucene(query)
    return "https://musicbrainz.org/ws/2/release-group/?" + urllib.parse.urlencode(
        {"query": q, "fmt": "json", "limit": LIMIT})


def parse_musicbrainz(data):
    out = []
    for g in json.loads(data).get("release-groups", []):
        gid = g.get("id")
        if not gid:
            continue
        credit = g.get("artist-credit") or []
        artist = "".join(c.get("name", "") + c.get("joinphrase", "") for c in credit if isinstance(c, dict))
        base = f"https://coverartarchive.org/release-group/{gid}/front"
        out.append(Candidate("Cover Art Archive", artist, g.get("title", ""), base + "-250", base))
    return out  # many groups have no cover: their thumbnail fails to load and is dropped


PROVIDERS = (
    ("Cover Art Archive", lambda q, a, b: musicbrainz_url(q, a, b), parse_musicbrainz),
    ("Deezer", lambda q, a, b: deezer_url(q), parse_deezer),
    ("iTunes", lambda q, a, b: itunes_url(q), parse_itunes),
)


class RateLimit:
    """At most one call per `interval` seconds (MusicBrainz' rule)."""

    def __init__(self, interval=1.0, clock=time.monotonic, sleep=time.sleep):
        self.interval, self.clock, self.sleep = interval, clock, sleep
        self.last = None

    def interval_wait(self, seconds):
        self.sleep(seconds)
        self.last = self.clock()

    def wait(self):
        if self.last is not None:
            delay = self.interval - (self.clock() - self.last)
            if delay > 0:
                self.sleep(delay)
        self.last = self.clock()


_MB_LIMIT = RateLimit()


def search(query, artist="", album="", get=http_get, providers=PROVIDERS, mb_limit=_MB_LIMIT, lastfm_key="",
           lastfm_guard=None):
    """Candidates from every provider, provider by provider; a provider that fails
    (offline, changed API, bad key) is skipped. Last.fm only with a key
    (lastfm.effective_key: the user's own or Toccata's).
    Returns (candidates, errors)."""
    query = (query or "").strip()
    if not query:
        return [], []
    key = (lastfm_key or "").strip()
    if key:
        providers = tuple(providers) + (("Last.fm", lambda q, a, b: lastfm_url(q, key), parse_lastfm),)
    found, errors = [], []
    seen = set()
    for name, make_url, parse in providers:
        try:
            url = make_url(query, artist, album)
            if name == "Last.fm":
                data = lastfm.fetch(get, url, key, lastfm_guard or lastfm.GUARD)  # spaced out; paused after a rate-limit answer
            elif name == "Cover Art Archive":
                mb_limit.wait()
                try:
                    data = get(url)
                except urllib.error.HTTPError as e:
                    if e.code != 503:  # MusicBrainz is overloaded at times: one more try
                        raise
                    mb_limit.interval_wait(1.5)
                    data = get(url)
            else:
                data = get(url)
            items = parse(data)
        except lastfm.Limited as e:  # names Last.fm itself, and when it is asked again
            errors.append(str(e))
            continue
        except Exception as e:  # network, HTTP or JSON trouble: the others still count
            errors.append(f"{name}: {e}")
            continue
        for c in items:
            if c.full_url not in seen:
                seen.add(c.full_url)
                found.append(c)
    return found, errors
