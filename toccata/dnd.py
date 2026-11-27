"""Drag and drop planning for the queue. No GTK, so it can be tested."""


def plan_moves(queue_ids, moved_ids, insert_at):
    """Moves that put `moved_ids` (kept in their queue order) just before the
    entry that is at index `insert_at` of the current queue (len(queue_ids) =
    the end). Returns [(songid, new_position)] to send as `moveid`, in order.

    The anchor is the first entry at or after `insert_at` that is not itself
    being moved, so dropping a selection onto one of its own rows works."""
    moved = set(moved_ids)
    order = [i for i in queue_ids if i in moved]
    insert_at = max(0, min(insert_at, len(queue_ids)))
    anchor = next((i for i in queue_ids[insert_at:] if i not in moved), None)
    current = list(queue_ids)
    moves = []
    for songid in order:
        current.remove(songid)
        to = len(current) if anchor is None else current.index(anchor)
        current.insert(to, songid)
        moves.append((songid, to))
    return moves


def apply_moves(queue_ids, moves):
    """What the queue looks like after `moves` (used by the tests)."""
    current = list(queue_ids)
    for songid, to in moves:
        current.remove(songid)
        current.insert(to, songid)
    return current


def insert_index(row_index, y_in_row, row_height, n_rows):
    """Drop position for a pointer over row `row_index`: before the row in its
    upper half, after it in the lower half; the end if over no row."""
    if row_index is None:
        return n_rows
    return row_index + (1 if row_height and y_in_row > row_height / 2 else 0)


def line_y(insert_at, row_index, row_top, row_bottom):
    """Where to draw the drop marker for a pointer over the visible row
    `row_index` (top/bottom in list coordinates): on the row's top edge when
    the drop goes before it, on its bottom edge otherwise (after the row, or
    after the whole album when the row is a collapsed album)."""
    return row_top if insert_at <= row_index else row_bottom


def track_name(song):
    """A song dict's title for the drag label (the file name if it has none)."""
    title = (song.get("title") or "").strip()
    return title or (song.get("file", "").rstrip("/").rsplit("/", 1)[-1]) or "Track"


def drag_label(names, noun, plural=None):
    """What a drag carries as text, which GTK shows at the pointer: the one item's
    name, else "N <plural>" (it used to be an internal tag like "toccata-artist")."""
    names = list(names)
    if len(names) == 1 and (names[0] or "").strip():
        return names[0].strip()
    n = len(names)
    return f"{n} {noun if n == 1 else (plural or noun + 's')}"


def tracks_label(songs):
    return drag_label([track_name(s) for s in songs], "track")
