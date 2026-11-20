import json
import unittest

from darkroom.domain import dedupe
from darkroom.domain.ingest import part_pairing
from darkroom.storage import db
from darkroom.storage.models import DedupeDecision, Export, TakeoutEntry, TakeoutSidecar
from tests.takeout_fixtures import GOOGLE_PHOTOS, TakeoutBuilder
from tests.test_pairing import CUT
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
                "metadata_due": sorted(row.name for row in result.metadata_due),
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
        world.fingerprint(media, outcome="skipped")
        second = world.pair(2, index)
        self.assertEqual((second["ready"], second["newly_paired"], second["metadata_due"]), ([], [media], [media]))
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


class SharedPathAndNameTests(unittest.TestCase):
    def entry_in_part(self, world, name, part):
        with db.session(world.world.engine) as opened:
            found = opened.query(TakeoutEntry).filter_by(name=name, part_number=part).one()
            opened.expunge(found)
            return found

    def sidecar_name_of(self, world, found):
        with db.session(world.world.engine) as opened:
            return None if found.sidecar_entry_id is None else opened.get(TakeoutEntry, found.sidecar_entry_id).name

    def test_two_long_names_with_one_cut_name_pair_correctly_when_they_arrive_in_different_parts(self):
        world = PairingWorld(self, parts=2)
        one, two = world.builder.shared_cut(CUT, ("_one.jpg", "_two.jpg"), sidecar_part=0)
        two.part = 1
        world.build()
        first, second = entry_name(one.media_name), entry_name(two.media_name)
        world.pair(1)
        world.pair(2)
        self.assertEqual(self.sidecar_name_of(world, world.row(first)), entry_name(f"{CUT}.json"))
        self.assertEqual(self.sidecar_name_of(world, world.row(second)), entry_name(f"{CUT}(1).json"))

    def test_a_path_repeated_in_a_later_part_pairs_every_entry_at_that_path(self):
        world = PairingWorld(self, parts=2)
        world.builder.photo("IMG_0040.JPG", part=0, form=None)
        second = world.builder.photo("IMG_0040.JPG", part=1, form="json", sidecar_part=1, data=world.builder.image_bytes(".JPG") + b"x")
        world.build()
        name = entry_name("IMG_0040.JPG")
        world.pair(1)
        found = world.pair(2)
        earlier, later = self.entry_in_part(world, name, 1), self.entry_in_part(world, name, 2)
        self.assertNotEqual(earlier.id, later.id)
        self.assertEqual(self.sidecar_name_of(world, later), entry_name(second.sidecar_name))
        self.assertEqual(self.sidecar_name_of(world, earlier), entry_name(second.sidecar_name))
        self.assertIn(name, found["ready"])


class LateSidecarTests(unittest.TestCase):
    def test_a_late_sidecar_pairs_with_an_edited_copy_held_for_the_export_and_keeps_it_held(self):
        world = PairingWorld(self, parts=2)
        original = world.builder.photo("IMG_0050.JPG", part=0, form="json", sidecar_part=1)
        edit = world.builder.add("IMG_0050-edited.JPG", world.builder.image_bytes(".JPG"), "Photos from 2019", 0, "json", 1, (1.0, 2.0), "")
        world.builder.manifest.edited_of[edit.media_name] = original.media_name
        world.build()
        name, edited = entry_name("IMG_0050.JPG"), entry_name("IMG_0050-edited.JPG")
        world.fingerprint(edited, is_edited=True, edited_of=name)
        world.pair(1)
        self.assertEqual((world.row(edited).outcome, world.row(edited).sidecar_entry_id), ("waiting_export", None))
        found = world.pair(2)
        row = world.row(edited)
        self.assertIsNotNone(row.sidecar_entry_id)
        self.assertEqual(row.outcome, "waiting_export")
        self.assertIn(edited, found["newly_paired"])
        self.assertNotIn(edited, found["ready"])

    def test_a_late_sidecar_pairs_with_a_trashed_entry_held_for_the_export_and_keeps_it_held(self):
        world = PairingWorld(self, parts=2)
        item = world.builder.add("IMG_0051.JPG", world.builder.image_bytes(".JPG"), "Trash", 0, "json", 1, (1.0, 2.0), "")
        world.build()
        name = entry_name(item.media_name, "Trash")
        world.pair(1)
        self.assertEqual((world.row(name).outcome, world.row(name).sidecar_entry_id), ("waiting_export", None))
        found = world.pair(2)
        self.assertIsNotNone(world.row(name).sidecar_entry_id)
        self.assertEqual((world.row(name).outcome, found["newly_paired"], found["ready"]), ("waiting_export", [name], []))


