import itertools
import unittest

from sqlalchemy import select

from darkroom.domain import dedupe
from darkroom.storage import db
from darkroom.storage.models import Export, MediaItem
from tests.api_world import ApiCase

NUMBERS = itertools.count(1)


class ServedPeopleGuardTests(ApiCase):
    def setUp(self):
        super().setUp()
        self.jo = self.person("Jo")["id"]
        self.sam = self.person("Sam")["id"]
        self.kai = self.person("Kai")["id"]
        self.family = self.account(self.jo, "family.photos@example.test")["id"]
        self.other = self.account(self.kai, "kai.photos@example.test")["id"]
        self.pixel_id = self.pixel(self.family, people_ids=[self.sam])["id"]

    def confirm(self, person_id, account_id, tier=1, decided_by="auto"):
        number = next(NUMBERS)
        with db.session(self.engine) as opened:
            item = MediaItem(
                person_id=person_id, source="phone", path=f"p{person_id}/IMG_{number:04d}.HEIC", sha1=f"{number:040x}", bytes=100,
                phash=None, captured_utc=1_700_000_000.0, captured_local=None, offset_minutes=0, time_source="exif_offset",
                width=4032, height=3024, make="Apple", model="iPhone 15 Pro", indexed_at=1.0,
            )
            export = Export(account_id=account_id, kind="manual")
            opened.add_all([item, export])
            opened.flush()
            decision = dedupe.Decision(f"Takeout/Google Photos/IMG_{number:04d}.HEIC", account_id, person_id, tier, "skip" if tier < 4 else "review",
                                       "test", item.id, "phone", decided_by)
            row = dedupe.apply(opened, decision, export, 5.0)
            return item.id, row.id

    def reverse(self, decision_id):
        with db.session(self.engine) as opened:
            dedupe.reverse(opened, decision_id, 6.0, "owner")

    def flagged(self):
        with db.session(self.engine) as opened:
            return set(opened.execute(select(MediaItem.id).where(MediaItem.confirmed_in_google.is_(True))).scalars())

    def every_row(self):
        with db.session(self.engine) as opened:
            return {table.name: sorted(map(tuple, opened.execute(table.select()).all()), key=repr) for table in db.Base.metadata.sorted_tables}

    def served(self, account_id):
        (found,) = [a for a in self.call("GET", "/api/accounts")[1]["accounts"] if a["id"] == account_id]
        return found["served_people"]

    def assert_refused(self, method, path, body=None):
        before = self.every_row()
        status, reply = self.call(method, path, body)
        self.assertEqual(status, 409, reply)
        self.assertIn("family.photos@example.test has matched photos of", reply["error"])
        self.assertIn("until those matches are reversed", reply["error"])
        self.assertEqual(self.every_row(), before)

    def test_dropping_the_owner_of_a_confirmed_photo_from_a_pixel_is_refused_and_changes_nothing(self):
        item, _ = self.confirm(self.sam, self.family)
        self.assert_refused("PATCH", f"/api/pixels/{self.pixel_id}", {"people_ids": [self.kai], "name": "Renamed"})
        self.assertEqual(self.flagged(), {item})
        self.assertIn(self.sam, self.served(self.family))

    def test_dropping_a_person_from_a_pixel_without_confirming_decisions_is_allowed(self):
        status, reply = self.call("PATCH", f"/api/pixels/{self.pixel_id}", {"people_ids": [self.kai]})
        self.assertEqual((status, reply["pixel"]["people_ids"]), (200, [self.kai]))
        self.assertEqual(self.served(self.family), sorted([self.jo, self.kai]))

    def test_deleting_a_pixel_whose_person_has_confirmed_photos_is_refused(self):
        item, _ = self.confirm(self.sam, self.family)
        self.assert_refused("DELETE", f"/api/pixels/{self.pixel_id}")
        self.assertEqual(self.flagged(), {item})

    def test_deleting_a_pixel_without_confirming_decisions_is_allowed(self):
        self.assertEqual(self.call("DELETE", f"/api/pixels/{self.pixel_id}"), (200, {"ok": True}))
        self.assertEqual(self.served(self.family), [self.jo])

    def test_moving_a_pixel_to_another_account_is_refused_while_the_old_account_confirms_its_people(self):
        item, _ = self.confirm(self.sam, self.family)
        self.assert_refused("PATCH", f"/api/pixels/{self.pixel_id}", {"google_account_id": self.other})
        self.assertEqual(self.flagged(), {item})
        self.assertNotIn(self.sam, self.served(self.other))

    def test_moving_a_pixel_to_another_account_without_confirming_decisions_is_allowed(self):
        self.confirm(self.kai, self.other)
        self.confirm(self.jo, self.family)
        status, reply = self.call("PATCH", f"/api/pixels/{self.pixel_id}", {"google_account_id": self.other})
        self.assertEqual((status, reply["pixel"]["google_account_id"]), (200, self.other))
        self.assertEqual(self.served(self.family), [self.jo])

    def test_changing_the_default_person_away_from_a_confirmed_owner_is_refused(self):
        item, _ = self.confirm(self.jo, self.family)
        self.assert_refused("PATCH", f"/api/accounts/{self.family}", {"default_person_id": self.kai, "email": "new@example.test"})
        self.assertEqual(self.flagged(), {item})

    def test_changing_the_default_person_without_confirming_decisions_is_allowed(self):
        status, reply = self.call("PATCH", f"/api/accounts/{self.family}", {"default_person_id": self.kai})
        self.assertEqual((status, reply["account"]["served_people"]), (200, sorted([self.sam, self.kai])))

    def test_a_flag_stays_when_its_owner_is_still_served_another_way(self):
        item, _ = self.confirm(self.sam, self.family)
        self.pixel(self.family, "Pixel B", people_ids=[self.sam])
        self.assertEqual(self.call("PATCH", f"/api/pixels/{self.pixel_id}", {"people_ids": [self.kai]})[0], 200)
        self.assertEqual(self.call("PATCH", f"/api/accounts/{self.family}", {"default_person_id": self.sam})[0], 200)
        self.assertEqual(self.call("DELETE", f"/api/pixels/{self.pixel_id}")[0], 200)
        self.assertEqual(self.flagged(), {item})
        self.assertIn(self.sam, self.served(self.family))

    def test_renaming_a_pixel_that_backs_confirmed_photos_is_allowed(self):
        self.confirm(self.sam, self.family)
        status, reply = self.call("PATCH", f"/api/pixels/{self.pixel_id}", {"name": "Renamed", "people_ids": [self.sam, self.kai]})
        self.assertEqual((status, reply["pixel"]["name"]), (200, "Renamed"))

    def test_a_reversed_owner_made_or_review_decision_does_not_hold_the_person(self):
        _, decision = self.confirm(self.sam, self.family)
        self.reverse(decision)
        self.confirm(self.sam, self.family, decided_by="owner")
        self.confirm(self.sam, self.family, tier=4)
        self.assertEqual(self.flagged(), set())
        self.assertEqual(self.call("PATCH", f"/api/pixels/{self.pixel_id}", {"people_ids": [self.kai]})[0], 200)

    def test_a_confirmation_by_another_account_does_not_hold_this_one(self):
        self.pixel(self.other, "Kai's Pixel", people_ids=[self.sam])
        item, _ = self.confirm(self.sam, self.other)
        self.assertEqual(self.call("DELETE", f"/api/pixels/{self.pixel_id}")[0], 200)
        self.assertEqual(self.flagged(), {item})


if __name__ == "__main__":
    unittest.main()
