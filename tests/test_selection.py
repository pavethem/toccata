import unittest

from toccata.selection import click, defer_to_release, select_all_target

ORDER = ["a", "b", "c", "d", "e"]


class TestClick(unittest.TestCase):
    def test_plain_click_selects_one(self):
        self.assertEqual(click(["a", "b"], "a", "c", ORDER), (["c"], "c"))

    def test_ctrl_toggles_and_keeps_order(self):
        self.assertEqual(click(["d"], "d", "b", ORDER, ctrl=True), (["b", "d"], "b"))
        self.assertEqual(click(["b", "d"], "b", "d", ORDER, ctrl=True), (["b"], "d"))

    def test_shift_range_from_anchor_either_way(self):
        self.assertEqual(click(["b"], "b", "d", ORDER, shift=True), (["b", "c", "d"], "b"))
        self.assertEqual(click(["d"], "d", "a", ORDER, shift=True), (["a", "b", "c", "d"], "d"))

    def test_shift_replaces_ctrl_shift_adds(self):
        self.assertEqual(click(["e"], "b", "c", ORDER, shift=True), (["b", "c"], "b"))
        self.assertEqual(click(["e"], "b", "c", ORDER, ctrl=True, shift=True), (["b", "c", "e"], "b"))

    def test_shift_without_anchor_is_a_plain_click(self):
        self.assertEqual(click([], None, "c", ORDER, shift=True), (["c"], "c"))


if __name__ == "__main__":
    unittest.main()


class TestDeferToRelease(unittest.TestCase):
    def test_press_on_member_of_multi_selection_waits(self):
        self.assertTrue(defer_to_release(["a", "c"], "c"))

    def test_single_selection_or_outside_selects_at_once(self):
        self.assertFalse(defer_to_release(["c"], "c"))
        self.assertFalse(defer_to_release(["a", "c"], "b"))
        self.assertFalse(defer_to_release([], "b"))

    def test_modifiers_act_at_once(self):
        self.assertFalse(defer_to_release(["a", "c"], "c", ctrl=True))
        self.assertFalse(defer_to_release(["a", "c"], "c", shift=True))

    def test_identity_not_equality(self):
        x, y = [1], [1]
        self.assertFalse(defer_to_release([y, [2]], x))


class TestSelectAllTarget(unittest.TestCase):
    def test_queue_wins_whatever_the_view(self):
        for view in ("library", "albums", "playlists", "search"):
            self.assertEqual(select_all_target(view, False, True, False), "queue")

    def test_artists_view_selects_albums(self):
        self.assertEqual(select_all_target("library", False, False, True), "albums")

    def test_text_fields_keep_ctrl_a(self):
        self.assertIsNone(select_all_target("library", True, True, True))

    def test_other_views_left_to_gtk(self):
        self.assertIsNone(select_all_target("playlists", False, False, True))
        self.assertIsNone(select_all_target("library", False, False, False))
