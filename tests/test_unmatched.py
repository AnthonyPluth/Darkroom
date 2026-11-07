import json
import unittest

from sqlalchemy import select

from darkroom.domain.ingest import run, unpack
from darkroom.storage.models import Alert, Export, ImportRun, MediaItem, Person
from tests.import_world import DAY, NOW, World, tree
from tests.takeout_fixtures import TakeoutBuilder

UNMATCHED = "Some originals are not in Google Photos"


class Household:
    def __init__(self, test, people=("jo", "sam"), pixel_people=("jo", "sam")):
        self.test = test
        self.world = World(test, people, pixel_people)
        self.builder = TakeoutBuilder(self.world.base / "parts-src", seed=11)
        self.stamp = 0

    def backed_up(self, slug, name):
        data = self.builder.image_bytes(".JPG")
        self.world.original(slug, name, data)
        return data

    def not_backed_up(self, slug, name):
        return self.world.original(slug, name, self.builder.image_bytes(".JPG"))

    def export(self, *photos, at=NOW):
        self.stamp += 1
        inbox = self.world.photos.manual_inbox(self.world.people[0]) / f"drop-{self.stamp}"
        builder = TakeoutBuilder(inbox, seed=self.stamp, stamp=f"2026010{self.stamp}T000000Z")
        for name, data in photos:
            builder.photo(name, data=data)
        if not photos:
            builder.photo("IMG_9000.JPG")
        builder.build()
        person = self.world.opened.get(Person, self.world.ids[self.world.people[0]])
        exports = unpack.register_zips(self.world.opened, person, inbox, at)
        self.world.opened.commit()
        return exports[0]

    def unmatched(self):
        return [(alert.subject, alert.key, alert.message) for alert in self.world.open_alerts("originals_unmatched")]


