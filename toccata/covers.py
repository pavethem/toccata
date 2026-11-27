"""Background cover fetching with a small LRU cache. No GTK imports, so it is
unit-testable; the UI injects `decode` (bytes -> Gdk.Texture) and `dispatch`
(run a function on the GTK main thread).

Covers are cached per album folder, because MPD's folder art is per directory.
"Folder has no cover" is cached for MISS_TTL seconds only (a cover may appear
later, e.g. another program downloads one and saves it as cover.jpg); a network
failure is NOT cached, so the next request retries.

Where a cover comes from, in order:
  1. MPD folder art (`albumart`: cover.jpg etc. in the song's folder);
  2. MPD embedded picture (`readpicture`);
  3. the album folder itself, if the music folder is readable here (e.g. an NFS
     mount): cover/albumart/folder, "<album artist> - <album>", "<album>"
     (.jpg/.png/.jpeg, names compared ignoring case), then any image in the
     folder (by name), then the same in subfolders. MPD's albumart only knows a
     few fixed names, so e.g. "Tago Mago.jpg" or "Can - Saw Delight.jpg" are
     only found this way.
"""
import hashlib
import os
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from .mpd_client import MPDError


MISS_TTL = 600  # seconds a "no cover" answer is believed


IMAGE_EXTS = (".jpg", ".png", ".jpeg")
MAX_IMAGE = 32 * 1024 * 1024


def folder_cover_names(song):
    """Preferred file names for a folder cover, in order (lower case)."""
    artist = song.get("albumartist") or song.get("artist") or ""
    album = song.get("album") or ""
    stems = ["cover", "albumart", "folder"]
    if album:
        if artist:
            stems.append(f"{artist} - {album}")
        stems.append(album)
    return [(stem + ext).casefold() for stem in stems for ext in (".jpg", ".png")]


def _images(folder):
    try:
        with os.scandir(folder) as it:
            entries = [(e.name, e.path) for e in it if e.is_file() and e.name.casefold().endswith(IMAGE_EXTS)]
    except OSError:
        return []
    return sorted(entries)


def _subfolders(folder):
    try:
        with os.scandir(folder) as it:
            return sorted(e.path for e in it if e.is_dir() and not e.name.startswith("."))
    except OSError:
        return []


def folder_cover_candidates(folder, song):
    """Image files to try for `song`, in the order of the module doc."""
    out = []
    names = folder_cover_names(song)
    for level in [folder] + _subfolders(folder):
        images = _images(level)
        by_name = {name.casefold(): path for name, path in images}
        out += [by_name[n] for n in names if n in by_name]
        out += [path for _name, path in images]
    seen, unique = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def song_folder(music_dir, uri):
    """Absolute folder of a song inside music_dir, or None (also for paths that
    would leave the music folder)."""
    if not music_dir or not uri or "://" in uri or os.path.isabs(uri):
        return None
    root = os.path.realpath(music_dir)
    folder = os.path.realpath(os.path.join(root, os.path.dirname(uri)))
    if folder != root and not folder.startswith(root + os.sep):
        return None
    return folder if os.path.isdir(folder) else None


def _read(path, max_bytes=MAX_IMAGE):
    try:
        if os.path.getsize(path) <= max_bytes:
            with open(path, "rb") as f:
                return f.read()
    except OSError:
        pass
    return None


def read_folder_cover(music_dir, uri, song, decodes=lambda data: True):
    """Bytes of the first usable cover image in the song's folder, or None.
    `decodes(data)` lets the caller skip files that are not real images."""
    folder = song_folder(music_dir, uri)
    if folder is None:
        return None
    for path in folder_cover_candidates(folder, song or {}):
        data = _read(path)
        if data and decodes(data):
            return data
    return None


DISK_MAX_AGE = 30 * 24 * 3600  # seconds; older thumbnails are fetched again (the cover may have changed)
BINARY_LIMIT = 1024 * 1024     # bytes per binary chunk; MPD's default 8 KiB needs a round trip per 8 KiB