class CompetingNameTests(unittest.TestCase):
    def test_swapped_titles_pair_as_the_whole_export_does_when_the_second_name_arrives_in_a_later_part(self):
        world = PairingWorld(self, parts=2)
        one, two = world.builder.shared_cut(CUT, ("_one.jpg", "_two.jpg"), titles=(f"{CUT}_two.jpg", f"{CUT}_one.jpg"), sidecar_part=0)
        two.part = 1
        world.build()
        first, second = entry_name(one.media_name), entry_name(two.media_name)
        self.assertEqual(world.pair(1)["ready"], [])
        self.assertEqual(world.row(first).outcome, "waiting_sidecar")
        self.assertEqual(world.pair(2)["ready"], [])
        self.assertEqual(world.sidecar_entry_of(first), entry_name(f"{CUT}(1).json"))
        self.assertEqual(world.sidecar_entry_of(second), entry_name(f"{CUT}.json"))
        self.assertEqual({world.row(first).outcome, world.row(second).outcome}, {"waiting_sidecar"})

    def test_a_stem_sidecar_is_left_unpaired_when_a_video_of_the_same_stem_arrives_in_a_later_part(self):
        world = PairingWorld(self, parts=2)
        world.builder.without_extension("IMG_0060", ".jpg")
        world.builder.add("IMG_0060.mp4", world.builder.video_bytes(), "Photos from 2019", 1, None, None, None, "")
        world.build()
        still, video = entry_name("IMG_0060.jpg"), entry_name("IMG_0060.mp4")
        self.assertEqual(world.pair(1)["ready"], [])
        self.assertEqual(world.row(still).outcome, "waiting_sidecar")
        world.pair(2)
        self.assertEqual((world.sidecar_entry_of(still), world.sidecar_entry_of(video)), (None, None))

    def test_a_creation_waits_for_its_own_sidecar_rather_than_keeping_its_base_files(self):
        world = PairingWorld(self, parts=2)
        world.builder.photo("IMG_0061.jpg")
        made = world.builder.photo("IMG_0061-effects.jpg", sidecar_part=1)
        world.build()
        name = entry_name("IMG_0061-effects.jpg")
        self.assertNotIn(name, world.pair(1)["ready"])
        self.assertEqual(world.row(name).outcome, "waiting_sidecar")
        self.assertIn(name, world.pair(2)["ready"])
        self.assertEqual(world.sidecar_entry_of(name), entry_name(made.sidecar_name))

    def test_a_placed_entry_keeps_the_sidecar_it_was_placed_with_even_when_a_later_name_contests_it(self):
        world = PairingWorld(self, parts=2)
        item = world.builder.photo("IMG_0063.jpg")
        item.sidecar_data = json.dumps(json.loads(item.sidecar_data) | {"title": "renamed.jpg"}).encode()
        world.builder.add("IMG_0063.jpg.mp4", world.builder.video_bytes(), "Photos from 2019", 1, None, None, None, "")
        world.build()
        name = entry_name("IMG_0063.jpg")
        self.assertIn(name, world.pair(1)["ready"])
        world.fingerprint(name, outcome="placed")
        world.pair(2)
        self.assertEqual(world.sidecar_entry_of(name), entry_name(item.sidecar_name))