class SharedPixelTests(unittest.TestCase):
    def setUp(self):
        self.home = Household(self)
        self.world = self.home.world
        jo_one = self.home.backed_up("jo", "IMG_0001.JPG")
        jo_two = self.home.backed_up("jo", "IMG_0002.JPG")
        self.home.not_backed_up("sam", "IMG_0101.JPG")
        self.home.not_backed_up("sam", "IMG_0102.JPG")
        self.home.not_backed_up("sam", "IMG_0103.JPG")
        self.world.index_all()
        self.export = self.home.export(("IMG_0001.JPG", jo_one), ("IMG_0002.JPG", jo_two))

    def test_the_person_whose_folder_is_not_backed_up_gets_the_alert_and_the_other_does_not(self):
        done = self.world.run_import(self.export)
        self.assertEqual(json.loads(done.unmatched_by_person), {"jo": 0, "sam": 3})
        alerts = self.home.unmatched()
        self.assertEqual(len(alerts), 1)
        subject, key, message = alerts[0]
        self.assertEqual((subject, key), ("sam", f"originals_unmatched:{self.world.account}:{self.world.ids['sam']}:{self.world.pixel}"))
        self.assertEqual(message, "3 of Sam's originals have no match in Google yet. Check that their folder is turned on for backup on Hall Pixel.")
        self.assertEqual(self.world.sent(UNMATCHED), 1)

    def test_their_originals_stay_unconfirmed(self):
        self.world.run_import(self.export)
        flags = dict(self.world.opened.execute(select(MediaItem.path, MediaItem.confirmed_in_google).where(MediaItem.source == "phone")).all())
        self.assertEqual(flags, {"jo/originals/IMG_0001.JPG": True, "jo/originals/IMG_0002.JPG": True, "sam/originals/IMG_0101.JPG": False,
                                 "sam/originals/IMG_0102.JPG": False, "sam/originals/IMG_0103.JPG": False})

    def test_the_alert_deletes_and_flags_nothing(self):
        before = {key: value for key, value in tree(self.world.root).items() if "/originals/" in f"/{key}"}
        self.world.run_import(self.export)
        flags_before = dict(self.world.opened.execute(select(MediaItem.id, MediaItem.confirmed_in_google)).all())
        run.check_unmatched(self.world.opened, self.world.settings, self.world.opened.get(Export, self.export.id), NOW, self.world.recorder)
        self.assertEqual(dict(self.world.opened.execute(select(MediaItem.id, MediaItem.confirmed_in_google)).all()), flags_before)
        after = {key: value for key, value in tree(self.world.root).items() if "/originals/" in f"/{key}"}
        self.assertEqual({key: after[key] for key in before}, before)
        self.assertTrue(set(after) - set(before) <= {key for key in after if key.endswith(".xmp")})
        for _, _, message in self.home.unmatched():
            self.assertNotIn("delet", message.lower())

    def test_a_later_import_that_matches_everything_closes_the_alert(self):
        self.world.run_import(self.export)
        self.assertEqual(len(self.home.unmatched()), 1)
        sam = {name: (self.world.root / "sam/originals" / name).read_bytes() for name in ("IMG_0101.JPG", "IMG_0102.JPG", "IMG_0103.JPG")}
        later = self.home.export(*sam.items(), at=NOW + 60 * DAY)
        done = self.world.run_import(later, NOW + 60 * DAY)
        self.assertEqual(json.loads(done.unmatched_by_person), {"jo": 0, "sam": 0})
        self.assertEqual(self.home.unmatched(), [])
        closed = self.world.opened.execute(select(Alert).where(Alert.kind == "originals_unmatched")).scalar_one()
        self.assertEqual(closed.closed, NOW + 60 * DAY)

    def test_a_count_that_changes_updates_the_open_alert_without_sending_again(self):
        self.world.run_import(self.export)
        (self.world.root / "sam/originals/IMG_0104.JPG").write_bytes(self.home.builder.image_bytes(".JPG"))
        self.world.index_all()
        self.world.run_import(self.home.export(at=NOW + DAY), NOW + DAY)
        self.assertEqual(self.home.unmatched()[0][2].split(" ")[0], "4")
        self.assertEqual(self.world.sent(UNMATCHED), 1)

    def test_originals_indexed_within_the_grace_period_are_not_counted(self):
        self.home.not_backed_up("sam", "IMG_0199.JPG")
        self.world.index_all(at=NOW - 6 * DAY)
        done = self.world.run_import(self.export)
        self.assertEqual(json.loads(done.unmatched_by_person)["sam"], 3)

    def test_a_pixel_that_no_longer_serves_the_person_drops_its_alert(self):
        from darkroom.storage.models import PixelPerson

        self.world.run_import(self.export)
        self.world.opened.execute(PixelPerson.__table__.delete().where(PixelPerson.person_id == self.world.ids["sam"]))
        self.world.opened.commit()
        self.world.run_import(self.home.export(at=NOW + DAY), NOW + DAY)
        self.assertEqual(self.home.unmatched(), [])

    def test_status_counts_come_from_the_latest_finished_import(self):
        self.assertEqual(run.unmatched_by_person(self.world.opened), {})
        self.world.run_import(self.export)
        self.assertEqual(run.unmatched_by_person(self.world.opened), {"jo": 0, "sam": 3})
        row = self.world.opened.execute(select(ImportRun)).scalar_one()
        row.unmatched_by_person = json.dumps({"sam": "3", "7": 1, "jo": 2})
        self.assertEqual(run.unmatched_by_person(self.world.opened), {"jo": 2, "7": 1})


class SinglePersonTests(unittest.TestCase):
    def test_nothing_opens_when_everything_matches(self):
        home = Household(self, people=("jo",), pixel_people=("jo",))
        one, two = home.backed_up("jo", "IMG_0001.JPG"), home.backed_up("jo", "2019/IMG_0002.JPG")
        home.world.index_all()
        done = home.world.run_import(home.export(("IMG_0001.JPG", one), ("IMG_0002.JPG", two)))
        self.assertEqual(json.loads(done.unmatched_by_person), {"jo": 0})
        self.assertEqual(home.unmatched(), [])
        self.assertEqual(home.world.sent(UNMATCHED), 0)

    def test_an_account_without_pixel_rows_names_the_person_only(self):
        home = Household(self, people=("jo",), pixel_people=())
        home.not_backed_up("jo", "IMG_0001.JPG")
        home.world.index_all()
        home.world.run_import(home.export())
        [(subject, key, message)] = home.unmatched()
        self.assertEqual((subject, key), ("jo", f"originals_unmatched:{home.world.account}:{home.world.ids['jo']}:account"))
        self.assertEqual(message, "1 of Jo's originals have no match in Google yet. Check that their folder is turned on for backup in Google Photos on the Pixel that serves them.")


if __name__ == "__main__":
    unittest.main()
