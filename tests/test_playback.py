import unittest

from toccata.playback import NowPlayingMarker, now_playing


class Song:
    def __init__(self, name): self.name = name
    def __repr__(self): return f"Song({self.name})"


def queue(*names):
    return {str(i): Song(n) for i, n in enumerate(names, start=10)}


class TestNowPlaying(unittest.TestCase):
    def test_playing_and_paused_are_highlighted(self):
        self.assertEqual(now_playing({"state": "play", "songid": "11"}), ("11", "play"))
        self.assertEqual(now_playing({"state": "pause", "songid": "11"}), ("11", "pause"))

    def test_stopped_highlights_nothing_even_though_mpd_names_a_current_song(self):
        self.assertEqual(now_playing({"state": "stop", "songid": "11"}), (None, ""))

    def test_missing_or_empty_fields(self):
        for status in ({}, {"state": "play"}, {"songid": "3"}, {"state": "play", "songid": ""}, {"state": "weird", "songid": "1"}):
            self.assertEqual(now_playing(status), (None, ""), status)

    def test_id_zero_is_a_real_id(self):
        self.assertEqual(now_playing({"state": "play", "songid": "0"}), ("0", "play"))


class TestMarker(unittest.TestCase):
    def setUp(self):
        self.m = NowPlayingMarker()
        self.q = queue("a", "b", "c")          # ids 10, 11, 12
        self.a, self.b, self.c = self.q["10"], self.q["11"], self.q["12"]

    def st(self, state="play", songid="10"):
        return {"state": state, "songid": songid}

    def test_starting_playback_marks_the_song(self):
        self.assertEqual(self.m.update(self.q, self.st()), [(self.a, "play")])

    def test_nothing_playing_changes_nothing(self):
        self.assertEqual(self.m.update(self.q, {"state": "stop"}), [])

    def test_repeated_identical_updates_are_free(self):
        self.m.update(self.q, self.st())
        self.assertEqual(self.m.update(self.q, self.st()), [])

    def test_pausing_changes_only_the_state(self):
        self.m.update(self.q, self.st())
        self.assertEqual(self.m.update(self.q, self.st("pause")), [(self.a, "pause")])
        self.assertEqual(self.m.update(self.q, self.st("play")), [(self.a, "play")])

    def test_next_track_clears_the_old_and_marks_the_new(self):
        self.m.update(self.q, self.st())
        self.assertEqual(self.m.update(self.q, self.st(songid="11")), [(self.a, ""), (self.b, "play")])

    def test_stopping_clears_and_stays_clear(self):
        self.m.update(self.q, self.st())
        self.assertEqual(self.m.update(self.q, self.st("stop")), [(self.a, "")])
        self.assertEqual(self.m.update(self.q, self.st("stop")), [])

    def test_removing_the_playing_song_from_the_queue_clears_it(self):
        self.m.update(self.q, self.st())
        smaller = {k: v for k, v in self.q.items() if k != "10"}
        self.assertEqual(self.m.update(smaller, self.st()), [(self.a, "")])

    def test_song_not_in_the_queue_marks_nothing(self):
        self.assertEqual(self.m.update(self.q, self.st(songid="999")), [])

    def test_rebuilt_queue_marks_the_new_objects_and_clears_the_old(self):
        self.m.update(self.q, self.st("pause"))
        rebuilt = queue("a", "b", "c")
        new_a = rebuilt["10"]
        self.assertEqual(self.m.update(rebuilt, self.st("pause")), [(self.a, ""), (new_a, "pause")])
        self.assertEqual(self.m.update(rebuilt, self.st("pause")), [])

    def test_rebuilt_queue_where_the_song_vanished(self):
        self.m.update(self.q, self.st())
        self.assertEqual(self.m.update({}, self.st()), [(self.a, "")])

    def test_reset_forgets_everything(self):
        self.m.update(self.q, self.st())
        self.m.reset()
        self.assertEqual(self.m.update(self.q, self.st()), [(self.a, "play")])

    def test_only_one_song_is_ever_highlighted(self):
        marked = set()
        for status in [self.st(), self.st(songid="11"), self.st("pause", "12"), self.st("stop", "12"),
                       self.st(songid="10"), self.st("pause", "10")]:
            for song, state in self.m.update(self.q, status):
                (marked.add if state else marked.discard)(song)
            self.assertLessEqual(len(marked), 1, status)

    def test_same_file_queued_twice_highlights_only_the_entry_in_use(self):
        two = {"20": Song("same file"), "21": Song("same file")}
        self.assertEqual(self.m.update(two, {"state": "play", "songid": "21"}), [(two["21"], "play")])




class TestDbUpdate(unittest.TestCase):
    def test_job_while_updating(self):
        from toccata.playback import db_update_job, db_update_text
        self.assertEqual(db_update_job({"state": "play", "updating_db": "17"}), "17")
        self.assertIsNone(db_update_job({"state": "play"}))
        self.assertIsNone(db_update_job({"updating_db": ""}))
        self.assertIsNone(db_update_job(None))
        label, tip = db_update_text("17")
        self.assertEqual(label, "Updating database…")
        self.assertIn("17", tip)
        self.assertEqual(db_update_text(None), (None, None))


class TestPlayOptions(unittest.TestCase):
    def test_options_from_status(self):
        from toccata.playback import play_options
        st = {"repeat": "1", "single": "oneshot", "random": "0", "consume": "1"}
        self.assertEqual(play_options(st), {"repeat": True, "single": True, "random": False, "consume": True})
        self.assertEqual(play_options({}), {"repeat": False, "single": False, "random": False, "consume": False})
        self.assertEqual(play_options(None)["consume"], False)


