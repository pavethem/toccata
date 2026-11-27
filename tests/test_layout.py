import unittest

from toccata.layout import UserDragFilter, WindowMemory, divider_key, divider_position
from toccata.settings import MAX_WINDOW, Settings


class TestUserDragFilter(unittest.TestCase):
    def test_first_notification_is_never_a_user_change(self):
        # GTK positions the divider while laying the window out for the first time
        self.assertFalse(UserDragFilter().is_user_change((1200, 700)))

    def test_change_at_unchanged_size_is_a_drag(self):
        f = UserDragFilter()
        f.is_user_change((1200, 700))
        self.assertTrue(f.is_user_change((1200, 700)))

    def test_continuous_drag_stays_a_drag(self):
        f = UserDragFilter()
        f.is_user_change((1200, 700))
        self.assertTrue(all(f.is_user_change((1200, 700)) for _ in range(20)))

    def test_change_caused_by_resizing_the_window_is_ignored(self):
        f = UserDragFilter()
        f.is_user_change((1200, 700))
        self.assertFalse(f.is_user_change((1500, 700)))  # width changed: GTK moved it
        self.assertFalse(f.is_user_change((1500, 900)))  # height changed

    def test_drag_after_a_resize_counts_again(self):
        f = UserDragFilter()
        f.is_user_change((1200, 700))
        f.is_user_change((1500, 700))
        self.assertTrue(f.is_user_change((1500, 700)))

    def test_startup_sequence(self):
        # layout at default size, compositor then changes the size, user drags later
        f = UserDragFilter()
        seen = [f.is_user_change(s) for s in [(1200, 700), (1200, 700 - 1), (1100, 600), (1100, 600), (1100, 600)]]
        self.assertEqual(seen, [False, False, False, True, True])

    def test_each_divider_has_its_own_state(self):
        a, b = UserDragFilter(), UserDragFilter()
        a.is_user_change((1200, 700))
        self.assertTrue(a.is_user_change((1200, 700)))
        self.assertFalse(b.is_user_change((1200, 700)))  # b has not seen a size yet


