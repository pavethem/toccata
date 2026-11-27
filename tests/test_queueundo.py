import random
import unittest

from toccata.queueundo import QueueHistory, restore_commands, _longest_increasing


def apply(queue, commands):
    """Run the commands like MPD would on [(id, uri)]; new entries get fresh ids."""
    queue = list(queue)
    next_id = max([i for i, _ in queue], default=0) + 1000
    for cmd in commands:
        if cmd[0] == "deleteid":
            queue = [e for e in queue if e[0] != cmd[1]]
        elif cmd[0] == "moveid":
            entry = next(e for e in queue if e[0] == cmd[1])
            queue.remove(entry)
            pos = int(cmd[2])
            assert 0 <= pos <= len(queue)
            queue.insert(pos, entry)
        elif cmd[0] == "add":
            pos = int(cmd[2]) if len(cmd) > 2 else len(queue)
            assert 0 <= pos <= len(queue), (cmd, len(queue))
            queue.insert(pos, (next_id, cmd[1]))
            next_id += 1
        else:
            raise AssertionError(cmd)
    return queue


def q(*uris):
    return [(n + 1, u) for n, u in enumerate(uris)]


class RestoreTest(unittest.TestCase):
    def check(self, current, target, keep_id=None):
        commands = restore_commands(current, target, keep_id)
        result = apply(current, commands)
        self.assertEqual([u for _, u in result], list(target))
        return commands, result

    def test_unchanged_queue_needs_no_commands(self):
        self.assertEqual(restore_commands(q("a", "b", "c"), ["a", "b", "c"]), [])

    def test_undo_remove_inserts_at_the_old_place(self):
        commands, _ = self.check(q("a", "c"), ["a", "b", "c"])
        self.assertEqual(commands, [("add", "b", "1")])

    def test_appending_has_no_position(self):
        commands, _ = self.check(q("a", "b"), ["a", "b", "c"])
        self.assertEqual(commands, [("add", "c")])

    def test_undo_add_deletes(self):
        commands, _ = self.check(q("a", "x", "b"), ["a", "b"])
        self.assertEqual(commands, [("deleteid", 2)])

    def test_undo_move_to_front_moves_only_that_entry(self):
        # c was dragged from the end to the front; undo must not move a and b
        commands, _ = self.check([(3, "c"), (1, "a"), (2, "b")], ["a", "b", "c"])
        self.assertEqual([c for c in commands if c[1] != 3], [])

    def test_undo_clear_re_adds_everything(self):
        self.check([], ["a", "b", "c"])

    def test_entries_keep_their_ids(self):
        _, result = self.check(q("c", "a", "b"), ["a", "b", "c"])
        self.assertEqual(sorted(i for i, _ in result), [1, 2, 3])

    def test_playing_duplicate_is_the_one_kept(self):
        # the same file twice; the second one plays, one copy goes away
        _, result = self.check(q("a", "x", "a"), ["x", "a"], keep_id=3)
        self.assertEqual(result, [(2, "x"), (3, "a")])
        _, result = self.check(q("a", "x", "a"), ["x", "a"])
        self.assertEqual(result, [(2, "x"), (1, "a")])

    def test_random_queues(self):
        rnd = random.Random(4)
        for _ in range(500):
            uris = [rnd.choice("abcdefgh") for _ in range(rnd.randint(0, 12))]
            current = [(n + 1, u) for n, u in enumerate(uris)]
            target = [rnd.choice("abcdefghij") for _ in range(rnd.randint(0, 12))]
            if rnd.random() < 0.5:  # often a permutation (undo of a move or shuffle)
                target = rnd.sample(uris, len(uris))
            keep = rnd.choice(current)[0] if current else None
            _, result = self.check(current, target, keep)
            kept_uri = dict(current).get(keep)
            if kept_uri in target:
                self.assertIn(keep, [i for i, _ in result])

    def test_longest_increasing(self):
        self.assertEqual(_longest_increasing([]), [])
        self.assertEqual(_longest_increasing([2, 0, 1, 3]), [1, 2, 3])
        self.assertEqual(_longest_increasing([3, 2, 1]).__len__(), 1)


class HistoryTest(unittest.TestCase):
    def test_undo_redo(self):
        h = QueueHistory()
        self.assertFalse(h.can_undo)
        h.record(["a", "b"])          # then b was removed
        self.assertEqual(h.undo(["a"]), ["a", "b"])
        self.assertTrue(h.can_redo)
        self.assertEqual(h.redo(["a", "b"]), ["a"])
        self.assertEqual(h.undo(["a"]), ["a", "b"])
        self.assertIsNone(h.undo(["a", "b"]))

    def test_new_change_drops_redo(self):
        h = QueueHistory()
        h.record(["a", "b"])
        h.undo(["a"])
        h.record(["a", "b"])
        self.assertFalse(h.can_redo)

    def test_limit(self):
        h = QueueHistory(limit=3)
        for n in range(6):
            h.record([str(n)])
        steps = []
        current = ["6"]
        while (current := h.undo(current)) is not None:
            steps.append(current)
        self.assertEqual(steps, [["5"], ["4"], ["3"]])

    def test_redo_limit(self):
        h = QueueHistory(limit=2)
        h.record(["0"]); h.record(["1"])
        h.undo(["2"]); h.undo(["1"])
        self.assertEqual(h.redo(["0"]), ["1"])
        self.assertEqual(h.redo(["1"]), ["2"])

    def test_steps_equal_to_the_queue_are_skipped(self):
        h = QueueHistory()
        h.record(["a"])
        h.record(["a", "b"])           # a change that did nothing (or failed)
        self.assertEqual(h.undo(["a", "b"]), ["a"])

    def test_repeated_record_is_one_step(self):
        h = QueueHistory()
        h.record(["a"]); h.record(["a"])
        self.assertEqual(h.undo(["b"]), ["a"])
        self.assertFalse(h.can_undo)  # (the Undo button goes insensitive)
        self.assertIsNone(h.undo(["a"]))

    def test_clear(self):
        h = QueueHistory()
        h.record(["a"])
        h.clear()
        self.assertFalse(h.can_undo)


if __name__ == "__main__":
    unittest.main()
