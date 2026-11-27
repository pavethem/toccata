import unittest

from toccata.mpd_client import Output
from toccata.serverview import collection_subtitle, partition_rows, split_outputs, window_subtitle
from toccata.settings import Collection


class TestPartitionRows(unittest.TestCase):
    def rows(self, current):
        return {r.name: r for r in partition_rows(["default", "party", "study"], current)}

    def test_current_partition_is_marked_and_cannot_be_switched_to_or_deleted(self):
        r = self.rows("party")["party"]
        self.assertEqual((r.current, r.can_switch, r.can_delete), (True, False, False))

    def test_default_can_be_switched_to_but_never_deleted(self):
        r = self.rows("party")["default"]
        self.assertEqual((r.current, r.can_switch, r.can_delete), (False, True, False))

    def test_other_partitions_can_be_switched_to_and_deleted(self):
        r = self.rows("default")["study"]
        self.assertEqual((r.current, r.can_switch, r.can_delete), (False, True, True))

    def test_default_is_current_by_default(self):
        r = self.rows("default")["default"]
        self.assertEqual((r.current, r.can_switch, r.can_delete), (True, False, False))

    def test_order_is_kept_and_empty_is_empty(self):
        self.assertEqual([r.name for r in partition_rows(["b", "a"], "a")], ["b", "a"])
        self.assertEqual(partition_rows([], "default"), [])


class TestSplitOutputs(unittest.TestCase):
    O = [Output(0, "Speakers", partition="default"), Output(1, "Head", partition="default"),
         Output(0, "Kitchen", partition="party")]

    def test_split(self):
        here, elsewhere = split_outputs(self.O, "default")
        self.assertEqual(([o.name for o in here], [o.name for o in elsewhere]), (["Speakers", "Head"], ["Kitchen"]))

    def test_from_inside_another_partition(self):
        here, elsewhere = split_outputs(self.O, "party")
        self.assertEqual(([o.name for o in here], [o.name for o in elsewhere]), (["Kitchen"], ["Speakers", "Head"]))

    def test_outputs_of_unknown_partition_count_as_here(self):
        here, elsewhere = split_outputs([Output(0, "X")], "default")
        self.assertEqual((len(here), len(elsewhere)), (1, 0))

    def test_empty(self):
        self.assertEqual(split_outputs([], "default"), ([], []))


class TestSubtitles(unittest.TestCase):
    def test_collection_subtitle(self):
        self.assertEqual(collection_subtitle(Collection("A", "nas", 6600)), "nas:6600")
        self.assertEqual(collection_subtitle(Collection("A", "nas", 6601, partition="party")), "nas:6601 \u00b7 party")
        self.assertEqual(collection_subtitle(Collection("A", "nas", 6600, partition="default")), "nas:6600")

    def test_window_subtitle(self):
        self.assertEqual(window_subtitle("Home", ""), "Home")
        self.assertEqual(window_subtitle("Home", "default"), "Home")
        self.assertEqual(window_subtitle("Home", "party"), "Home \u00b7 party")

    def test_window_subtitle_without_a_collection_name(self):
        self.assertEqual(window_subtitle("", "party"), "party")
        self.assertEqual(window_subtitle("", "default"), "")
        self.assertEqual(window_subtitle("", None), "")


if __name__ == "__main__":
    unittest.main()


class TestServerInfo(unittest.TestCase):
    def test_format_span(self):
        from toccata.serverview import format_span
        self.assertEqual(format_span("638082"), "7 days 9:14:42")
        self.assertEqual(format_span(86410), "1 day 0:00:10")
        self.assertEqual(format_span("3725.9"), "1:02:05")
        self.assertEqual(format_span(None), "")

    def test_rows(self):
        from toccata.serverview import server_info_rows
        info = {"version": "0.24.0", "urlhandlers": ["http://", "nfs://"], "tagtypes": ["Artist", "Album"],
                "stats": {"uptime": "60", "playtime": "0", "artists": "22352", "albums": "16046",
                          "songs": "186107", "db_playtime": "90000", "db_update": "0"}}
        (s_title, server), (d_title, db) = server_info_rows(info, "nas:6600")
        self.assertEqual((s_title, d_title), ("Server", "Database"))
        server, db = dict(server), dict(db)
        self.assertEqual(server["Address"], "nas:6600")
        self.assertEqual(server["Protocol version"], "0.24.0")
        self.assertEqual(server["Uptime"], "0:01:00")
        self.assertEqual(server["Handlers"], "http, nfs")
        self.assertEqual(server["Tags"], "Artist, Album")
        self.assertEqual(db["Songs"], "186,107")
        self.assertEqual(db["Duration"], "1 day 1:00:00")
        self.assertIn("Last updated", db)

    def test_missing_values_are_left_out(self):
        from toccata.serverview import server_info_rows
        (_, server), (_, db) = server_info_rows({"stats": {"songs": "x"}})
        self.assertEqual(server, [])
        self.assertEqual(db, [])
