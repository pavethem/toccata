"""Which queue entry to highlight as "now playing". No GTK, so it can be tested."""


def now_playing(status):
    """(song id, state) of the song to highlight, or (None, "") for none.

    Only while playing or paused: when stopped MPD still names a current song,
    but nothing is playing. Ids are the queue-unique `songid` strings, so the
    same file queued twice highlights only the entry actually in use."""
    state = status.get("state")
    songid = status.get("songid")
    if state in ("play", "pause") and songid:
        return songid, state
    return None, ""


class NowPlayingMarker:
    """Works out which songs need their highlight changed after a status or
    queue update, so the UI only touches the rows that changed."""

    def __init__(self):
        self._song, self._state = None, ""

    def reset(self):
        self._song, self._state = None, ""

    def update(self, songs_by_id, status):
        """Returns [(song, state), ...] to apply; state "" removes the highlight,
        "play" / "pause" set it. Call again after the queue was rebuilt: the new
        song objects are then marked and the old ones cleared."""
        songid, state = now_playing(status)
        target = songs_by_id.get(songid) if songid is not None else None
        if target is None:
            state = ""
        previous, previous_state = self._song, self._state
        changes = []
        if previous is not None and previous is not target:
            changes.append((previous, ""))
        if target is not None and (target is not previous or state != previous_state):
            changes.append((target, state))
        self._song, self._state = target, state
        return changes


def reuse_entries(old, new, make):
    """Queue entries for MPD's new queue `new` (song dicts): the old entry object
    is kept for every song with the same queue id and the same tags (its
    position may differ; it gets the new dict), anything else is make(dict).

    Why: the list view keeps its scroll anchor on an entry *object*. When every
    entry was new on each refresh, GTK guessed the anchor's place
    proportionally: after removing the bottom of the queue and adding albums
    again, row 3 of 4 became row ~355 of 474, the view jumped deep into the
    list, and in the grouped queue it was left empty (user report 2026-11-11).
    Kept objects also keep their now-playing mark (NowPlayingMarker)."""
    by_id = {}
    for entry in old:
        by_id.setdefault(entry.data.get("id"), entry)
    out, used = [], set()
    for song in new:
        entry = by_id.get(song.get("id"))
        if entry is not None and id(entry) not in used and _same_song(entry.data, song):
            entry.data = song
            used.add(id(entry))
            out.append(entry)
        else:
            out.append(make(song))
    return out


def entry_to_play(queue, uri, current_pos=None):
    """The queue id to play for a track clicked in the Info view (Cantata plays it,
    and adds it first if it is not in the queue): the first entry of `uri` from the
    current position on (the playing one itself restarts), else the first one at all;
    None if it is not in the queue.
    queue: [(songid, uri)] in queue order."""
    hits = [(i, sid) for i, (sid, u) in enumerate(queue) if u == uri]
    if not hits:
        return None
    if current_pos is not None:
        for i, sid in hits:
            if i >= current_pos:
                return sid
    return hits[0][1]


def changed_range(old, new):
    """(start, n_remove, insert) that turns list `old` into `new` with a single
    splice: the entries both start with (and end with) — the same objects, see
    reuse_entries — stay untouched; only the part in between is replaced.
    Adding albums is then a pure insert and removing them a pure removal, so GTK
    never has to relocate rows it already shows (it lost one in the grouped queue
    when everything was replaced, user report 2026-11-11)."""
    n_old, n_new = len(old), len(new)
    start = 0
    while start < n_old and start < n_new and old[start] is new[start]:
        start += 1
    end = 0  # common tail, not overlapping the common head
    while end < n_old - start and end < n_new - start and old[n_old - 1 - end] is new[n_new - 1 - end]:
        end += 1
    return start, n_old - start - end, new[start:n_new - end]


def _same_song(a, b):
    return {k: v for k, v in a.items() if k != "pos"} == {k: v for k, v in b.items() if k != "pos"}


def db_update_job(status):
    """MPD's database update job id while an update (rescan) runs, else None.
    MPD reports it in `status` as `updating_db: <id>`; the `update` idle event
    fires when an update starts and when it ends."""
    job = (status or {}).get("updating_db")
    return str(job) if job not in (None, "") else None


def db_update_text(job):
    """(label, tooltip) for the player bar indicator, or (None, None)."""
    if job is None:
        return None, None
    return "Updating database…", f"MPD is rescanning its music database (update job {job})"


PLAY_OPTIONS = ("repeat", "single", "random", "consume")


def play_options(status):
    """Which of MPD's play options are on: repeat, single, random (shuffle), consume.
    "oneshot" (single/consume, MPD >= 0.21/0.24) counts as on."""
    status = status or {}
    return {k: status.get(k) in ("1", "oneshot") for k in PLAY_OPTIONS}


class DbReloadGate:
    """Holds the library reload back while MPD's database update runs.

    MPD sends `database` whenever an update job changed something, possibly
    several times during one rescan (one job per `update <uri>`), and `update`
    when a job starts and when the last one ends. Reloading on every
    `database` event refreshed the library views mid-update (user request
    2026-11-12): instead note the change and reload once, when `status` no
    longer shows `updating_db`."""

    def __init__(self):
        self.pending = False      # the database changed since the last reload
        self.queue_too = False    # also refresh the queue (its tags) then

    def changed(self, queue_too=False):
        """The database changed, or will change (we asked MPD to rescan)."""
        self.pending = True
        self.queue_too = self.queue_too or queue_too

    def take(self, job):
        """With `job` = db_update_job(status): None while nothing is due or the
        update still runs, else whether the queue needs a refresh too (the
        reload is then due and the gate is cleared)."""
        if not self.pending or job is not None:
            return None
        queue_too = self.queue_too
        self.pending = self.queue_too = False
        return queue_too

    def reset(self):
        """Everything was just reloaded (e.g. after a reconnect)."""
        self.pending = self.queue_too = False
