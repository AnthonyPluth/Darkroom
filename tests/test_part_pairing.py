import unittest

from darkroom.domain import dedupe
from darkroom.domain.ingest import part_pairing
from darkroom.storage import db
from darkroom.storage.models import DedupeDecision, Export, TakeoutEntry, TakeoutSidecar
from tests.takeout_fixtures import GOOGLE_PHOTOS, TakeoutBuilder
from tests.test_part_reader import World

YEAR = f"{GOOGLE_PHOTOS}/Photos from 2019"
PHONE_SECOND = 1_500_000_000.0


def entry_name(name, folder="Photos from 2019"):
    return f"{GOOGLE_PHOTOS}/{folder}/{name}"


def shot(shot_id, sha1="0" * 40, **fields):
    values = dict(person_id=1, source="phone", phash=None, captured_utc=PHONE_SECOND, captured_local=None, width=32, height=24, duration_ms=None,
                  make="Apple", model="iPhone 12", is_video=False, is_raw=False)
    return dedupe.Shot(id=shot_id, sha1=sha1, **(values | fields))


def index_of(*shots):
    return dedupe.PersonIndex(account_id=1, default_person_id=1, person_ids=frozenset({1}), phone=tuple(shots))


class PairingWorld:
    def __init__(self, case, parts=1, seed=0):
        self.case = case
        self.world = World(case)
        self.builder = TakeoutBuilder(self.world.inbox, seed=seed, parts=parts)
        self.parts = parts
        self.read_parts = set()

    def build(self):
        self.builder.build()
        self.world.register()
        for number in range(1, self.parts + 1):
            self.world.read(number)
        return self

    def sha1_of(self, name):
        with db.session(self.world.engine) as opened:
            return opened.query(TakeoutEntry).filter_by(name=name).one().sha1

    def fingerprint(self, name, **fields):
        with db.session(self.world.engine, writing=True) as opened:
            row = opened.query(TakeoutEntry).filter_by(name=name).one()
            for key, value in fields.items():
                setattr(row, key, value)

    def pair(self, upto, index=None, only_part=None):
        with db.session(self.world.engine, writing=True) as opened:
            export = opened.query(Export).one()
            entries = opened.query(TakeoutEntry).filter(TakeoutEntry.export_id == export.id, TakeoutEntry.kind == "media",
                                                        TakeoutEntry.part_number <= upto).order_by(TakeoutEntry.id).all()
            sidecars = opened.query(TakeoutSidecar).filter(TakeoutSidecar.export_id == export.id, TakeoutSidecar.part_number <= upto).all()
            names = {row.id: row.name for row in entries}
            result = part_pairing.pair_part(entries, sidecars, only_part or upto, index or index_of())
            return {
                "ready": sorted(row.name for row in result.ready),
                "newly_paired": sorted(row.name for row in result.newly_paired),
                "copies": sorted(row.name for row in result.copies),
                "names": names,
            }

    def row(self, name):
        with db.session(self.world.engine) as opened:
            found = opened.query(TakeoutEntry).filter_by(name=name).one()
            opened.expunge(found)
            return found

    def sidecar_rows(self):
        with db.session(self.world.engine) as opened:
            return sorted((s.part_number, s.folder, s.name, s.entry_id) for s in opened.query(TakeoutSidecar))

    def sidecar_entry_of(self, name):
        with db.session(self.world.engine) as opened:
            found = opened.query(TakeoutEntry).filter_by(name=name).one()
            if found.sidecar_entry_id is None:
                return None
            return opened.get(TakeoutEntry, found.sidecar_entry_id).name

    def decisions(self):
        with db.session(self.world.engine) as opened:
            return opened.query(DedupeDecision).count()