class SidecarTimeTests(unittest.TestCase):
    def held(self, sidecar_part=None, **fields):
        world = PairingWorld(self, parts=2)
        world.builder.photo("IMG_0070.JPG", taken=int(PHONE_SECOND), sidecar_part=sidecar_part)
        world.build()
        name = entry_name("IMG_0070.JPG")
        world.fingerprint(name, **({"captured_utc": None, "captured_local": None, "make": "Apple", "model": "iPhone 12", "width": 32, "height": 24} | fields))
        index = index_of(shot(1, sha1="1" * 40))
        found = [world.pair(1, index)]
        if sidecar_part is not None:
            self.assertEqual(world.row(name).outcome, "waiting_sidecar")
            found.append(world.pair(2, index))
        return found[-1], world.row(name), name

    def test_a_sidecar_dated_tier_2_candidate_is_held_for_the_export(self):
        found, row, _ = self.held()
        self.assertEqual((found["ready"], row.outcome), ([], "waiting_export"))

    def test_a_sidecar_dated_tier_2_candidate_released_by_a_later_part_is_held_for_the_export(self):
        found, row, name = self.held(sidecar_part=1)
        self.assertEqual((found["ready"], found["newly_paired"], row.outcome), ([], [name], "waiting_export"))

    def test_a_wall_clock_time_the_sidecar_places_on_a_phone_shot_is_held_for_the_export(self):
        found, row, _ = self.held(captured_local="2017-07-14T02:40:00")
        self.assertEqual((found["ready"], row.outcome), ([], "waiting_export"))

    def test_a_scan_date_the_sidecar_can_replace_is_held_for_the_export(self):
        found, row, _ = self.held(captured_utc=PHONE_SECOND + 30 * 86400)
        self.assertEqual((found["ready"], row.outcome), ([], "waiting_export"))

    def wall_clock_shot(self, local, gps):
        world = PairingWorld(self)
        world.builder.photo("IMG_0072.JPG", taken=int(PHONE_SECOND), gps=gps)
        world.build()
        name = entry_name("IMG_0072.JPG")
        world.fingerprint(name, captured_utc=None, captured_local=None, make="Apple", model="iPhone 12", width=32, height=24)
        found = world.pair(1, index_of(shot(1, sha1="1" * 40, captured_utc=None, captured_local=local)))
        return found, world.row(name), name

    def test_a_sidecar_time_without_gps_is_held_when_the_files_own_gps_could_place_it_on_a_phone_shots_wall_clock(self):
        found, row, _ = self.wall_clock_shot("2017-07-13T22:40:00", None)
        self.assertEqual((found["ready"], row.outcome), ([], "waiting_export"))

    def test_a_sidecar_time_without_gps_is_held_for_a_zone_with_an_odd_offset(self):
        found, row, _ = self.wall_clock_shot("2017-07-14T08:25:00", None)
        self.assertEqual((found["ready"], row.outcome), ([], "waiting_export"))

    def test_a_wall_clock_no_zone_could_give_leaves_the_entry_ready(self):
        for local in ("2017-07-13T22:40:07", "2017-07-15T22:40:00"):
            with self.subTest(local=local):
                found, row, name = self.wall_clock_shot(local, None)
                self.assertEqual((found["ready"], row.outcome), ([name], "listed"))

    def test_a_sidecar_with_gps_places_the_wall_clock_in_its_own_zone_only(self):
        found, row, name = self.wall_clock_shot("2017-07-14T08:25:00", (40.7, -74.0))
        self.assertEqual((found["ready"], row.outcome), ([name], "listed"))
        found, row, _ = self.wall_clock_shot("2017-07-13T22:40:00", (40.7, -74.0))
        self.assertEqual((found["ready"], row.outcome), ([], "waiting_export"))

    def test_a_wall_clock_s_offset_derived_from_the_sidecar_gives_its_utc_time(self):
        entry = TakeoutEntry(name=entry_name("IMG_0073.JPG"), captured_utc=None, captured_local="2017-07-14T04:40:01")
        text = json.dumps({"photoTakenTime": {"timestamp": str(int(PHONE_SECOND))}})
        self.assertIn((PHONE_SECOND + 1, "2017-07-14T04:40:01"), part_pairing.sidecar_moments(entry, text))

    def test_a_phone_shot_only_the_offset_derived_utc_time_matches_is_held_for_the_export(self):
        world = PairingWorld(self)
        world.builder.photo("IMG_0074.JPG", taken=int(PHONE_SECOND), gps=None)
        world.build()
        name = entry_name("IMG_0074.JPG")
        world.fingerprint(name, captured_utc=None, captured_local="2017-07-14T04:40:01", make="Apple", model="iPhone 12", width=32, height=24)
        found = world.pair(1, index_of(shot(1, sha1="1" * 40, captured_utc=PHONE_SECOND + 1)))
        self.assertEqual((found["ready"], world.row(name).outcome), ([], "waiting_export"))

    def test_a_sidecar_that_does_not_parse_adds_no_time(self):
        entry = TakeoutEntry(name=entry_name("IMG_0071.JPG"), captured_utc=PHONE_SECOND, captured_local=None)
        self.assertEqual((part_pairing.sidecar_moments(entry, "not json"), part_pairing.sidecar_moments(entry, None)), ([], []))

    def test_a_sidecar_time_on_no_phone_shot_leaves_the_entry_ready(self):
        found, row, name = self.held(captured_utc=PHONE_SECOND + 30 * 86400, make="Canon")
        self.assertEqual((found["ready"], row.outcome), ([name], "listed"))


