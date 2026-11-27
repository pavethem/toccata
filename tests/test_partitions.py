import unittest

from toccata.mpd_client import CommandError, MPDClient, Output, ProtocolError, parse_outputs
from tests import partition_mock
from tests.partition_mock import PartHandler


class ServerCase(unittest.TestCase):
    def serve(self, partition=None, **settings):
        srv, port = partition_mock.start(**settings)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.port = port
        return self.client(partition)

    def client(self, partition=None):
        c = MPDClient("127.0.0.1", self.port, partition=partition)
        c.connect()
        self.addCleanup(c.close)
        return c

    def names(self, outputs):
        return [(o.name, o.partition) for o in outputs]


class TestParseOutputs(unittest.TestCase):
    PAIRS = [("outputid", "0"), ("outputname", "Speakers"), ("plugin", "alsa"), ("outputenabled", "1"),
             ("attribute", "dop=0"), ("attribute", "x=a=b"),
             ("outputid", "1"), ("outputname", "Head"), ("plugin", "pulse"), ("outputenabled", "0")]

    def test_fields(self):
        a, b = parse_outputs(self.PAIRS, "default")
        self.assertEqual((a.id, a.name, a.plugin, a.enabled, a.partition), (0, "Speakers", "alsa", True, "default"))
        self.assertEqual((b.id, b.enabled, b.attributes), (1, False, {}))

    def test_attributes_split_on_the_first_equals_only(self):
        self.assertEqual(parse_outputs(self.PAIRS)[0].attributes, {"dop": "0", "x": "a=b"})

    def test_keys_are_case_insensitive_and_unknown_ones_ignored(self):
        o, = parse_outputs([("OutputID", "7"), ("OutputName", "N"), ("OutputEnabled", "1"), ("future", "x")])
        self.assertEqual((o.id, o.name, o.enabled), (7, "N", True))

    def test_lines_before_the_first_output_are_ignored(self):
        self.assertEqual(parse_outputs([("outputname", "stray"), ("outputid", "0")]), [Output(0)])

    def test_empty(self):
        self.assertEqual(parse_outputs([]), [])

    def test_bad_id_is_a_protocol_error(self):
        with self.assertRaises(ProtocolError):
            parse_outputs([("outputid", "x")])


class TestOutputs(ServerCase):
    def test_lists_the_current_partitions_outputs(self):
        c = self.serve()
        self.assertEqual(self.names(c.outputs("default")), [("Speakers", "default"), ("Headphones", "default")])

    def test_enable_and_disable(self):
        c = self.serve()
        c.set_output_enabled(1, True)
        self.assertTrue(c.outputs()[1].enabled)
        c.set_output_enabled(0, False)
        self.assertFalse(c.outputs()[0].enabled)

    def test_unknown_id_is_an_error_and_the_connection_survives(self):
        c = self.serve()
        with self.assertRaises(CommandError):
            c.set_output_enabled(9, True)
        self.assertEqual(c.status()["state"], "stop")

    def test_attributes_are_kept(self):
        self.assertEqual(self.serve().outputs()[0].attributes, {"dop": "0"})


class TestByName(ServerCase):
    def test_enable_by_name(self):
        c = self.serve()
        c.set_output_enabled_by_name("Headphones", True)
        self.assertEqual({o.name: o.enabled for o in c.outputs()}, {"Speakers": True, "Headphones": True})

    def test_disable_by_name(self):
        c = self.serve()
        c.set_output_enabled_by_name("Speakers", False)
        self.assertFalse(c.outputs()[0].enabled)

    def test_hits_the_right_output_even_after_ids_were_renumbered(self):
        c = self.serve()
        shown = {o.name: o.id for o in c.outputs()}           # the UI listed: Speakers=0, Headphones=1
        other = self.client()
        other.move_output("Kitchen")                           # ...then another client moved things
        other.switch_partition("party")
        other.move_output("Speakers")                          # Speakers leaves 'default': Headphones is now #0
        # clicking by the remembered id would now switch the wrong output (or none):
        self.assertEqual([o.name for o in c.outputs()], ["Headphones", "Kitchen"])
        c.set_output_enabled_by_name("Headphones", True)
        self.assertEqual({o.name: o.enabled for o in c.outputs()}, {"Headphones": True, "Kitchen": True})
        c.set_output_enabled_by_name("Kitchen", False)
        self.assertEqual({o.name: o.enabled for o in c.outputs()}, {"Headphones": True, "Kitchen": False})
        self.assertEqual(shown["Headphones"], 1)               # the stale id really was wrong

    def test_unknown_name_is_a_command_error_and_the_connection_survives(self):
        c = self.serve()
        with self.assertRaises(CommandError) as cm:
            c.set_output_enabled_by_name("Nope", True)
        self.assertIn("Nope", str(cm.exception))
        self.assertEqual(c.status()["state"], "stop")

    def test_an_output_of_another_partition_is_not_found_here(self):
        with self.assertRaises(CommandError):
            self.serve().set_output_enabled_by_name("Kitchen", True)