class SidecarPairingTests(unittest.TestCase):
    def test_media_pairs_with_a_sidecar_an_earlier_part_brought(self):
        world = PairingWorld(self, parts=2)
        photo = world.builder.photo("IMG_0001.JPG", part=1, sidecar_part=0)
        world.build()
        before = world.sidecar_rows()
        self.assertEqual(world.pair(1)["ready"], [])
        found = world.pair(2)
        media = entry_name("IMG_0001.JPG")
        self.assertEqual((found["ready"], found["newly_paired"]), ([media], []))
        self.assertEqual(world.sidecar_entry_of(media), entry_name(photo.sidecar_name))
        self.assertEqual(world.row(media).outcome, "listed")
        self.assertEqual(world.sidecar_rows(), before)

    def test_a_sidecar_that_arrives_in_a_later_part_pairs_with_held_media(self):
        world = PairingWorld(self, parts=2)
        photo = world.builder.photo("IMG_0002.JPG", part=0, sidecar_part=1)
        world.build()
        media = entry_name("IMG_0002.JPG")
        first = world.pair(1)
        self.assertEqual((first["ready"], first["newly_paired"], world.row(media).outcome, world.row(media).sidecar_entry_id), ([], [], "waiting_sidecar", None))
        second = world.pair(2)
        self.assertEqual((second["ready"], second["newly_paired"]), ([media], [media]))
        self.assertEqual((world.row(media).outcome, world.sidecar_entry_of(media)), ("listed", entry_name(photo.sidecar_name)))
        self.assertEqual(len(world.sidecar_rows()), 1)

    def test_a_late_sidecar_pairs_with_a_tier_1_entry_that_was_decided_without_one(self):
        world = PairingWorld(self, parts=2)
        photo = world.builder.photo("IMG_0003.JPG", part=0, sidecar_part=1)
        world.build()
        media = entry_name("IMG_0003.JPG")
        index = index_of(shot(7, sha1=world.sha1_of(media)))
        first = world.pair(1, index)
        self.assertEqual((first["ready"], world.row(media).metadata_pending, world.row(media).outcome), ([media], True, "listed"))
        second = world.pair(2, index)
        self.assertEqual((second["ready"], second["newly_paired"]), ([], [media]))
        self.assertEqual(world.sidecar_entry_of(media), entry_name(photo.sidecar_name))
        self.assertEqual(world.pair(2, index)["newly_paired"], [])

    def test_sidecar_rules_run_over_the_sidecars_of_every_earlier_part(self):
        world = PairingWorld(self, parts=3)
        counted = world.builder.numbered_duplicate("IMG_0004", ".JPG", part=2)
        world.builder.items[-1].sidecar_part = 0
        world.builder.items[-2].sidecar_part = 1
        world.build()
        second = entry_name("IMG_0004(1).JPG")
        found = world.pair(3)
        self.assertIn(second, found["ready"])
        self.assertEqual(world.sidecar_entry_of(second), entry_name(counted[1].sidecar_name))
        self.assertEqual(world.sidecar_entry_of(entry_name("IMG_0004.JPG")), entry_name(counted[0].sidecar_name))

    def test_the_sidecar_stays_in_the_ledger_for_an_edited_copy(self):
        world = PairingWorld(self, parts=2)
        world.builder.edited("IMG_0005", ".JPG", part=1)
        world.builder.items[0].sidecar_part = 0
        world.build()
        original, edit = entry_name("IMG_0005.JPG"), entry_name("IMG_0005-edited.JPG")
        world.fingerprint(edit, is_edited=True, edited_of=original)
        world.pair(2)
        self.assertEqual(world.sidecar_entry_of(edit), world.sidecar_entry_of(original))
        self.assertEqual(len(world.sidecar_rows()), 1)