class TestReuseEntries(unittest.TestCase):
    class E:
        def __init__(self, data):
            self.data = data

    def songs(self, *ids, **extra):
        return [dict({"id": str(i), "file": f"{i}.flac", "pos": str(p)}, **extra) for p, i in enumerate(ids)]

    def test_appending_keeps_the_existing_objects(self):
        from toccata.playback import reuse_entries
        old = [self.E(d) for d in self.songs(1, 2, 3, 4)]
        new = reuse_entries(old, self.songs(1, 2, 3, 4, 5, 6), self.E)
        self.assertTrue(all(a is b for a, b in zip(old, new[:4])))
        self.assertEqual([e.data["id"] for e in new[4:]], ["5", "6"])

    def test_moved_entry_kept_with_its_new_position(self):
        from toccata.playback import reuse_entries
        old = [self.E(d) for d in self.songs(1, 2, 3)]
        new = reuse_entries(old, self.songs(1, 3), self.E)  # 2 removed: 3 moves up
        self.assertIs(new[1], old[2])
        self.assertEqual(new[1].data["pos"], "1")

    def test_changed_tags_give_a_new_object(self):
        from toccata.playback import reuse_entries
        old = [self.E(d) for d in self.songs(1, 2)]
        changed = self.songs(1, 2)
        changed[1]["title"] = "Edited"
        new = reuse_entries(old, changed, self.E)
        self.assertIs(new[0], old[0])
        self.assertIsNot(new[1], old[1])
        self.assertEqual(new[1].data["title"], "Edited")

    def test_new_ids_are_new_objects_and_an_object_is_used_once(self):
        from toccata.playback import reuse_entries
        old = [self.E(d) for d in self.songs(1)]
        new = reuse_entries(old, [self.songs(1)[0], self.songs(1)[0], self.songs(7)[0]], self.E)
        self.assertIs(new[0], old[0])
        self.assertIsNot(new[1], old[0])
        self.assertIsNot(new[2], old[0])


class TestChangedRange(unittest.TestCase):
    def setUp(self):
        from toccata.playback import changed_range
        self.cr = changed_range
        self.o = [object() for _ in range(6)]

    def apply(self, old, start, n, insert):
        return old[:start] + list(insert) + old[start + n:]

    def check(self, old, new):
        start, n, insert = self.cr(old, new)
        self.assertEqual(self.apply(old, start, n, insert), new)
        return start, n, list(insert)

    def test_append_is_a_pure_insert_at_the_end(self):
        a, b = object(), object()
        self.assertEqual(self.check(self.o[:4], self.o[:4] + [a, b]), (4, 0, [a, b]))

    def test_removing_the_end_is_a_pure_removal(self):
        self.assertEqual(self.check(self.o, self.o[:4]), (4, 2, []))

    def test_removing_the_middle(self):
        self.assertEqual(self.check(self.o, self.o[:2] + self.o[4:]), (2, 2, []))

    def test_replaced_entry_in_the_middle(self):
        x = object()
        new = self.o[:3] + [x] + self.o[4:]
        self.assertEqual(self.check(self.o, new), (3, 1, [x]))

    def test_unchanged_and_everything_new(self):
        self.assertEqual(self.check(self.o, list(self.o)), (6, 0, []))
        fresh = [object() for _ in range(3)]
        self.assertEqual(self.check(self.o, fresh), (0, 6, fresh))

    def test_repeated_object_does_not_overlap(self):
        a = object()
        self.assertEqual(self.check([a], [a, a]), (1, 0, [a]))
        self.check([a, a], [a])
        self.check([], [a]); self.check([a], [])


class TestEntryToPlay(unittest.TestCase):
    Q = [("1", "a.flac"), ("2", "b.flac"), ("3", "a.flac"), ("4", "c.flac")]

    def test_first_after_the_current_one(self):
        from toccata.playback import entry_to_play
        self.assertEqual(entry_to_play(self.Q, "a.flac", current_pos=1), "3")
        self.assertEqual(entry_to_play(self.Q, "a.flac", current_pos=None), "1")

    def test_wraps_to_the_first_one(self):
        from toccata.playback import entry_to_play
        self.assertEqual(entry_to_play(self.Q, "a.flac", current_pos=3), "1")
        self.assertEqual(entry_to_play(self.Q, "a.flac", current_pos=2), "3")  # the playing entry itself: restarts it

    def test_not_in_the_queue(self):
        from toccata.playback import entry_to_play
        self.assertIsNone(entry_to_play(self.Q, "zzz.flac", 0))
        self.assertIsNone(entry_to_play([], "a.flac"))


class TestDbReloadGate(unittest.TestCase):
    def setUp(self):
        from toccata.playback import DbReloadGate
        self.g = DbReloadGate()

    def test_nothing_due_without_a_change(self):
        self.assertIsNone(self.g.take(None))

    def test_change_without_update_running_reloads_at_once(self):
        self.g.changed()
        self.assertIs(self.g.take(None), False)
        self.assertIsNone(self.g.take(None))  # once

    def test_held_back_while_the_update_runs(self):
        self.g.changed()
        self.assertIsNone(self.g.take("7"))
        self.g.changed()  # a second job of the same rescan
        self.assertIsNone(self.g.take("8"))
        self.assertIs(self.g.take(None), False)  # one reload when it is over
        self.assertIsNone(self.g.take(None))

    def test_queue_flag_survives_until_the_reload(self):
        self.g.changed(queue_too=True)
        self.g.changed()
        self.assertIsNone(self.g.take("3"))
        self.assertIs(self.g.take(None), True)
        self.g.changed()
        self.assertIs(self.g.take(None), False)

    def test_reset_drops_a_pending_reload(self):
        self.g.changed(queue_too=True)
        self.g.reset()
        self.assertIsNone(self.g.take(None))


if __name__ == "__main__":
    unittest.main()
