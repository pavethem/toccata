"""Information about the playing song (the "Info" view, like Cantata's context view):
Wikipedia text on the album artist and the album, similar artists (Last.fm), lyrics.

No GTK. Network access goes through an injected `get(url) -> bytes` (urllib in the
app, canned answers in tests), on a worker thread.

Finding the right Wikipedia article: by name alone a search often hits the wrong
page (a band called "Low"), so the way is MusicBrainz -> Wikidata -> Wikipedia:
MusicBrainz ids from the file's tags when present (Picard writes them), else a
MusicBrainz search that only accepts an exact name match; MusicBrainz links the
artist / release group to its Wikidata item, whose sitelinks name the article in
English (the interface is English-only, user 2026-11-11), lead section only.
When MusicBrainz is busy or has neither the artist / album nor any link for it, a
Wikipedia search as in Cantata, with checks (see "Wikipedia search" below). Only
artist, album and track names (or MusicBrainz ids) are sent;
requests identify as Toccata (coversearch.USER_AGENT). MusicBrainz: one request per
second (coversearch's RateLimit).

Lyrics (own design, 2026-11-11; Cantata's lyrics fetching is broken): the file's own
tags (tags.read_lyrics), a .lrc / .txt file next to it, then LRCLIB (lrclib.net:
free, no key; often time-synced LRC, so the current line can follow the song).
"""
import hashlib
import json
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse

from . import lastfm
from .coversearch import RateLimit, http_get

TEXT_MAX = 2 * 1024 * 1024
_MB = "https://musicbrainz.org/ws/2/"
_MB_LIMIT = RateLimit()
# The Info view looks up artist and album on two threads; MusicBrainz requests must
# still go one at a time (its rule: 1 per second; RateLimit itself is not thread-safe).
_MB_LOCK = threading.Lock()
MB_RETRIES = 3  # after a 503 ("busy": rate limit or its search servers shedding load)


class Busy(Exception):
    """MusicBrainz kept answering 503 (busy)."""


class _NoArticle:
    """MusicBrainz links the entity to Wikipedia / Wikidata, but not in a wanted
    language. False like None."""
    __slots__ = ()

    def __bool__(self):
        return False


NO_ARTICLE = _NoArticle()


def get_text(url):
    """The app's get(): bytes of a page; None when the server says 404."""
    try:
        return http_get(url, max_bytes=TEXT_MAX)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


# -- names ------------------------------------------------------------------------------

def norm(name):
    """For comparing names: case, accents, punctuation and "the" ignored."""
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    text = text.replace("&", " and ")
    text = re.sub(r"[^\w]+", " ", text).strip()
    return re.sub(r"^the ", "", text)