# -- setting a cover by hand ("Set Cover..."), as Cantata does with "store covers in MPD dir"

COVER_EXTS = (".jpg", ".png", ".webp")  # what MPD's albumart finds as cover.<ext>


class CoverWriteError(Exception):
    pass


def album_folders(music_dir, uris):
    """Distinct folders of an album's songs inside the music folder (a multi-disc
    album may have one per disc), in order; songs outside it are ignored."""
    out = []
    for uri in uris:
        folder = song_folder(music_dir, uri)
        if folder is not None and folder not in out:
            out.append(folder)
    return out


def existing_covers(folder):
    """cover.* files MPD would pick up in this folder (any case)."""
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return sorted(os.path.join(folder, n) for n in names
                  if n.lower().startswith("cover.") and os.path.splitext(n)[1].lower() in COVER_EXTS + (".jpeg",))


def cover_extension(data):
    """File extension for image bytes, from their signature; None if not JPEG/PNG/WebP."""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def write_cover(folder, data):
    """Save image bytes as cover.<ext> in `folder`. An existing cover.* is kept as
    <name>.old (one level; MPD ignores it), so the change can be undone by hand.
    Returns the new file's path. Raises CoverWriteError with a readable message."""
    ext = cover_extension(data)
    if ext is None:
        raise CoverWriteError("Not a JPEG, PNG or WebP image")
    if not os.access(folder, os.W_OK):
        raise CoverWriteError(f"No permission to write in {folder}")
    target = os.path.join(folder, "cover" + ext)
    tmp = os.path.join(folder, ".cover-new" + ext)
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        for old in existing_covers(folder):  # also cover.png when writing cover.jpg: MPD would prefer it
            os.replace(old, old + ".old")
        os.replace(tmp, target)
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise CoverWriteError(f"Could not save the cover: {e.strerror or e}")
    return target


def thumb_file_name(key, size):
    return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest() + f"-{size}.png"


