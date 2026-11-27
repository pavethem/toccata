import json
import os
import tempfile
import unittest

from toccata import settings as st
from toccata.settings import Collection, Settings


def home():
    return Settings(host="nas", port=6600, password="pw", music_dir="/music", partition="",
                    last_artist="Band")


class Base(unittest.TestCase):
    def names(self, s):
        return [c.name for c in st.collections_of(s)]


class TestPersistence(Base):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "settings.json")

    def test_round_trip_keeps_collections_as_objects(self):
        s = home()
        s.collection = "Home"
        s.collections = [Collection("Home", "nas", 6600, "pw", "/music", "party", "Band"),
                         Collection("Büro ♥", "10.0.0.2", 6601)]
        st.save(s, self.path)
        back = st.load(self.path)
        self.assertEqual(back, s)
        self.assertIsInstance(back.collections[0], Collection)

    def test_file_is_plain_json(self):
        s = home(); s.collections = [Collection("A", "h")]
        st.save(s, self.path)
        with open(self.path) as f:
            data = json.load(f)
        self.assertEqual(data["collections"][0]["host"], "h")
        self.assertEqual(data["partition"], "")

    def test_partition_round_trips(self):
        st.save(Settings(partition="party"), self.path)
        self.assertEqual(st.load(self.path).partition, "party")

    def test_old_file_without_collections_loads(self):
        with open(self.path, "w") as f:
            json.dump({"host": "box"}, f)
        s = st.load(self.path)
        self.assertEqual((s.host, s.collection, s.collections, s.partition), ("box", "", [], ""))


class TestCleaning(Base):
    def clean(self, **data):
        return st._clean(data)

    def test_bad_entries_are_dropped(self):
        bad = [5, None, "x", [], {}, {"name": ""}, {"name": "   "}, {"name": 7}, {"name": "x" * 61},
               {"name": "tab\there"}, {"name": "ctl\x00"}]
        s = self.clean(collections=bad + [{"name": "Good", "host": "h"}])
        self.assertEqual([c.name for c in s.collections], ["Good"])

    def test_duplicate_names_keep_the_first_ignoring_case(self):
        s = self.clean(collections=[{"name": "Home", "host": "a"}, {"name": "HOME", "host": "b"},
                                    {"name": "home ", "host": "c"}])
        self.assertEqual([(c.name, c.host) for c in s.collections], [("Home", "a")])

    def test_fields_are_validated_like_the_flat_settings(self):
        c, = self.clean(collections=[{"name": "A", "host": "  ", "port": 99999, "password": 5,
                                      "music_dir": "/m", "partition": "p", "last_artist": "x",
                                      "junk": 1}]).collections
        self.assertEqual((c.host, c.port, c.password, c.music_dir, c.partition, c.last_artist),
                         ("localhost", 6600, "", "/m", "p", "x"))

    def test_names_are_stripped(self):
        self.assertEqual(self.clean(collections=[{"name": "  Home  "}]).collections[0].name, "Home")

    def test_collections_must_be_a_list(self):
        for bad in ({"name": "A"}, "A", 5, None):
            self.assertEqual(self.clean(collections=bad).collections, [])

    def test_limit(self):
        many = [{"name": f"c{i}"} for i in range(st.MAX_COLLECTIONS + 10)]
        self.assertEqual(len(self.clean(collections=many).collections), st.MAX_COLLECTIONS)

    def test_active_name_validation(self):
        self.assertEqual(self.clean(collection=" Home ").collection, "Home")
        for bad in (5, None, "", "x" * 61, "a\tb"):
            self.assertEqual(self.clean(collection=bad).collection, "")

    def test_defaults_are_not_shared_between_instances(self):
        a, b = Settings(), Settings()
        a.collections.append(Collection("x"))
        self.assertEqual(b.collections, [])


class TestListing(Base):
    def test_a_fresh_settings_object_has_one_default_collection(self):
        c, = st.collections_of(home())
        self.assertEqual((c.name, c.host, c.last_artist), (st.DEFAULT_COLLECTION, "nas", "Band"))

    def test_the_active_collection_always_reflects_the_flat_fields(self):
        s = home(); s.collection = "Home"
        s.collections = [Collection("Home", "STALE", 1)]
        self.assertEqual(st.collections_of(s)[0].host, "nas")

    def test_the_active_one_is_added_if_missing_from_the_list(self):
        s = home(); s.collection = "Home"; s.collections = [Collection("Other")]
        self.assertEqual(self.names(s), ["Home", "Other"])

    def test_returned_collections_are_copies(self):
        s = home(); s.collection = "A"; s.collections = [Collection("B", "bh")]
        for c in st.collections_of(s):
            c.host = "changed"
        self.assertEqual((s.host, s.collections[0].host), ("nas", "bh"))

    def test_listing_does_not_change_settings(self):
        s = home()
        st.collections_of(s)
        self.assertEqual((s.collection, s.collections), ("", []))

    def test_sync_current_materialises_the_default(self):
        s = home()
        st.sync_current(s)
        self.assertEqual((s.collection, [c.name for c in s.collections]), (st.DEFAULT_COLLECTION, [st.DEFAULT_COLLECTION]))