class TestIdleWatcherPartition(ServerCase):
    def watch(self, **kw):
        import threading
        from toccata.connection import IdleWatcher
        got, evt = [], threading.Event()
        w = IdleWatcher(("127.0.0.1", self.port, None), lambda ch: (got.append(ch), evt.set()),
                        retry_seconds=0.05, **kw)
        self.addCleanup(w.stop, 3)
        w.start()
        self.assertTrue(evt.wait(3))
        return got

    def test_idle_connection_enters_the_partition_before_idling(self):
        self.serve()
        PartHandler.log.clear()
        self.watch(partition="party")
        cmds = [a[0] + (" " + a[1] if a[0] == "partition" else "") for a in PartHandler.log]
        self.assertLess(cmds.index("partition party"), cmds.index("idle"))

    def test_no_partition_command_without_a_partition(self):
        self.serve()
        PartHandler.log.clear()
        self.watch()
        self.assertNotIn("partition", [a[0] for a in PartHandler.log])

    def test_output_and_partition_events_are_delivered(self):
        self.serve()
        self.assertEqual(self.watch(partition="party")[0], ["output"])


class TestPartitions(ServerCase):
    def test_list(self):
        self.assertEqual(self.serve().listpartitions(), ["default", "party"])

    def test_list_is_none_on_servers_without_partitions(self):
        self.assertIsNone(self.serve(supports_partitions=False).listpartitions())

    def test_current_partition_defaults_to_default(self):
        self.assertEqual(self.serve().current_partition(), "default")
        self.assertEqual(self.serve(supports_partitions=False).current_partition(), "default")

    def test_switch_changes_what_the_connection_sees_and_remembers_it(self):
        c = self.serve()
        c.switch_partition("party")
        self.assertEqual(c.current_partition(), "party")
        self.assertEqual([o.name for o in c.outputs()], ["Kitchen"])
        self.assertEqual(c.partition, "party")

    def test_switch_to_unknown_partition_fails_and_changes_nothing(self):
        c = self.serve()
        with self.assertRaises(CommandError):
            c.switch_partition("nope")
        self.assertEqual((c.current_partition(), c.partition), ("default", None))

    def test_partition_is_per_connection(self):
        a = self.serve()
        b = self.client()
        a.switch_partition("party")
        self.assertEqual(b.current_partition(), "default")

    def test_new_partition(self):
        c = self.serve()
        c.new_partition("study")
        self.assertEqual(c.listpartitions(), ["default", "party", "study"])
        with self.assertRaises(CommandError):
            c.new_partition("study")  # already exists

    def test_delete_merges_its_outputs_into_default(self):
        c = self.serve()
        c.delete_partition("party")
        self.assertEqual(c.listpartitions(), ["default"])
        self.assertEqual([o.name for o in c.outputs()], ["Speakers", "Headphones", "Kitchen"])

    def test_default_cannot_be_deleted(self):
        c = self.serve()
        with self.assertRaises(CommandError):
            c.delete_partition("default")

    def test_move_output_into_this_partition_renumbers(self):
        c = self.serve()
        c.move_output("Kitchen")
        self.assertEqual([(o.id, o.name) for o in c.outputs()], [(0, "Speakers"), (1, "Headphones"), (2, "Kitchen")])
        c.switch_partition("party")
        self.assertEqual(c.outputs(), [])

    def test_move_unknown_output_fails(self):
        with self.assertRaises(CommandError):
            self.serve().move_output("Nope")


class TestConnectingIntoAPartition(ServerCase):
    def test_connect_enters_the_partition(self):
        c = self.serve(partition="party")
        self.assertEqual((c.current_partition(), c.partition_failed), ("party", False))

    def test_unknown_partition_is_tolerated(self):
        c = self.serve(partition="gone")
        self.assertTrue(c.partition_failed)
        self.assertEqual(c.current_partition(), "default")  # stayed usable in default
        self.assertEqual(c.status()["state"], "stop")

    def test_old_server_without_partitions_is_tolerated(self):
        c = self.serve(partition="party", supports_partitions=False)
        self.assertTrue(c.partition_failed)
        self.assertEqual(c.status()["state"], "stop")

    def test_no_partition_command_is_sent_when_none_is_set(self):
        self.serve()
        self.assertNotIn("partition", [a[0] for a in PartHandler.log])

    def test_reconnect_returns_to_the_partition(self):
        c = self.serve()
        c.switch_partition("party")
        c.close()
        c.connect()
        self.assertEqual(c.current_partition(), "party")

    def test_failed_flag_resets_on_a_successful_reconnect(self):
        c = self.serve(partition="gone")
        self.assertTrue(c.partition_failed)
        PartHandler.state["partitions"]["gone"] = []
        c.close(); c.connect()
        self.assertFalse(c.partition_failed)
        self.assertEqual(c.current_partition(), "gone")


