import unittest

from sqlalchemy.exc import IntegrityError

from darkroom.storage import db
from darkroom.storage.models import (
    Alert, BrowserJob, DedupeDecision, Device, Event, Export, ExportPart, GoogleAccount, ImportRun, MediaItem, PhoneAsset,
    PhoneResource, Person, ReviewItem,
)
from tests.shared import add_person, own_database


def media(person_id, path="a.jpg", **extra):
    return MediaItem(person_id=person_id, source="phone", path=path, sha1="a" * 40, bytes=3, indexed_at=1.0, **extra)


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.engine = own_database(self)

    def test_defaults(self):
        with db.session(self.engine) as opened:
            jo = add_person(opened, "jo")
            device = Device(person_id=jo, name="Phone", token_hash="h", created=1.0)
            item = media(jo)
            opened.add_all([device, item])
            opened.flush()
            self.assertFalse(device.cleanup_enabled)
            self.assertEqual(device.pending_uploads, 0)
            self.assertFalse(item.confirmed_in_google)
            self.assertEqual((item.time_source, item.gps_source), ("unknown", "none"))

    def test_media_is_unique_per_person_and_path(self):
        with db.session(self.engine) as opened:
            jo, sam = add_person(opened, "jo"), add_person(opened, "sam")
            opened.add_all([media(jo), media(sam)])
            opened.flush()
        with self.assertRaises(IntegrityError), db.session(self.engine) as opened:
            opened.add(media(1))

    def test_choices_are_enforced(self):
        with self.assertRaises(IntegrityError), db.session(self.engine) as opened:
            jo = add_person(opened, "jo")
            opened.add(MediaItem(person_id=jo, source="cloud", path="a", sha1="a", bytes=1, indexed_at=1.0))
        with self.assertRaises(IntegrityError), db.session(self.engine) as opened:
            jo = add_person(opened, "jo")
            opened.add(GoogleAccount(email="a@x", default_person_id=jo, session_state="maybe", created=1.0))

    def test_a_resource_kind_is_unique_per_asset(self):
        with db.session(self.engine) as opened:
            jo = add_person(opened, "jo")
            device = Device(person_id=jo, name="Phone", token_hash="h", created=1.0)
            opened.add(device)
            opened.flush()
            asset = PhoneAsset(device_id=device.id, photokit_id="P1", created=1.0)
            opened.add(asset)
            opened.flush()
            opened.add(PhoneResource(asset_id=asset.id, kind="photo", sha1="a", bytes=1, filename="a.jpg"))
            opened.add(PhoneResource(asset_id=asset.id, kind="live_video", sha1="b", bytes=1, filename="a.mov"))
        with self.assertRaises(IntegrityError), db.session(self.engine) as opened:
            opened.add(PhoneResource(asset_id=1, kind="photo", sha1="c", bytes=1, filename="b.jpg"))

    def test_the_rest_round_trip(self):
        with db.session(self.engine) as opened:
            jo = add_person(opened, "jo")
            account = GoogleAccount(email="jo@x.test", default_person_id=jo, created=1.0)
            opened.add(account)
            opened.flush()
            export = Export(account_id=account.id, kind="manual")
            opened.add(export)
            opened.flush()
            opened.add(ExportPart(export_id=export.id, number=1))
            phone, takeout = media(jo, "a.jpg"), media(jo, "b.jpg")
            opened.add_all([phone, takeout])
            opened.flush()
            decision = DedupeDecision(export_id=export.id, account_id=account.id, takeout_item_id=takeout.id, matched_item_id=phone.id,
                                      tier=1, reason="same sha1", decided_by="auto", decided_at=1.0)
            opened.add(decision)
            opened.flush()
            opened.add_all([ReviewItem(decision_id=decision.id), ImportRun(export_id=export.id, started=1.0),
                            BrowserJob(account_id=account.id, kind="noop", created=1.0), Alert(key="k", kind="x", subject="s", message="m", opened=1.0),
                            Event(time=1.0, type="t", subject="s")])
        with db.session(self.engine) as opened:
            self.assertEqual(opened.query(DedupeDecision).one().tier, 1)
            self.assertEqual(opened.query(ReviewItem).one().status, "pending")
            self.assertEqual(opened.query(Export).one().status, "announced")

    def test_a_decision_tier_is_one_to_five(self):
        with self.assertRaises(IntegrityError), db.session(self.engine) as opened:
            jo = add_person(opened, "jo")
            account = GoogleAccount(email="jo@x.test", default_person_id=jo, created=1.0)
            opened.add(account)
            opened.flush()
            export = Export(account_id=account.id, kind="manual")
            opened.add(export)
            opened.flush()
            opened.add(DedupeDecision(export_id=export.id, account_id=account.id, tier=6, reason="x", decided_by="auto", decided_at=1.0))

    def test_deleting_a_person_with_an_account_is_refused(self):
        with db.session(self.engine) as opened:
            jo = add_person(opened, "jo")
            opened.add(GoogleAccount(email="jo@x.test", default_person_id=jo, created=1.0))
        with self.assertRaises(IntegrityError), db.session(self.engine) as opened:
            opened.delete(opened.get(Person, 1))


if __name__ == "__main__":
    unittest.main()