def _lucene(text):
    return '"' + (text or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def song_artist(song):
    return song.get("albumartist") or song.get("artist") or ""


def first_tag(song, *keys):
    for key in keys:
        value = song.get(key)
        if isinstance(value, list):
            value = value[0] if value else ""
        if value:
            return value
    return ""


# -- MusicBrainz ------------------------------------------------------------------------

def mb_artist_search_url(name):
    return _MB + "artist?" + urllib.parse.urlencode({"query": "artist:" + _lucene(name), "fmt": "json", "limit": 10})


def parse_mb_artist_search(data, name):
    """Ids of artists whose name (or an alias) is exactly `name`, best first."""
    want = norm(name)
    out = []
    for a in json.loads(data).get("artists", []):
        names = [a.get("name", ""), a.get("sort-name", "")] + [x.get("name", "") for x in a.get("aliases") or []]
        if any(norm(n) == want for n in names if n):
            out.append((-int(a.get("score", 0)), a["id"]))
    return [i for _, i in sorted(out, key=lambda t: t[0])]


def mb_rg_search_url(album, artist="", artist_id=""):
    q = "releasegroup:" + _lucene(album)
    if artist_id:
        q += " AND arid:" + artist_id
    elif artist:
        q += " AND artist:" + _lucene(artist)
    return _MB + "release-group?" + urllib.parse.urlencode({"query": q, "fmt": "json", "limit": 10})


def parse_mb_rg_search(data, album):
    """Ids of release groups titled exactly `album`, best first."""
    want = norm(album)
    out = [(-int(rg.get("score", 0)), rg["id"]) for rg in json.loads(data).get("release-groups", [])
           if norm(rg.get("title", "")) == want]
    return [i for _, i in sorted(out, key=lambda t: t[0])]


def mb_lookup_url(kind, mbid, inc="url-rels"):
    return f"{_MB}{kind}/{urllib.parse.quote(mbid)}?" + urllib.parse.urlencode({"inc": inc, "fmt": "json"})


def parse_release_group_of(data):
    """The release group id of a release (MUSICBRAINZ_ALBUMID is a release id)."""
    return (json.loads(data).get("release-group") or {}).get("id")


def parse_links(data):
    """(wikidata id or None, {lang: title} of direct Wikipedia links) of an MB entity."""
    qid, wiki = None, {}
    for rel in json.loads(data).get("relations") or []:
        url = (rel.get("url") or {}).get("resource", "")
        if rel.get("type") == "wikidata":
            m = re.search(r"/(Q\d+)$", url)
            if m:
                qid = m.group(1)
        elif rel.get("type") == "wikipedia":
            m = re.match(r"https?://([a-z-]+)\.wikipedia\.org/wiki/(.+)$", url)
            if m:
                wiki[m.group(1)] = urllib.parse.unquote(m.group(2)).replace("_", " ")
    return qid, wiki


# -- Wikidata / Wikipedia ---------------------------------------------------------------

def wikidata_url(qid):
    return "https://www.wikidata.org/w/api.php?" + urllib.parse.urlencode(
        {"action": "wbgetentities", "ids": qid, "props": "sitelinks", "format": "json"})


def parse_sitelinks(data, qid):
    """{lang: article title} from a Wikidata item's sitelinks (Wikipedias only)."""
    links = ((json.loads(data).get("entities") or {}).get(qid) or {}).get("sitelinks") or {}
    out = {}
    for site, link in links.items():
        m = re.fullmatch(r"([a-z_]+)wiki", site)
        if m and m.group(1) not in ("commons", "species", "meta"):
            out[m.group(1).replace("_", "-")] = link.get("title", "")
    return out


def pick_article(titles, langs):
    """(lang, title) in the first wanted language that has an article, else None."""
    for lang in langs:
        if titles.get(lang):
            return lang, titles[lang]
    return None


def wikipedia_url(lang, title):
    return f"https://{lang}.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
        {"action": "query", "prop": "extracts", "exintro": 1, "explaintext": 1,
         "redirects": 1, "format": "json", "formatversion": 2, "titles": title})


def article_link(lang, title):
    return f"https://{lang}.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"), safe="()',:!")


def parse_extract(data):
    """The article's lead section (the text before the first heading; Wikipedia is
    asked for just that, exintro) as a list of paragraphs. [] when the page does not
    exist. The full article is a link in the view."""
    pages = (json.loads(data).get("query") or {}).get("pages") or []
    if not pages or pages[0].get("missing") or not pages[0].get("extract"):
        return []
    out = []
    for block in pages[0]["extract"].split("\n"):
        block = block.strip()
        if re.fullmatch(r"=+.*=+", block):
            break  # a heading: the lead is over
        if block:
            out.append(block)
    return out


# -- Wikipedia search (the fallback, Cantata's way) ---------------------------------------
# When MusicBrainz is busy or knows nothing about the artist / album, the article is
# found as Cantata does (context/wikipediaengine.cpp): a Wikipedia full-text search,
# then an exact title, "Name (band)" style first. Cantata then takes a plain "Name" as
# well, which tried live (2026-11-21) gave a name page for "James" and the wrong
# "Embrace" for Low's album; so a page is only taken if it is no disambiguation page
# and its lead looks right (`plausible`).

SEARCH_PATTERNS = {"artist": ("artist", "band", "singer", "vocalist", "musician"),  # Cantata's order
                   "album": ("album", "score", "soundtrack")}
SEARCH_TRIES = 3  # candidate pages fetched at most
_MUSIC_WORDS = re.compile(r"\b(band|singer|musician|rapper|songwriter|composer|vocalist|guitarist|pianist|"
                          r"drummer|bassist|duo|trio|orchestra|ensemble|dj|record producer|musical group)\b")
_RELEASE_WORDS = re.compile(r"\b(album|ep|soundtrack|score|mixtape|compilation|record|recording)\b")


