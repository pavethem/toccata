import unittest

from toccata.selection import band_hits, band_merge, band_positions, band_targets, edge_scroll


class TestBandPositions(unittest.TestCase):
    def test_from_below_the_end_up_to_a_row(self):
        self.assertEqual(band_positions(10, 7, 10), [7, 8, 9])

    def test_both_below_the_end_selects_nothing(self):
        self.assertEqual(band_positions(10, 10, 10), [])

    def test_between_two_rows_includes_both(self):
        self.assertEqual(band_positions(5, 2, 10), [2, 3, 4, 5])
        self.assertEqual(band_positions(2, 5, 10), [2, 3, 4, 5])

    def test_empty_list(self):
        self.assertEqual(band_positions(0, 0, 0), [])


class TestBandHits(unittest.TestCase):
    SPANS = [(0, 40), (40, 80), (80, 120), (200, 240)]

    def test_touched_spans_either_direction(self):
        self.assertEqual(band_hits(self.SPANS, 50, 90), [1, 2])
        self.assertEqual(band_hits(self.SPANS, 90, 50), [1, 2])

    def test_touching_an_edge_counts(self):
        self.assertEqual(band_hits(self.SPANS, 120, 160), [2])
        self.assertEqual(band_hits(self.SPANS, 160, 200), [3])

    def test_gap_hits_nothing(self):
        self.assertEqual(band_hits(self.SPANS, 130, 190), [])

    def test_below_everything_up_to_last(self):
        self.assertEqual(band_hits(self.SPANS, 400, 230), [3])


class TestBandTargets(unittest.TestCase):
    def test_albums_win(self):
        self.assertEqual(band_targets([(0, 50), (300, 350)], [(50, 90), (90, 130)], 100, 320),
                         ("albums", [1]))

    def test_tracks_when_no_album_header_touched(self):
        self.assertEqual(band_targets([(0, 50), (300, 350)], [(50, 90), (90, 130)], 100, 120),
                         ("tracks", [1]))


class TestBandMerge(unittest.TestCase):
    ORDER = ["a", "b", "c", "d", "e"]

    def test_plain_replaces(self):
        self.assertEqual(band_merge(["a"], ["d", "c"], self.ORDER, False), ["c", "d"])

    def test_ctrl_adds(self):
        self.assertEqual(band_merge(["a"], ["d", "c"], self.ORDER, True), ["a", "c", "d"])


class TestEdgeScroll(unittest.TestCase):
    def test_middle_does_not_scroll(self):
        self.assertEqual(edge_scroll(300, 600), 0)

    def test_near_edges_scroll_towards_them(self):
        self.assertLess(edge_scroll(5, 600), 0)
        self.assertGreater(edge_scroll(595, 600), 0)

    def test_faster_further_out_but_capped(self):
        self.assertLess(abs(edge_scroll(30, 600)), abs(edge_scroll(-40, 600)))
        self.assertEqual(edge_scroll(-10000, 600), -80)
        self.assertEqual(edge_scroll(10000, 600), 80)
        self.assertEqual(edge_scroll(31.5, 600), -1)  # just inside the zone: slow

    def test_small_view_keeps_a_still_middle(self):
        self.assertEqual(edge_scroll(20, 40), 0)

    def test_no_height(self):
        self.assertEqual(edge_scroll(10, 0), 0)


if __name__ == "__main__":
    unittest.main()