class CopyTests(unittest.TestCase):
    def test_a_copy_in_another_folder_attaches_to_the_first_item(self):
        world = PairingWorld(self)
        world.builder.photo("IMG_0010.JPG", album="Trip")
        world.build()
        year, album = entry_name("IMG_0010.JPG"), entry_name("IMG_0010.JPG", "Trip")
        found = world.pair(1)
        self.assertEqual((found["copies"], found["ready"]), ([album], [year]))
        copy, first = world.row(album), world.row(year)
        self.assertEqual((copy.outcome, copy.copy_of_id, copy.decision_id, copy.item_id), ("copy", first.id, None, None))
        self.assertEqual((first.outcome, first.copy_of_id), ("listed", None))
        self.assertEqual(world.decisions(), 0)

    def test_a_copy_in_a_later_part_attaches_to_the_first_item_not_to_another_copy(self):
        world = PairingWorld(self, parts=3)
        item = world.builder.photo("IMG_0011.JPG", album="Trip")
        world.builder.add(item.media_name, item.data, "Holiday", 1, None, None, None, "")
        world.builder.add(item.media_name, item.data, "Beach", 2, None, None, None, "")
        world.build()
        year = entry_name("IMG_0011.JPG")
        for upto in (1, 2, 3):
            world.pair(upto)
        first = world.row(year)
        for folder in ("Trip", "Holiday", "Beach"):
            copy = world.row(entry_name("IMG_0011.JPG", folder))
            self.assertEqual((copy.outcome, copy.copy_of_id), ("copy", first.id), folder)
        self.assertEqual(first.outcome, "listed")
        self.assertEqual(world.decisions(), 0)

    def test_the_first_seen_entry_names_the_item_whichever_folder_it_is_in(self):
        world = PairingWorld(self, parts=2)
        data = world.builder.image_bytes(".JPG")
        world.builder.add("IMG_0012.JPG", data, "Trip", 0, "json", None, (1.0, 2.0), "")
        world.builder.add("IMG_0012.JPG", data, "Photos from 2019", 1, "json", None, (1.0, 2.0), "")
        world.build()
        world.pair(1)
        world.pair(2)
        trip, year = world.row(entry_name("IMG_0012.JPG", "Trip")), world.row(entry_name("IMG_0012.JPG"))
        self.assertEqual((trip.outcome, trip.copy_of_id, year.outcome, year.copy_of_id), ("listed", None, "copy", trip.id))

    def test_two_copies_in_one_folder_are_two_items_as_whole_export_pairing_has_them(self):
        world = PairingWorld(self)
        data = world.builder.image_bytes(".JPG")
        world.builder.add("IMG_0013.JPG", data, "Photos from 2019", 0, None, None, None, "")
        world.builder.add("IMG_0013(1).JPG", data, "Photos from 2019", 0, None, None, None, "")
        world.build()
        found = world.pair(1)
        self.assertEqual(found["copies"], [])
        self.assertEqual({world.row(entry_name(n)).outcome for n in ("IMG_0013.JPG", "IMG_0013(1).JPG")}, {"waiting_sidecar"})

    def test_pairing_again_attaches_nothing_twice(self):
        world = PairingWorld(self)
        world.builder.photo("IMG_0014.JPG", album="Trip")
        world.build()
        world.pair(1)
        again = world.pair(1)
        self.assertEqual(again["copies"], [])
        self.assertEqual(world.row(entry_name("IMG_0014.JPG", "Trip")).outcome, "copy")

    def test_a_copys_sidecar_names_the_item_s_sidecar_when_the_first_copy_has_none(self):
        world = PairingWorld(self, parts=2)
        item = world.builder.photo("IMG_0015.JPG", form=None)
        world.builder.add(item.media_name, item.data, "Trip", 1, "json", None, (1.0, 2.0), "")
        world.build()
        year = entry_name("IMG_0015.JPG")
        self.assertEqual(world.pair(1)["ready"], [])
        self.assertEqual(world.row(year).outcome, "waiting_sidecar")
        found = world.pair(2)
        self.assertEqual((found["ready"], found["newly_paired"]), ([year], [year]))
        self.assertEqual(world.sidecar_entry_of(year), entry_name("IMG_0015.JPG.json", "Trip"))


