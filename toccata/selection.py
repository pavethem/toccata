"""Click selection for hand-made lists (tracks inside opened albums): plain click
selects one, Ctrl+click toggles, Shift+click selects a range from the anchor,
Ctrl+Shift+click adds that range. No GTK here; items are any objects."""


def click(selected, anchor, item, order, ctrl=False, shift=False):
    """New (selection in `order` order, anchor) after clicking `item`.
    order: every selectable item as shown, top to bottom."""
    if shift and anchor in order and item in order:
        i, j = order.index(anchor), order.index(item)
        span = order[min(i, j):max(i, j) + 1]
        chosen = set(map(id, span))
        if ctrl:
            chosen |= set(map(id, selected))
        return [x for x in order if id(x) in chosen], anchor
    if ctrl:
        if any(x is item for x in selected):
            rest = [x for x in selected if x is not item]
            return [x for x in order if any(x is y for y in rest)], item
        return [x for x in order if x is item or any(x is y for y in selected)], item
    return [item], item


def defer_to_release(selected, item, ctrl=False, shift=False):
    """True when a plain press lands on an item that is part of a selection of
    several: the press must not reduce the selection to that item, because the
    user may be starting a drag of the whole selection. The plain click is then
    applied on button release instead (and dropped if a drag started)."""
    if ctrl or shift or len(selected) < 2:
        return False
    return any(x is item for x in selected)


def select_all_target(view, in_text, in_queue, in_library):
    """Where Ctrl+A goes: "queue" (every queue entry), "albums" (every album of the
    artist shown in the Artists view) or None (leave the key alone: a text field
    selects its text, other lists use GTK's own Ctrl+A).
    in_library: the focus is in the Artists view's panes, or nowhere."""
    if in_text:
        return None
    if in_queue:
        return "queue"
    if view == "library" and in_library:
        return "albums"
    return None


# -- click-and-drag selection ("rubber band") ----------------------------------------
# A drag that starts on empty space of a list (below the last row, in the margins)
# selects the rows it covers. A drag that starts on a row stays drag-and-drop
# (user decision 2026-11-12).

BAND_THRESHOLD = 6  # px of movement before a press on empty space becomes a band


def band_positions(anchor, current, n):
    """Positions 0..n-1 of a list a band covers, from the row under the press
    (`anchor`) to the row under the pointer (`current`); n stands for the empty
    space below the last row."""
    lo, hi = min(anchor, current), max(anchor, current)
    return list(range(max(lo, 0), min(hi + 1, n)))


def band_hits(spans, y0, y1):
    """Indices of the spans (top, bottom) the vertical range y0..y1 touches."""
    lo, hi = min(y0, y1), max(y0, y1)
    return [i for i, (top, bottom) in enumerate(spans) if top <= hi and bottom >= lo]


def band_targets(album_spans, track_spans, y0, y1):
    """What a band over the Artists view's album list selects: ("albums", indices)
    when it touches album headers, else ("tracks", indices) of the opened albums'
    track rows it touches (one kind of selection at a time, as with clicks)."""
    albums = band_hits(album_spans, y0, y1)
    if albums:
        return "albums", albums
    return "tracks", band_hits(track_spans, y0, y1)


def band_merge(base, hits, order, ctrl):
    """The selection while banding: the hits, plus with Ctrl what was selected
    before the drag; in `order` order."""
    chosen = set(map(id, hits))
    if ctrl:
        chosen |= set(map(id, base))
    return [x for x in order if id(x) in chosen]


def edge_scroll(y, height, zone=32, top_speed=80):
    """Pixels to scroll per step (30 ms) while a band's pointer is near or beyond the
    top (negative) or bottom (positive) edge of a view `height` high; 0 elsewhere.
    Slow at the edge, faster the further out the pointer goes."""
    if height <= 0:
        return 0
    zone = min(zone, height / 4)
    if y < zone:
        depth = zone - y
        sign = -1
    elif y > height - zone:
        depth = y - (height - zone)
        sign = 1
    else:
        return 0
    return sign * max(1, min(top_speed, round(depth / 2)))