class TestActivate(Base):
    def two(self):
        s = home()
        s.collection = "Home"
        s.collections = [Collection("Home"), Collection("Work", "work.example", 6601, "wpw", "/w", "party", "Cure")]
        return s

    def test_switching_copies_every_collection_field_into_the_flat_settings(self):
        s = self.two()
        self.assertIsNone(st.activate(s, "Work"))
        self.assertEqual((s.collection, s.host, s.port, s.password, s.music_dir, s.partition, s.last_artist),
                         ("Work", "work.example", 6601, "wpw", "/w", "party", "Cure"))

    def test_switching_away_saves_what_changed_meanwhile(self):
        s = self.two()
        s.last_artist, s.partition = "Beatles", "kitchen"  # user browsed in Home
        st.activate(s, "Work")
        st.activate(s, "Home")
        self.assertEqual((s.collection, s.host, s.last_artist, s.partition), ("Home", "nas", "Beatles", "kitchen"))

    def test_work_is_unchanged_by_a_round_trip(self):
        s = self.two()
        st.activate(s, "Work"); st.activate(s, "Home"); st.activate(s, "Work")
        self.assertEqual((s.host, s.last_artist, s.partition), ("work.example", "Cure", "party"))

    def test_other_settings_are_untouched(self):
        s = self.two(); s.main_split, s.replaygain = 700, "album"
        st.activate(s, "Work")
        self.assertEqual((s.main_split, s.replaygain), (700, "album"))

    def test_unknown_name_is_an_error_and_changes_nothing(self):
        s = self.two()
        self.assertIn("no collection", st.activate(s, "Nope"))
        self.assertEqual((s.collection, s.host), ("Home", "nas"))

    def test_activating_the_active_one_is_a_no_op(self):
        s = self.two(); s.last_artist = "Mine"
        self.assertIsNone(st.activate(s, "Home"))
        self.assertEqual(s.last_artist, "Mine")

    def test_first_activation_works_even_before_any_collection_was_saved(self):
        s = home()
        st.add_collection(s, Collection("Work", "w"))
        self.assertIsNone(st.activate(s, "Work"))
        self.assertEqual(self.names(s), [st.DEFAULT_COLLECTION, "Work"])
        st.activate(s, st.DEFAULT_COLLECTION)
        self.assertEqual((s.host, s.last_artist), ("nas", "Band"))  # the original is back

    def test_connection_follows_the_switch(self):
        s = self.two(); st.activate(s, "Work")
        self.assertEqual(s.connection(), ("work.example", 6601, "wpw"))


class TestAdd(Base):
    def test_add(self):
        s = home()
        self.assertIsNone(st.add_collection(s, Collection("  Work  ", " w ", 6601)))
        self.assertEqual(self.names(s), [st.DEFAULT_COLLECTION, "Work"])
        self.assertEqual(s.collections[1].host, "w")  # stripped

    def test_adding_does_not_touch_the_active_settings(self):
        s = home()
        st.add_collection(s, Collection("Work", "w"))
        self.assertEqual((s.host, s.last_artist), ("nas", "Band"))

    def test_validation_messages(self):
        s = home()
        for c, text in [(Collection(""), "name"), (Collection("x" * 61), "name"), (Collection("a\tb"), "name"),
                        (Collection("A", host="  "), "host"), (Collection("A", port=0), "port"),
                        (Collection("A", port=70000), "port"), (Collection("A", port=True), "port")]:
            self.assertIn(text, st.add_collection(s, c).lower(), c)
        self.assertEqual(self.names(s), [st.DEFAULT_COLLECTION])

    def test_duplicate_names_are_refused_ignoring_case(self):
        s = home()
        st.add_collection(s, Collection("Work", "w"))
        for dup in ("Work", "WORK", " work "):
            self.assertIn("already exists", st.add_collection(s, Collection(dup, "x")))
        self.assertEqual(len(s.collections), 2)

    def test_cannot_reuse_the_default_name(self):
        self.assertIn("already exists", st.add_collection(home(), Collection(st.DEFAULT_COLLECTION, "x")))

    def test_limit(self):
        s = Settings()
        for i in range(st.MAX_COLLECTIONS - 1):  # the implicit default counts as one
            self.assertIsNone(st.add_collection(s, Collection(f"c{i}", "h")))
        self.assertIn("At most", st.add_collection(s, Collection("one too many", "h")))