class StateTests(unittest.TestCase):
    def one(self, name="IMG_0020.JPG", **photo):
        world = PairingWorld(self)
        world.builder.photo(name, **photo)
        world.build()
        return world, entry_name(name)

    def test_tier_1_with_a_sidecar_is_ready_and_complete(self):
        world, name = self.one()
        found = world.pair(1, index_of(shot(1, sha1=world.sha1_of(name))))
        row = world.row(name)
        self.assertEqual((found["ready"], row.outcome, row.metadata_pending), ([name], "listed", False))
        self.assertIsNotNone(row.sidecar_entry_id)

    def test_tier_1_without_a_sidecar_is_ready_with_its_metadata_pending(self):
        world, name = self.one(form=None)
        found = world.pair(1, index_of(shot(1, sha1=world.sha1_of(name))))
        row = world.row(name)
        self.assertEqual((found["ready"], row.outcome, row.metadata_pending, row.sidecar_entry_id), ([name], "listed", True, None))

    def test_a_takeout_item_of_an_earlier_export_makes_tier_1_too(self):
        world, name = self.one()
        earlier = shot(2, sha1=world.sha1_of(name), source="takeout")
        index = dedupe.PersonIndex(account_id=1, default_person_id=1, person_ids=frozenset({1}), phone=(), takeout=(earlier,))
        self.assertEqual(world.pair(1, index)["ready"], [name])

    def test_a_paired_entry_with_no_phone_shot_matching_is_ready(self):
        world, name = self.one()
        world.fingerprint(name, captured_utc=PHONE_SECOND + 600, make="Apple", model="iPhone 12", width=32, height=24)
        found = world.pair(1, index_of(shot(1, sha1="1" * 40)))
        self.assertEqual((found["ready"], world.row(name).outcome), ([name], "listed"))

    def test_an_entry_with_no_sidecar_yet_waits_for_one(self):
        world, name = self.one(form=None)
        found = world.pair(1, index_of(shot(1, sha1="1" * 40)))
        row = world.row(name)
        self.assertEqual((found["ready"], row.outcome, row.metadata_pending), ([], "waiting_sidecar", False))

    def test_an_edited_copy_waits_for_the_export(self):
        world = PairingWorld(self)
        world.builder.edited("IMG_0021", ".JPG")
        world.build()
        original, edit = entry_name("IMG_0021.JPG"), entry_name("IMG_0021-edited.JPG")
        world.fingerprint(edit, is_edited=True, edited_of=original)
        found = world.pair(1, index_of(shot(1, sha1="1" * 40)))
        self.assertEqual((found["ready"], world.row(edit).outcome, world.row(original).outcome), ([original], "waiting_export", "listed"))

    def test_a_tier_2_candidate_waits_for_the_export(self):
        world, name = self.one()
        world.fingerprint(name, captured_utc=PHONE_SECOND, make="Apple", model="iPhone 12", width=32, height=24)
        found = world.pair(1, index_of(shot(1, sha1="1" * 40)))
        self.assertEqual((found["ready"], world.row(name).outcome), ([], "waiting_export"))

    def test_a_tier_2_candidate_that_was_waiting_for_its_sidecar_waits_for_the_export_once_paired(self):
        world = PairingWorld(self, parts=2)
        world.builder.photo("IMG_0022.JPG", part=0, sidecar_part=1)
        world.build()
        name = entry_name("IMG_0022.JPG")
        world.fingerprint(name, captured_utc=PHONE_SECOND, make="Apple", model="iPhone 12", width=32, height=24)
        index = index_of(shot(1, sha1="1" * 40))
        world.pair(1, index)
        self.assertEqual(world.row(name).outcome, "waiting_sidecar")
        found = world.pair(2, index)
        self.assertEqual((found["ready"], found["newly_paired"], world.row(name).outcome), ([], [name], "waiting_export"))

    def test_a_different_camera_is_not_a_tier_2_candidate(self):
        world, name = self.one()
        world.fingerprint(name, captured_utc=PHONE_SECOND, make="Canon", model="R5", width=32, height=24)
        self.assertEqual(world.pair(1, index_of(shot(1, sha1="1" * 40)))["ready"], [name])

    def test_an_entry_whose_copies_are_all_in_trash_or_bin_waits_for_the_export(self):
        world = PairingWorld(self)
        data = world.builder.image_bytes(".JPG")
        world.builder.add("IMG_0023.JPG", data, "Trash", 0, "json", None, (1.0, 2.0), "")
        world.builder.add("IMG_0023.JPG", data, "Bin", 0, "json", None, (1.0, 2.0), "")
        world.builder.add("IMG_0024.JPG", world.builder.image_bytes(".JPG"), "Bin", 0, "json", None, (1.0, 2.0), "")
        world.build()
        found = world.pair(1, index_of(shot(1, sha1="1" * 40)))
        self.assertEqual(found["ready"], [])
        for name in (entry_name("IMG_0023.JPG", "Bin"), entry_name("IMG_0024.JPG", "Bin")):
            self.assertEqual(world.row(name).outcome, "waiting_export", name)
        self.assertEqual(world.row(entry_name("IMG_0023.JPG", "Trash")).outcome, "copy")

    def test_a_trashed_entry_waits_even_when_its_sha1_is_a_tier_1_match(self):
        world = PairingWorld(self)
        world.builder.add("IMG_0025.JPG", world.builder.image_bytes(".JPG"), "Trash", 0, "json", None, (1.0, 2.0), "")
        world.build()
        name = entry_name("IMG_0025.JPG", "Trash")
        found = world.pair(1, index_of(shot(1, sha1=world.sha1_of(name))))
        self.assertEqual((found["ready"], world.row(name).outcome), ([], "waiting_export"))

    def test_a_copy_outside_trash_makes_the_entry_decidable(self):
        world = PairingWorld(self)
        data = world.builder.image_bytes(".JPG")
        world.builder.add("IMG_0026.JPG", data, "Trash", 0, "json", None, (1.0, 2.0), "")
        world.builder.add("IMG_0026.JPG", data, "Photos from 2019", 0, "json", None, (1.0, 2.0), "")
        world.build()
        found = world.pair(1)
        self.assertEqual(found["ready"], [entry_name("IMG_0026.JPG")])
        self.assertEqual(world.row(entry_name("IMG_0026.JPG", "Trash")).outcome, "copy")

    def test_an_archived_entry_is_not_held_as_trash(self):
        world = PairingWorld(self)
        world.builder.add("IMG_0027.JPG", world.builder.image_bytes(".JPG"), "Archive", 0, "json", None, (1.0, 2.0), "")
        world.build()
        self.assertEqual(world.pair(1)["ready"], [entry_name("IMG_0027.JPG", "Archive")])

    def test_a_live_photo_video_with_no_sidecar_of_its_own_waits_for_it(self):
        world = PairingWorld(self)
        world.builder.live_photo("IMG_0028", video_sidecar=False)
        world.build()
        still, video = entry_name("IMG_0028.HEIC"), entry_name("IMG_0028.MOV")
        found = world.pair(1)
        self.assertEqual((found["ready"], world.row(still).outcome, world.row(video).outcome), ([still], "listed", "waiting_sidecar"))
        self.assertIsNone(world.row(video).sidecar_entry_id)

    def test_a_live_photo_video_with_its_own_sidecar_is_ready(self):
        world = PairingWorld(self)
        world.builder.live_photo("IMG_0029")
        world.build()
        self.assertEqual(world.pair(1)["ready"], [entry_name("IMG_0029.HEIC"), entry_name("IMG_0029.MOV")])

    def test_a_tier_1_live_photo_video_with_no_sidecar_is_ready_with_its_metadata_pending(self):
        world = PairingWorld(self)
        world.builder.live_photo("IMG_0030", video_sidecar=False)
        world.build()
        video = entry_name("IMG_0030.MOV")
        found = world.pair(1, index_of(shot(1, sha1=world.sha1_of(video))))
        self.assertIn(video, found["ready"])
        self.assertEqual((world.row(video).outcome, world.row(video).metadata_pending), ("listed", True))

    def test_a_later_part_s_still_lends_nothing_to_the_waiting_video(self):
        world = PairingWorld(self, parts=2)
        world.builder.photo("IMG_0031.HEIC", part=1)
        world.builder.add("IMG_0031.MOV", world.builder.video_bytes(), "Photos from 2019", 0, None, None, None, "")
        world.build()
        video = entry_name("IMG_0031.MOV")
        world.pair(1)
        found = world.pair(2)
        self.assertEqual(found["ready"], [entry_name("IMG_0031.HEIC")])
        self.assertEqual((world.row(video).outcome, world.row(video).sidecar_entry_id), ("waiting_sidecar", None))

    def test_entries_of_a_part_with_no_sha1_or_already_decided_are_left_alone(self):
        world, name = self.one()
        world.fingerprint(name, outcome="placed")
        found = world.pair(1)
        self.assertEqual((found["ready"], found["copies"], world.row(name).outcome), ([], [], "placed"))


if __name__ == "__main__":
    unittest.main()