class RepeatTests(unittest.TestCase):
    def test_pairing_the_same_part_twice_re_returns_an_undecided_released_entry(self):
        world = PairingWorld(self, parts=2)
        world.builder.photo("IMG_0062.JPG", part=0, sidecar_part=1)
        world.build()
        media = entry_name("IMG_0062.JPG")
        world.pair(1)
        self.assertEqual(world.pair(2)["ready"], [media])
        again = world.pair(2)
        self.assertEqual((again["ready"], again["newly_paired"]), ([media], []))
        world.fingerprint(media, outcome="placed")
        self.assertEqual(world.pair(2)["ready"], [])

    def test_a_late_sidecars_metadata_fill_is_returned_until_it_is_done(self):
        world = PairingWorld(self, parts=2)
        world.builder.photo("IMG_0064.JPG", part=0, sidecar_part=1)
        world.build()
        media = entry_name("IMG_0064.JPG")
        index = index_of(shot(7, sha1=world.sha1_of(media)))
        world.pair(1, index)
        world.fingerprint(media, outcome="skipped")
        self.assertEqual(world.pair(2, index)["metadata_due"], [media])
        self.assertEqual(world.pair(2, index)["metadata_due"], [media])
        world.fingerprint(media, metadata_pending=False)
        self.assertEqual(world.pair(2, index)["metadata_due"], [])

    def test_a_tier_1_entry_paired_by_a_contestable_rule_keeps_its_metadata_pending(self):
        world = PairingWorld(self, parts=2)
        world.builder.without_extension("IMG_0065", ".jpg")
        world.build()
        name = entry_name("IMG_0065.jpg")
        index = index_of(shot(7, sha1=world.sha1_of(name)))
        found = world.pair(1, index)
        self.assertEqual((found["ready"], found["metadata_due"], world.row(name).metadata_pending), ([name], [], True))
        world.fingerprint(name, outcome="skipped")
        self.assertEqual(world.pair(2, index)["metadata_due"], [])


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
        world, name = self.one(taken=int(PHONE_SECOND) + 600)
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