def wikipedia_search_url(lang, text):
    return f"https://{lang}.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
        {"action": "query", "list": "search", "srsearch": text, "srprop": "", "srlimit": 20,
         "format": "json", "formatversion": 2})


def parse_search_titles(data):
    return [p["title"] for p in (json.loads(data).get("query") or {}).get("search") or [] if p.get("title")]


def _simple(text):
    return " ".join((text or "").split()).casefold()


def search_candidates(titles, artist, album=None):
    """The titles Cantata would take, in its order of preference (Cantata takes only
    the first): for an album "Album (Artist album)", then for "Artist Album" and
    "Album" each "... (album)" and the plain title; for an artist "Name (band)" etc.
    and the plain name. Each query also without dots (A.S.A.P.) and with "-" as "/"
    (AC-DC)."""
    by_simple = {}
    for t in titles:
        by_simple.setdefault(_simple(t), t)
    kind = "album" if album else "artist"
    words = [artist, album] if album else [artist]
    wanted = []
    if album:
        wanted += [f"{album} ({artist} {p})" for p in SEARCH_PATTERNS["album"]]
    while words:
        q = " ".join(words)
        for variant in dict.fromkeys([q, q.replace(".", ""), q.replace("-", "/")]):
            wanted += [f"{variant} ({p})" for p in SEARCH_PATTERNS[kind]] + [variant]
        words = words[1:]
    out = []
    for w in wanted:
        t = by_simple.get(_simple(w))
        if t and t not in out:
            out.append(t)
    return out


def wikipedia_page_url(lang, title):
    """Like wikipedia_url, but also asks whether the page is a disambiguation page."""
    return f"https://{lang}.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
        {"action": "query", "prop": "extracts|pageprops", "ppprop": "disambiguation", "exintro": 1,
         "explaintext": 1, "redirects": 1, "format": "json", "formatversion": 2, "titles": title})


def is_disambiguation(data):
    pages = (json.loads(data).get("query") or {}).get("pages") or []
    return bool(pages) and "disambiguation" in (pages[0].get("pageprops") or {})


def page_title(data, default):
    """The page's title after redirects."""
    pages = (json.loads(data).get("query") or {}).get("pages") or []
    return (pages[0].get("title") if pages else None) or default


def plausible(paragraphs, kind, artist):
    """Does an article's lead fit? An artist's says what kind of musician (band,
    singer, ...); an album's names the artist and says album, EP, soundtrack, ..."""
    lead = " " + norm(" ".join(paragraphs)) + " "
    if kind == "artist":
        return bool(_MUSIC_WORDS.search(lead))
    name = norm(artist)
    return bool(name) and f" {name} " in lead and bool(_RELEASE_WORDS.search(lead))


# -- Last.fm (similar artists) ----------------------------------------------------------

def lastfm_similar_url(artist, api_key, limit=100):
    # 100: Last.fm's default, what Cantata gets (it sends no limit; 20 was too few, user 2026-11-11)
    return "https://ws.audioscrobbler.com/2.0/?" + urllib.parse.urlencode(
        {"method": "artist.getsimilar", "artist": artist, "api_key": api_key, "format": "json",
         "limit": limit, "autocorrect": 1})


def parse_lastfm_similar(data):
    """Names of similar artists, most similar first. ValueError when Last.fm
    answers with an error (e.g. a bad key)."""
    doc = json.loads(data)
    if "error" in doc:
        raise ValueError(doc.get("message") or f"Last.fm error {doc['error']}")
    return [a.get("name", "") for a in (doc.get("similarartists") or {}).get("artist") or [] if a.get("name")]


SIMILAR_LINK = "toccata-artist:"


def similar_markup(names, library_names):
    """Pango markup "A, B, C" for the similar artists: those in the library (compared
    with norm) are links to SIMILAR_LINK + the library's spelling of the name, the
    others plain text (user: links, not buttons, 2026-11-11)."""
    by_norm = {norm(n): n for n in library_names}
    parts = []
    for name in names:
        mine = by_norm.get(norm(name))
        text = _xml(name)
        if mine is not None:
            href = _xml(SIMILAR_LINK + urllib.parse.quote(mine, safe=""))
            parts.append(f'<a href="{href}" title="Show in Artists">{text}</a>')
        else:
            parts.append(text)
    return ", ".join(parts)