class TestUpdate(Base):
    def setUp(self):
        self.s = home()
        st.add_collection(self.s, Collection("Work", "w", 6601, "wpw", "/w", "party", "Cure"))

    def test_edit_a_non_active_collection(self):
        c = Collection("Work", "new-host", 6602, "x", "/x", "party", "Cure")
        self.assertIsNone(st.update_collection(self.s, "Work", c))
        w = [c for c in st.collections_of(self.s) if c.name == "Work"][0]
        self.assertEqual((w.host, w.port), ("new-host", 6602))
        self.assertEqual((self.s.host, self.s.collection), ("nas", st.DEFAULT_COLLECTION))  # active untouched

    def test_editing_the_active_collection_updates_the_flat_settings(self):
        c = Collection(st.DEFAULT_COLLECTION, "other-nas", 6700, "n", "/n", "", "Band")
        self.assertIsNone(st.update_collection(self.s, st.DEFAULT_COLLECTION, c))
        self.assertEqual((self.s.host, self.s.port, self.s.password), ("other-nas", 6700, "n"))

    def test_renaming_the_active_collection_follows_it(self):
        c = Collection("Living room", "nas", 6600)
        self.assertIsNone(st.update_collection(self.s, st.DEFAULT_COLLECTION, c))
        self.assertEqual(self.s.collection, "Living room")
        self.assertEqual(self.names(self.s), ["Living room", "Work"])

    def test_rename_to_an_existing_name_is_refused(self):
        self.assertIn("already exists", st.update_collection(self.s, "Work", Collection(st.DEFAULT_COLLECTION, "w")))

    def test_renaming_only_the_case_of_its_own_name_is_fine(self):
        self.assertIsNone(st.update_collection(self.s, "Work", Collection("WORK", "w")))
        self.assertEqual(self.names(self.s), [st.DEFAULT_COLLECTION, "WORK"])

    def test_unknown_collection(self):
        self.assertIn("no collection", st.update_collection(self.s, "Nope", Collection("X", "h")))

    def test_invalid_edit_changes_nothing(self):
        before = st.collections_of(self.s)
        self.assertIn("host", st.update_collection(self.s, "Work", Collection("Work", host="")).lower())
        self.assertEqual(st.collections_of(self.s), before)

    def test_active_name_stays_consistent_after_activation_and_rename(self):
        st.activate(self.s, "Work")
        st.update_collection(self.s, "Work", Collection("Office", "w", 6601, "wpw", "/w", "party", "Cure"))
        self.assertEqual((self.s.collection, self.s.host), ("Office", "w"))


class TestDelete(Base):
    def setUp(self):
        self.s = home()
        st.add_collection(self.s, Collection("Work", "w"))

    def test_delete_a_non_active_collection(self):
        self.assertIsNone(st.delete_collection(self.s, "Work"))
        self.assertEqual(self.names(self.s), [st.DEFAULT_COLLECTION])

    def test_the_active_collection_cannot_be_deleted(self):
        self.assertIn("in use", st.delete_collection(self.s, st.DEFAULT_COLLECTION))
        self.assertEqual(len(self.names(self.s)), 2)

    def test_after_switching_the_old_one_can_go(self):
        st.activate(self.s, "Work")
        self.assertIsNone(st.delete_collection(self.s, st.DEFAULT_COLLECTION))
        self.assertEqual(self.names(self.s), ["Work"])

    def test_the_last_collection_is_always_protected(self):
        s = home()
        self.assertIn("in use", st.delete_collection(s, st.DEFAULT_COLLECTION))

    def test_unknown(self):
        self.assertIn("no collection", st.delete_collection(self.s, "Nope"))


class TestEffective(Base):
    def test_overrides_do_not_touch_the_collections(self):
        s = home(); st.add_collection(s, Collection("Work", "w"))
        eff, _ = st.effective(s, cli_host="flag", env={})
        self.assertEqual(eff.host, "flag")
        self.assertEqual((s.host, s.collections[1].host), ("nas", "w"))


if __name__ == "__main__":
    unittest.main()
