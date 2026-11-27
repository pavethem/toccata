# SPDX-License-Identifier: GPL-2.0-or-later
"""Undo / redo of play-queue changes (GTK-free).

A step is the queue as a list of URIs before a change made in Toccata (remove,
move, add, clear, play). Undo restores that list on the server; redo goes back.
Restoring does not clear and re-add the queue: entries that stay keep their MPD
ids, so the playing song keeps playing (restore_commands)."""

from collections import defaultdict

UNDO_STEPS = 10  # Cantata's default number of undo steps


class QueueHistory:
    def __init__(self, limit=UNDO_STEPS):
        self.limit = limit
        self._undo = []
        self._redo = []

    def record(self, before):
        """A change is about to be made to the queue `before` (list of URIs)."""
        before = list(before)
        if self._undo and self._undo[-1] == before:
            return  # e.g. a change that failed: one step is enough
        self._undo.append(before)
        del self._undo[:-self.limit]
        self._redo.clear()

    def undo(self, current):
        """The queue to restore instead of `current`, or None. Steps equal to the
        current queue (a failed or no-op change) are skipped."""
        return self._step(self._undo, self._redo, list(current))

    def redo(self, current):
        return self._step(self._redo, self._undo, list(current))

    def _step(self, source, other, current):
        while source and source[-1] == current:
            source.pop()
        if not source:
            return None
        other.append(current)  # (steps only move between the stacks: no limit needed)
        return source.pop()

    @property
    def can_undo(self):
        return bool(self._undo)

    @property
    def can_redo(self):
        return bool(self._redo)

    def clear(self):
        self._undo.clear()
        self._redo.clear()


def restore_commands(current, target, keep_id=None):
    """MPD commands that turn the queue `current` [(songid, uri)] into `target`
    [uri]. Entries are reused by URI (`keep_id`, the playing entry, first), so
    they keep their ids; the others are deleted. Then the reused entries that are
    out of order and the missing URIs are put in place in target order. Returns
    [("deleteid", id) | ("moveid", id, pos) | ("add", uri[, pos])] (pos as str)."""
    free = defaultdict(list)
    for sid, uri in current:
        if sid == keep_id:
            free[uri].insert(0, sid)
        else:
            free[uri].append(sid)
    wanted = [free[uri].pop(0) if free.get(uri) else None for uri in target]  # id or None (add)
    used = {sid for sid in wanted if sid is not None}
    commands = [("deleteid", sid) for sid, _uri in current if sid not in used]
    order = [sid for sid, _uri in current if sid in used]  # the queue after the deletes
    rank = {sid: n for n, sid in enumerate(s for s in wanted if s is not None)}
    stable = _longest_increasing([rank[sid] for sid in order])
    stable = {order[n] for n in stable}
    # the entries out of order go to the end first; then every target position is
    # filled in ascending order: everything before it is final, and the next stable
    # entry is already next (the stable ones are in target order)
    for sid in order:
        if sid not in stable:
            commands.append(("moveid", sid, str(len(order) - 1)))
    length = len(order)
    for pos, (sid, uri) in enumerate(zip(wanted, target)):
        if sid is None:
            # appending needs no position (works on MPD < 0.23 too)
            commands.append(("add", uri) if pos == length else ("add", uri, str(pos)))
            length += 1
        elif sid not in stable:
            commands.append(("moveid", sid, str(pos)))
    return commands


def _longest_increasing(values):
    """Indexes of a longest strictly increasing subsequence of `values`."""
    import bisect
    tails, tail_idx, prev = [], [], [None] * len(values)
    for i, v in enumerate(values):
        k = bisect.bisect_left(tails, v)
        if k == len(tails):
            tails.append(v)
            tail_idx.append(i)
        else:
            tails[k] = v
            tail_idx[k] = i
        prev[i] = tail_idx[k - 1] if k else None
    out, i = [], tail_idx[-1] if tail_idx else None
    while i is not None:
        out.append(i)
        i = prev[i]
    return out[::-1]