def _xml(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


# -- lyrics -----------------------------------------------------------------------------

def lrclib_get_url(artist, title, album="", duration=None):
    params = {"artist_name": artist, "track_name": title}
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = int(round(float(duration)))
    return "https://lrclib.net/api/get?" + urllib.parse.urlencode(params)


def lrclib_search_url(artist, title):
    return "https://lrclib.net/api/search?" + urllib.parse.urlencode({"artist_name": artist, "track_name": title})


def parse_lrclib(data):
    """(plain, synced) of an LRCLIB record; ("", "") for none, ("[instrumental]", "") too."""
    if not data:
        return "", ""
    doc = json.loads(data)
    if isinstance(doc, dict) and doc.get("instrumental"):
        return "[Instrumental]", ""
    if not isinstance(doc, dict):
        return "", ""
    return (doc.get("plainLyrics") or "").strip(), (doc.get("syncedLyrics") or "").strip()


def pick_lrclib(data, artist, title, duration=None):
    """The best record of an LRCLIB search: same title and artist (as norm), length
    within 3 s when known; synced ones first. (plain, synced) or ("", "")."""
    best = None
    for rec in json.loads(data or b"[]") or []:
        if norm(rec.get("trackName", "")) != norm(title) or norm(rec.get("artistName", "")) != norm(artist):
            continue
        if duration and rec.get("duration") and abs(float(rec["duration"]) - float(duration)) > 3:
            continue
        rank = (0 if rec.get("syncedLyrics") else 1, 0 if rec.get("plainLyrics") else 1)
        if best is None or rank < best[0]:
            best = (rank, rec)
    return parse_lrclib(json.dumps(best[1]).encode()) if best else ("", "")


_LRC_TIME = re.compile(r"\[(\d+):(\d+(?:[.:]\d+)?)\]")


def parse_lrc(text):
    """Synced lyrics: [(seconds, line)] sorted by time ([] if not LRC). A line may
    carry several times; tags such as [ar:...] are skipped."""
    out = []
    for raw in (text or "").splitlines():
        times = list(_LRC_TIME.finditer(raw))
        if not times:
            continue
        line = _LRC_TIME.sub("", raw).strip()
        for m in times:
            out.append((int(m.group(1)) * 60 + float(m.group(2).replace(":", ".")), line))
    return sorted(out, key=lambda t: t[0])


def lrc_plain(synced):
    return "\n".join(line for _, line in synced)


def current_line(synced, elapsed):
    """Index of the line being sung at `elapsed` seconds, -1 before the first."""
    lo, hi = 0, len(synced)
    while lo < hi:
        mid = (lo + hi) // 2
        if synced[mid][0] <= elapsed:
            lo = mid + 1
        else:
            hi = mid
    return lo - 1


def sidecar_lyrics(audio_path):
    """Lyrics from a file next to the song: same name with .lrc or .txt."""
    base = os.path.splitext(audio_path)[0]
    for ext in (".lrc", ".LRC", ".txt"):
        try:
            with open(base + ext, encoding="utf-8", errors="replace") as f:
                text = f.read(TEXT_MAX).strip()
        except OSError:
            continue
        if text:
            return text, os.path.basename(base + ext)
    return "", ""


def lyrics_result(text, source):
    """{"plain", "synced": [(s, line)], "source"} from tag / file / LRCLIB text."""
    synced = parse_lrc(text)
    return {"plain": lrc_plain(synced) if synced else text, "synced": synced, "source": source}


# -- the lookups (run on a worker thread) ------------------------------------------------

class Lookup:
    """One lookup context: get(), the MusicBrainz rate limit, the cache."""

    def __init__(self, get=get_text, cache=None, mb_limit=_MB_LIMIT, langs=("en",), lastfm_key="",
                 sleep=time.sleep, lastfm_guard=None):
        # langs: Wikipedia languages to try; English only (the interface is English, user 2026-11-11)
        self.get, self.cache, self.mb_limit = get, cache, mb_limit
        self.langs, self.lastfm_key = list(langs), lastfm_key
        self.sleep = sleep
        self.lastfm_guard = lastfm_guard or lastfm.GUARD

    def _mb(self, url):
        """A MusicBrainz request: one at a time over all threads, at most one per
        second; a 503 is retried after a pause (Retry-After if given, else 2, 4, 6 s).
        Seen 2026-11-11: MusicBrainz' search servers answered 503 ("search-shed")
        to single requests; the first version gave up at once."""
        with _MB_LOCK:
            for attempt in range(MB_RETRIES + 1):
                self.mb_limit.wait()
                try:
                    return self.get(url)
                except urllib.error.HTTPError as e:
                    if e.code not in (503, 429) or attempt == MB_RETRIES:
                        if e.code in (503, 429):
                            raise Busy("MusicBrainz is busy right now") from e
                        raise
                    try:
                        delay = float((e.headers or {}).get("Retry-After") or 0)
                    except (TypeError, ValueError):
                        delay = 0
                    self.sleep(min(max(delay, 2.0 * (attempt + 1)), 30))

    def _cached(self, key, compute, ttl):
        if self.cache is not None:
            hit = self.cache.get(key, ttl)
            if hit is not None:
                return hit.value
        value = compute()
        if self.cache is not None:
            self.cache.put(key, value)
        return value

    # artist
    def artist_id(self, song):
        mbid = first_tag(song, "musicbrainz_albumartistid", "musicbrainz_artistid")
        if mbid:
            return mbid
        name = song_artist(song)
        if not name:
            return None
        data = self._mb(mb_artist_search_url(name))
        ids = parse_mb_artist_search(data, name) if data else []
        if len(ids) > 1 and song.get("album"):
            for cand in ids[:3]:  # same name: the one with this album
                data = self._mb(mb_rg_search_url(song["album"], artist_id=cand))
                if data and parse_mb_rg_search(data, song["album"]):
                    return cand
        return ids[0] if ids else None

    def album_id(self, song, artist_id=None):
        ids = self.album_ids(song, artist_id)
        return ids[0] if ids else None

    def album_ids(self, song, artist_id=None):
        """Release group candidates, best first: from the tags (one), else the
        search's exact title matches (MusicBrainz often has the same album twice, one
        without links: "Highway 61 Revisited", 2026-11-11)."""
        rg = first_tag(song, "musicbrainz_releasegroupid")
        if rg:
            return [rg]
        rel = first_tag(song, "musicbrainz_albumid")
        if rel:
            data = self._mb(mb_lookup_url("release", rel, "release-groups"))
            rg = parse_release_group_of(data) if data else None
            if rg:
                return [rg]
        album = song.get("album")
        if not album:
            return []
        data = self._mb(mb_rg_search_url(album, song_artist(song), artist_id or ""))
        return parse_mb_rg_search(data, album) if data else []

    def _wiki_of(self, kind, mbid):
        """{"lang", "title", "url", "paragraphs"} (the article's lead) for an MB artist /
        release group, or None; NO_ARTICLE when MusicBrainz links it to Wikidata or
        Wikipedia but not in a wanted language (then the search would only find a
        namesake)."""
        data = self._mb(mb_lookup_url(kind, mbid))
        if not data:
            return None
        qid, titles = parse_links(data)
        linked = bool(qid or titles)
        if qid:
            wd = self.get(wikidata_url(qid))
            if wd:
                titles = {**titles, **parse_sitelinks(wd, qid)}
        pick = pick_article(titles, self.langs)
        if not pick:
            return NO_ARTICLE if linked else None
        lang, title = pick
        page = self.get(wikipedia_url(lang, title))
        paragraphs = parse_extract(page) if page else []
        if not paragraphs:
            return None
        return {"lang": lang, "title": title, "url": article_link(lang, title), "paragraphs": paragraphs}

    def wiki_by_search(self, kind, song):
        """The article found Cantata's way (see search_candidates), checked; or None."""
        artist = song_artist(song)
        album = song.get("album", "") if kind == "album" else None
        if not artist or (kind == "album" and not album):
            return None
        for lang in self.langs:
            data = self.get(wikipedia_search_url(lang, f"{artist} {album}" if album else artist))
            titles = parse_search_titles(data) if data else []
            for title in search_candidates(titles, artist, album)[:SEARCH_TRIES]:
                page = self.get(wikipedia_page_url(lang, title))
                if not page or is_disambiguation(page):
                    continue
                paragraphs = parse_extract(page)
                if paragraphs and plausible(paragraphs, kind, artist):
                    title = page_title(page, title)
                    return {"lang": lang, "title": title, "url": article_link(lang, title), "paragraphs": paragraphs}
        return None

    def _or_search(self, kind, song, key, compute):
        """compute() through the cache; when MusicBrainz is busy, the search instead
        (not cached: MusicBrainz is asked again next time). Still nothing: Busy."""
        try:
            return self._cached(key, compute, ttl=30 * 86400)
        except Busy:
            try:
                wiki = self.wiki_by_search(kind, song)
            except Exception:
                wiki = None  # (Wikipedia failing too: say that MusicBrainz is busy)
            if wiki:
                return {"wiki": wiki, "mbid": None}
            raise

    def artist_wiki(self, song):
        name = song_artist(song)
        key = ("artist-wiki", 3, norm(name), first_tag(song, "musicbrainz_albumartistid", "musicbrainz_artistid"),
               tuple(self.langs))

        def compute():
            mbid = self.artist_id(song)
            wiki = self._wiki_of("artist", mbid) if mbid else None
            if wiki is None:  # MusicBrainz does not know it, or has no link for it
                wiki = self.wiki_by_search("artist", song)
            return {"wiki": wiki or None, "mbid": mbid}
        return self._or_search("artist", song, key, compute)

    def album_wiki(self, song, artist_id=None):
        key = ("album-wiki", 4, norm(song_artist(song)), norm(song.get("album", "")),
               first_tag(song, "musicbrainz_releasegroupid", "musicbrainz_albumid"), tuple(self.langs))

        def compute():
            ids = self.album_ids(song, artist_id)
            linked = False
            for rg in ids[:3]:  # the first one that leads to an article
                wiki = self._wiki_of("release-group", rg)
                if wiki:
                    return {"wiki": wiki, "mbid": rg}
                linked = linked or wiki is NO_ARTICLE
            wiki = None if linked else self.wiki_by_search("album", song)
            return {"wiki": wiki, "mbid": ids[0] if ids else None}
        return self._or_search("album", song, key, compute)

    def similar(self, artist):
        if not self.lastfm_key:
            return None  # no key: nothing to ask
        key = ("similar", 2, norm(artist))  # 2: lists cached with the old limit of 20 are not reused
        url = lastfm_similar_url(artist, self.lastfm_key)
        # lastfm.fetch: spaced out, and lastfm.Limited (not cached) while Last.fm is paused
        return self._cached(key, lambda: parse_lastfm_similar(lastfm.fetch(self.get, url, self.lastfm_key, self.lastfm_guard) or b"{}"),
                            ttl=14 * 86400)

    def online_lyrics(self, song):
        artist = song.get("artist") or song_artist(song)
        title = song.get("title", "")
        if not artist or not title:
            return None
        duration = song.get("duration") or song.get("time")
        key = ("lyrics", norm(artist), norm(title), int(float(duration)) if duration else 0)

        def compute():
            plain, synced = parse_lrclib(self.get(lrclib_get_url(artist, title, song.get("album", ""), duration)))
            if not (plain or synced):
                plain, synced = pick_lrclib(self.get(lrclib_search_url(artist, title)), artist, title, duration)
            if not (plain or synced):
                return None
            return lyrics_result(synced or plain, "LRCLIB")
        return self._cached(key, compute, ttl=30 * 86400)


class Cache:
    """JSON files in a directory, one per key; entries older than their ttl are
    ignored. "Nothing found" (None) is cached too, for a week, so a missing
    article is not asked for on every song."""
    NONE_TTL = 7 * 86400

    def __init__(self, directory):
        self.dir = directory

    def _path(self, key):
        return os.path.join(self.dir, hashlib.sha1(json.dumps(key).encode()).hexdigest() + ".json")

    def get(self, key, ttl):
        try:
            with open(self._path(key), encoding="utf-8") as f:
                entry = json.load(f)
        except (OSError, ValueError):
            return None
        age = time.time() - entry.get("time", 0)
        value = entry.get("value")
        if age > (self.NONE_TTL if value is None else ttl):
            return None
        return _Hit(value)

    def put(self, key, value):
        try:
            os.makedirs(self.dir, exist_ok=True)
            tmp = self._path(key) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"time": time.time(), "value": value}, f)
            os.replace(tmp, self._path(key))
        except OSError:
            pass  # a cache that cannot be written is only slower


class _Hit:
    """A cache hit (also when the cached value is None)."""
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value