class CoverFetcher:
    """Fetches covers on one background connection.

    decode(data) -> image and thumbnail(data, size) -> PNG bytes (or None) are
    supplied by the UI and run on the worker thread (they must be thread-safe;
    GdkPixbuf/Gdk.Texture are). request(size=N) gives an image scaled so its
    shorter side is N pixels, and keeps it in `disk_dir` as a PNG, so a list of
    hundreds of albums costs neither MPD round trips nor much memory the next
    time; size=None gives the full image (player bar, preview).
    """

    def __init__(self, client, decode, dispatch, cache_size=256, miss_ttl=MISS_TTL,
                 clock=time.monotonic, music_dir=lambda: None, thumbnail=None, disk_dir=None,
                 wall_clock=time.time):
        self._client = client      # used only from the single worker thread
        self._decode = decode
        self._thumbnail = thumbnail
        self._dispatch = dispatch
        self._cache_size = cache_size
        self._music_dir = music_dir          # callable: MPD's music folder as seen here, or None
        self._miss_ttl = miss_ttl
        self._clock = clock
        self._wall_clock = wall_clock
        self._disk_dir = disk_dir
        # (folder, size) -> (decoded image | None, time of a miss) (main thread only)
        self._cache = OrderedDict()
        self._limit_set = False   # binarylimit sent on the current connection
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cover")

    @staticmethod
    def key_for(uri):
        return os.path.dirname(uri)

    def request(self, uri, callback, wanted=None, song=None, size=None):
        """Call callback(image_or_None) on the main thread (via dispatch).

        wanted: optional callable. If it returns False by the time the request
        reaches the front of the queue, the fetch is skipped and callback is
        never called (e.g. the user already switched to another artist).
        song: the song's tags (dict), used to find a cover by album name in the
        song's folder when MPD has none.
        size: thumbnail size (shorter side, pixels), or None for the full image.
        """
        key = (self.key_for(uri), size)
        if key in self._cache:
            image, missed_at = self._cache[key]
            if image is not None or self._clock() - missed_at < self._miss_ttl:
                self._cache.move_to_end(key)
                callback(image)
                return
            del self._cache[key]  # an old "no cover": look again
        self._pool.submit(self._work, uri, key, callback, wanted, song)

    def _disk_path(self, key):
        folder, size = key
        if size is None or not self._disk_dir or self._thumbnail is None:
            return None
        return os.path.join(self._disk_dir, thumb_file_name(folder, size))

    def _read_disk(self, path):
        try:
            if self._wall_clock() - os.path.getmtime(path) > DISK_MAX_AGE:
                return None
            with open(path, "rb") as f:
                return f.read()
        except OSError:
            return None

    def _write_disk(self, path, data):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except OSError:
            pass  # a cache that cannot be written is only slower

    def _fetch(self, uri, song):  # worker thread: (bytes or None, ok)
        data, ok = None, False
        for _attempt in (1, 2):  # second try covers a connection MPD dropped
            try:
                if not self._client.connected:
                    self._client.connect()
                    self._limit_set = False
                if not self._limit_set:
                    self._limit_set = True
                    setter = getattr(self._client, "set_binary_limit", None)
                    if setter is not None:
                        setter(BINARY_LIMIT)
                data = self._client.cover(uri)
                ok = True
                break
            except (OSError, MPDError):
                self._client.close()
        if data is None:
            local = read_folder_cover(self._music_dir(), uri, song)
            if local is not None:
                data, ok = local, True
        return data, ok

    def _work(self, uri, key, callback, wanted=None, song=None):  # worker thread
        if wanted is not None and not wanted():
            return
        path = self._disk_path(key)
        data = self._read_disk(path) if path else None
        ok = data is not None
        if data is None:
            data, ok = self._fetch(uri, song)
            if data and path:
                try:
                    data = self._thumbnail(data, key[1])
                except Exception:
                    data = None
                if data:
                    self._write_disk(path, data)
        image = None
        if data:
            try:
                image = self._decode(data)
            except Exception:  # undecodable image: same as "no cover"
                image = None
        self._dispatch(self._finish, key, image, ok, callback)

    def _finish(self, key, image, ok, callback):  # main thread
        if ok:
            self._cache[key] = (image, self._clock())
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        callback(image)

    def forget(self, folder):
        """A folder's cover changed: drop it from memory (all sizes) and from the
        thumbnail cache on disk, so the next request fetches it again."""
        for key in [k for k in self._cache if k[0] == folder]:
            del self._cache[key]
        if self._disk_dir:
            prefix = thumb_file_name(folder, 0).rsplit("-", 1)[0] + "-"
            try:
                for name in os.listdir(self._disk_dir):
                    if name.startswith(prefix):
                        os.remove(os.path.join(self._disk_dir, name))
            except OSError:
                pass

    def shutdown(self):
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._client.close()


def cover_fit_rect(box_w, box_h, img_w, img_h):
    """(x, y, w, h) that scales an image to *fill* the box, centred. The parts
    sticking out (negative x/y, or beyond the box) are meant to be clipped."""
    if min(box_w, box_h, img_w, img_h) <= 0:
        return (0.0, 0.0, float(box_w), float(box_h))
    scale = max(box_w / img_w, box_h / img_h)
    w, h = img_w * scale, img_h * scale
    return ((box_w - w) / 2, (box_h - h) / 2, w, h)


def preview_size(img_w, img_h, max_w, max_h, min_side=320):
    """(w, h) for the large cover preview: the whole image, aspect kept, as big as
    fits in max_w x max_h but not enlarged beyond its own size - except that
    small images are blown up until their longer side is min_side."""
    if min(img_w, img_h) <= 0 or min(max_w, max_h) <= 0:
        return (0, 0)
    fit = min(max_w / img_w, max_h / img_h)
    grow = max(1.0, min_side / max(img_w, img_h))
    scale = min(fit, grow)
    return (max(1, round(img_w * scale)), max(1, round(img_h * scale)))
