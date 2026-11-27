import itertools
import unittest

from toccata.dnd import apply_moves, drag_label, insert_index, line_y, plan_moves, track_name, tracks_label

Q = list("abcdef")


def result(moved, at, queue=Q):
    return apply_moves(queue, plan_moves(queue, moved, at))


class TestPlanMoves(unittest.TestCase):
    def test_single_moves(self):
        self.assertEqual(result(["a"], 3), list("bcade f".replace(" ", "")))
        self.assertEqual(result(["f"], 0), list("fabcde"))
        self.assertEqual(result(["c"], 6), list("abdefc"))

    def test_several_keep_their_order(self):
        self.assertEqual(result(["e", "b"], 3), list("acbedf"))  # b, e placed before d
        self.assertEqual(result(["a", "b"], 6), list("cdefab"))

    def test_drop_inside_own_selection_keeps_everything_in_place(self):
        self.assertEqual(result(["b", "c"], 2), list(Q))
        self.assertEqual(result(["b", "c"], 1), list(Q))
        self.assertEqual(result(["b", "c"], 3), list(Q))

    def test_always_a_permutation_with_moved_block_together(self):
        for k in (1, 2, 3):
            for moved in itertools.combinations(Q, k):
                for at in range(len(Q) + 1):
                    out = result(list(moved), at)
                    self.assertEqual(sorted(out), sorted(Q))
                    idx = [out.index(m) for m in moved]
                    self.assertEqual(idx, list(range(idx[0], idx[0] + k)), (moved, at))
                    # the block sits right before the first unmoved entry at/after `at`
                    anchor = next((i for i in Q[at:] if i not in moved), None)
                    after = idx[-1] + 1
                    self.assertEqual(out[after] if after < len(out) else None, anchor, (moved, at))

    def test_out_of_range_insert_is_clamped(self):
        self.assertEqual(result(["a"], 99), list("bcdefa"))
        self.assertEqual(result(["f"], -5), list("fabcde"))

    def test_nothing_to_move(self):
        self.assertEqual(plan_moves(Q, [], 2), [])
        self.assertEqual(plan_moves(Q, ["zzz"], 2), [])


class TestInsertIndex(unittest.TestCase):
    def test_halves_and_end(self):
        self.assertEqual(insert_index(2, 5, 40, 10), 2)
        self.assertEqual(insert_index(2, 30, 40, 10), 3)
        self.assertEqual(insert_index(None, 0, 0, 10), 10)


if __name__ == "__main__":
    unittest.main()


class TestLineY(unittest.TestCase):
    def test_before_row_is_top_edge(self):
        self.assertEqual(line_y(4, 4, 100, 140), 100)

    def test_after_row_is_bottom_edge(self):
        self.assertEqual(line_y(5, 4, 100, 140), 140)

    def test_after_collapsed_album_is_bottom_edge(self):
        # collapsed album at rows 4..9: its lower half drops at 10, marker below the row
        self.assertEqual(line_y(10, 4, 100, 140), 140)


class TestClientCommands(unittest.TestCase):
    def test_add_many_with_position_and_move_ids(self):
        from toccata.mpd_client import MPDClient
        c = MPDClient("x", 1)
        sent = []
        c.command_list = lambda cmds: sent.append(list(cmds))
        c.add_many(["a", "b", "c"], chunk=2, position=4)
        c.add_many(["z"])
        c.move_ids([("7", 0), ("9", 3)])
        self.assertEqual(sent, [[("add", "a", "4"), ("add", "b", "5")], [("add", "c", "6")],
                                [("add", "z")], [("moveid", "7", "0"), ("moveid", "9", "3")]])


class TestDragLabel(unittest.TestCase):
    def test_one_item_shows_its_name(self):
        self.assertEqual(drag_label(["Pink Floyd"], "artist"), "Pink Floyd")
        self.assertEqual(tracks_label([{"title": "Time", "file": "a/b.flac"}]), "Time")

    def test_several_show_the_count(self):
        self.assertEqual(tracks_label([{"title": "A"}, {"title": "B"}, {"title": "C"}]), "3 tracks")
        self.assertEqual(drag_label(["X", "Y"], "album"), "2 albums")
        self.assertEqual(drag_label(["X", "Y"], "entry", "entries"), "2 entries")

    def test_never_empty(self):
        self.assertEqual(drag_label([""], "album"), "1 album")
        self.assertEqual(track_name({}), "Track")

    def test_track_without_title_uses_the_file_name(self):
        self.assertEqual(track_name({"file": "Music/Band/01 - Song.flac"}), "01 - Song.flac")
        self.assertEqual(track_name({"title": "  ", "file": "x/y.mp3"}), "y.mp3")
        self.assertEqual(track_name({"file": "http://radio.example/stream"}), "stream")
