import unittest

from toccata.albums import cover_order


class TestCoverOrder(unittest.TestCase):
    def test_only_near_tiles_on_screen_first(self):
        tiles = [("far-above", 0, -2000, 180), ("above", 0, -200, 180), ("a2", 200, 0, 180),
                 ("a1", 0, 0, 180), ("b1", 0, 190, 180), ("below", 0, 600, 180), ("far-below", 0, 3000, 180)]
        order, near = cover_order(tiles, view_height=500, margin=250)
        self.assertEqual(order, ["a1", "a2", "b1", "above", "below"])
        self.assertEqual(near, {"a1", "a2", "b1", "above", "below"})

    def test_partly_visible_counts_as_shown(self):
        order, _ = cover_order([("below", 0, 520, 100), ("cut", 0, -100, 150)], view_height=500, margin=200)
        self.assertEqual(order, ["cut", "below"])

    def test_around_nearest_edge_first(self):
        tiles = [("up-far", 0, -400, 100), ("down-near", 0, 510, 100), ("up-near", 0, -130, 100)]
        order, _ = cover_order(tiles, view_height=500, margin=500)
        self.assertEqual(order, ["down-near", "up-near", "up-far"])

    def test_edges_outside_margin(self):
        order, near = cover_order([("just-above", 0, -300, 100), ("just-below", 0, 700, 100)],
                                  view_height=500, margin=200)
        self.assertEqual((order, near), ([], set()))


if __name__ == "__main__":
    unittest.main()