class TestCommandListOk(ServerCase):
    def test_one_answer_per_command(self):
        c = self.serve()
        res = c.command_list_ok([("status",), ("listpartitions",), ("outputs",)])
        self.assertEqual(len(res), 3)
        self.assertEqual(res[1], [("partition", "default"), ("partition", "party")])

    def test_empty_command_sends_nothing(self):
        self.assertEqual(self.serve().command_list_ok([]), [])

    def test_failure_raises_and_the_connection_stays_in_sync(self):
        c = self.serve()
        with self.assertRaises(CommandError):
            c.command_list_ok([("status",), ("partition", "nope"), ("outputs",)])
        self.assertEqual(c.status()["state"], "stop")

    def test_requires_a_connection(self):
        with self.assertRaises(ProtocolError):
            MPDClient("127.0.0.1", 1).command_list_ok([("status",)])


class TestAllOutputs(ServerCase):
    def test_every_partitions_outputs_tagged_with_their_partition(self):
        c = self.serve()
        self.assertEqual(self.names(c.all_outputs()),
                         [("Speakers", "default"), ("Headphones", "default"), ("Kitchen", "party")])

    def test_works_from_inside_another_partition(self):
        c = self.serve(partition="party")
        self.assertEqual(self.names(c.all_outputs()),
                         [("Kitchen", "party"), ("Speakers", "default"), ("Headphones", "default")])

    def test_connection_is_back_in_its_partition_afterwards(self):
        for start in ("default", "party"):
            c = self.serve(partition=None if start == "default" else start)
            c.all_outputs()
            self.assertEqual(c.current_partition(), start)

    def test_is_a_single_atomic_round_trip(self):
        c = self.serve()
        PartHandler.log.clear()
        c.all_outputs()
        lists = [a for a in PartHandler.log if a[0] == "command_list_ok_begin"]
        self.assertEqual(len(lists), 1)

    def test_ids_belong_to_each_outputs_own_partition(self):
        found = {o.name: o.id for o in self.serve().all_outputs()}
        self.assertEqual(found, {"Speakers": 0, "Headphones": 1, "Kitchen": 0})  # Kitchen is #0 in 'party'

    def test_without_partitions_it_just_lists_the_outputs(self):
        c = self.serve(supports_partitions=False)
        self.assertEqual([o.name for o in c.all_outputs()], ["Speakers", "Headphones"])

    def test_single_partition_server(self):
        c = self.serve()
        c.delete_partition("party")
        self.assertEqual([o.name for o in c.all_outputs()], ["Speakers", "Headphones", "Kitchen"])

    def test_failure_midway_switches_back_before_raising(self):
        c = self.serve()
        PartHandler.deny = {"party"}           # the hop into 'party' is refused
        with self.assertRaises(CommandError):
            c.all_outputs()
        PartHandler.deny = set()
        self.assertTrue(c.connected)
        self.assertEqual(c.current_partition(), "default")   # not stranded anywhere else

    def test_stranded_in_a_middle_partition_is_switched_back(self):
        # default -> party works, party -> study fails: the connection is now IN 'party'
        c = self.serve()
        c.new_partition("study")
        PartHandler.deny = {"study"}
        with self.assertRaises(CommandError):
            c.all_outputs()
        PartHandler.deny = set()
        self.assertTrue(c.connected)
        self.assertEqual(c.current_partition(), "default")

    def test_stranded_and_unable_to_return_closes_the_connection(self):
        # starts in 'party'; hops to 'default' fine, 'study' fails, and 'party' is now refused
        c = self.serve(partition="party")
        c.new_partition("study")
        PartHandler.deny = {"study", "party"}
        with self.assertRaises(CommandError):
            c.all_outputs()
        self.assertFalse(c.connected)

    def test_a_partition_without_outputs_contributes_nothing(self):
        c = self.serve()
        c.new_partition("study")
        self.assertEqual({p for _, p in self.names(c.all_outputs())}, {"default", "party"})  # 'study' has no outputs

    def test_if_even_switching_back_fails_the_connection_is_closed(self):
        c = self.serve(partition="party")
        PartHandler.deny = {"party", "default"}  # nothing can be entered any more
        with self.assertRaises(CommandError):
            c.all_outputs()
        self.assertFalse(c.connected)            # forces a clean reconnect


if __name__ == "__main__":
    unittest.main()
