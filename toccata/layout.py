"""Window layout helpers that need no GTK."""


class UserDragFilter:
    """Tells the user's divider drags apart from GTK moving the divider itself.

    GTK changes a Gtk.Paned position on its own when the window is first laid
    out and every time the window is resized. Saving those would overwrite the
    position the user actually chose. A change is the user's own only if the
    window size did not change since the previous notification.

    Call is_user_change(size) from the position notification, with the current
    (width, height) of the window. The very first call is never a user change.
    """

    def __init__(self):
        self._size = None

    def is_user_change(self, size):
        changed = size != self._size
        self._size = size
        return not changed


def divider_key(field, maximized):
    """Settings field that holds divider `field` ("main_split" / "artist_split") for
    the given window state. Maximized and windowed layouts are remembered separately,
    so dragging a divider in a small window leaves the maximized layout alone."""
    return field + "_max" if maximized else field


def divider_position(settings, field, maximized, default):
    """Position to restore for divider `field` in the given window state. A maximized
    layout that was never saved falls back to the windowed one (settings written before
    the two were separate hold a single value), then to `default`."""
    value = getattr(settings, divider_key(field, maximized))
    if not value and maximized:
        value = getattr(settings, field)
    return value or default


class WindowMemory:
    """Decides what part of the window's geometry is worth remembering.

    Rules:
      * the *normal* size (not maximized) and the maximized flag are kept
        separately, so un-maximizing later gives back the old normal size
        instead of a screen-sized window;
      * fullscreen is a temporary mode: it changes nothing;
      * observe() is meant to be called periodically and only believes a
        reading once it has been the same twice in a row, because right
        after maximizing or un-maximizing GTK can report the new state while
        the size is still the old one;
      * capture() is for closing the window, where nothing is in flight.

    Both functions take the settings object, change window_width,
    window_height and window_maximized on it, and return True if anything
    changed (so the caller knows to save).
    """

    def __init__(self):
        self._last = None

    def observe(self, settings, width, height, maximized, fullscreen):
        sample = (width, height, maximized, fullscreen)
        stable = sample == self._last
        self._last = sample
        return self.capture(settings, width, height, maximized, fullscreen) if stable else False

    @staticmethod
    def capture(settings, width, height, maximized, fullscreen):
        from .settings import MAX_WINDOW
        if fullscreen:
            return False
        changed = False
        if settings.window_maximized != bool(maximized):
            settings.window_maximized = bool(maximized)
            changed = True
        if (not maximized and 1 <= width <= MAX_WINDOW and 1 <= height <= MAX_WINDOW
                and (settings.window_width, settings.window_height) != (width, height)):
            settings.window_width, settings.window_height = width, height
            changed = True
        return changed