class TestWindowMemory(unittest.TestCase):
    def setUp(self):
        self.s = Settings()
        self.m = WindowMemory()

    def seen(self, w, h, maximized=False, fullscreen=False):
        return self.m.observe(self.s, w, h, maximized, fullscreen)

    def geometry(self):
        return (self.s.window_width, self.s.window_height, self.s.window_maximized)

    # -- observe(): periodic sampling
    def test_a_single_reading_is_not_believed(self):
        self.assertFalse(self.seen(1000, 600))
        self.assertEqual(self.geometry(), (0, 0, False))

    def test_a_repeated_reading_is_remembered(self):
        self.seen(1000, 600)
        self.assertTrue(self.seen(1000, 600))
        self.assertEqual(self.geometry(), (1000, 600, False))

    def test_unchanged_geometry_reports_no_change(self):
        self.seen(1000, 600); self.seen(1000, 600)
        self.assertFalse(self.seen(1000, 600))

    def test_resizing_is_remembered_once_it_settles(self):
        self.seen(1000, 600); self.seen(1000, 600)
        self.seen(1100, 650)                       # still being dragged
        self.assertEqual(self.geometry(), (1000, 600, False))
        self.assertTrue(self.seen(1100, 650))
        self.assertEqual(self.geometry(), (1100, 650, False))

    def test_maximizing_sets_the_flag_but_keeps_the_normal_size(self):
        self.seen(1000, 600); self.seen(1000, 600)
        self.seen(1920, 1080, maximized=True)
        self.assertTrue(self.seen(1920, 1080, maximized=True))
        self.assertEqual(self.geometry(), (1000, 600, True))   # not 1920x1080

    def test_unmaximizing_clears_the_flag_and_takes_the_restored_size(self):
        self.seen(1920, 1080, maximized=True); self.seen(1920, 1080, maximized=True)
        self.seen(1000, 600); self.assertTrue(self.seen(1000, 600))
        self.assertEqual(self.geometry(), (1000, 600, False))

    def test_stale_size_during_unmaximize_is_never_stored(self):
        stored = []

        class Recording(Settings):  # remembers every width that is ever written
            def __setattr__(self, name, value):
                if name == "window_width":
                    stored.append(value)
                super().__setattr__(name, value)
        self.s = Recording()
        self.seen(1000, 600); self.seen(1000, 600)
        self.seen(1920, 1080, maximized=True); self.seen(1920, 1080, maximized=True)
        # state already says "normal" but the size is still the maximized one for one sample:
        self.seen(1920, 1080, maximized=False)
        self.seen(1000, 600, maximized=False)
        self.seen(1000, 600, maximized=False)
        self.assertNotIn(1920, stored)  # not even temporarily
        self.assertEqual(self.geometry(), (1000, 600, False))

    def test_fullscreen_changes_nothing(self):
        self.seen(1000, 600); self.seen(1000, 600)
        self.seen(1920, 1080, fullscreen=True)
        self.assertFalse(self.seen(1920, 1080, fullscreen=True))
        self.assertEqual(self.geometry(), (1000, 600, False))

    def test_maximized_then_fullscreen_keeps_the_maximized_flag(self):
        self.seen(1920, 1080, maximized=True); self.seen(1920, 1080, maximized=True)
        self.seen(1920, 1080, maximized=True, fullscreen=True)
        self.assertFalse(self.seen(1920, 1080, maximized=True, fullscreen=True))
        self.assertTrue(self.s.window_maximized)

    def test_nonsense_sizes_are_ignored(self):
        for w, h in [(0, 0), (-5, 600), (1000, 0), (MAX_WINDOW + 1, 600), (1000, MAX_WINDOW + 1)]:
            self.seen(w, h); self.assertFalse(self.seen(w, h), (w, h))
        self.assertEqual(self.geometry(), (0, 0, False))

    def test_limits_are_inclusive(self):
        self.seen(1, MAX_WINDOW); self.assertTrue(self.seen(1, MAX_WINDOW))
        self.assertEqual(self.geometry(), (1, MAX_WINDOW, False))

    # -- capture(): used when the window closes
    def test_capture_needs_no_stability(self):
        self.assertTrue(WindowMemory.capture(self.s, 1000, 600, False, False))
        self.assertEqual(self.geometry(), (1000, 600, False))

    def test_capture_when_closing_maximized_keeps_the_normal_size(self):
        self.s.window_width, self.s.window_height = 1000, 600
        self.assertTrue(WindowMemory.capture(self.s, 1920, 1080, True, False))
        self.assertEqual(self.geometry(), (1000, 600, True))

    def test_capture_when_closing_fullscreen_changes_nothing(self):
        self.s.window_width, self.s.window_height = 1000, 600
        self.assertFalse(WindowMemory.capture(self.s, 1920, 1080, False, True))
        self.assertEqual(self.geometry(), (1000, 600, False))

    def test_capture_reports_no_change_when_nothing_differs(self):
        WindowMemory.capture(self.s, 1000, 600, False, False)
        self.assertFalse(WindowMemory.capture(self.s, 1000, 600, False, False))

    def test_capture_changes_only_the_window_fields(self):
        self.s.last_artist, self.s.main_split = "Band", 700
        WindowMemory.capture(self.s, 1000, 600, True, False)
        self.assertEqual((self.s.last_artist, self.s.main_split), ("Band", 700))


class TestDividerPerWindowState(unittest.TestCase):
    def test_maximized_and_windowed_use_different_fields(self):
        self.assertEqual(divider_key("main_split", False), "main_split")
        self.assertEqual(divider_key("main_split", True), "main_split_max")
        self.assertEqual(divider_key("artist_split", True), "artist_split_max")

    def test_each_state_restores_its_own_position(self):
        s = Settings(main_split=500, main_split_max=1100)
        self.assertEqual(divider_position(s, "main_split", False, 640), 500)
        self.assertEqual(divider_position(s, "main_split", True, 640), 1100)

    def test_maximized_falls_back_to_the_windowed_value(self):
        # settings saved before the split: the single value was the maximized layout too
        s = Settings(artist_split=250)
        self.assertEqual(divider_position(s, "artist_split", True, 190), 250)

    def test_windowed_does_not_take_the_maximized_value(self):
        s = Settings(artist_split_max=400)
        self.assertEqual(divider_position(s, "artist_split", False, 190), 190)

    def test_default_when_nothing_saved(self):
        self.assertEqual(divider_position(Settings(), "main_split", True, 640), 640)


if __name__ == "__main__":
    unittest.main()
