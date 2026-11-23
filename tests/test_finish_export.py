import json
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from sqlalchemy import event, select

from darkroom.domain import exports
from darkroom.domain.ingest import finish_export, metadata, part_free, part_reader, place, run
from darkroom.storage import paths
from darkroom.storage.models import Alert, DedupeDecision, Event, Export, ImportRun, MediaItem
from tests.import_world import DAY, NOW
from tests.privacy import no_leaks
from tests.test_part_ingest import Crash, PartWorld, recording_writes, short

SUMMARY = "Takeout import finished"
MOMENT = {"taken": "2019:09:09 09:09:09", "offset": "+02:00", "make": "Apple", "model": "iPhone 11"}


class FinishWorld(PartWorld):
    def finish(self, export, at=NOW, tools=None):
        self.opened.expire_all()
        found = self.opened.get(Export, export.id)
        return finish_export.finish(self.opened, self.settings, found, at, tools or self.tools)

    def finished(self, build, parts=3):
        export = self.create(build, parts=parts)
        self.ingest_all(export, range(1, parts + 1))
        self.finish(export)
        return export

    def run_row(self):
        self.opened.expire_all()
        row = self.opened.execute(select(ImportRun).order_by(ImportRun.id.desc())).scalars().first()
        return (row.status, row.stage, row.added, row.skipped_t1, row.skipped_t2, row.skipped_t3, row.review, row.missing_gps, row.failed, row.repeated,
                row.not_imported, row.orphan_sidecars, row.unpaired_media, row.unmatched_by_person, row.paths_renamed, row.sidecars_differ)

    def import_done(self):
        return self.opened.execute(select(Event).where(Event.type == "import_done")).scalar_one().detail

    def reasons(self):
        self.opened.expire_all()
        rows = self.opened.execute(select(DedupeDecision).order_by(DedupeDecision.source_path)).scalars()
        return [(short(row.source_path), row.tier, row.reason) for row in rows]

    def comparable(self):
        state = self.state()
        return {"decisions": state["decisions"], "reasons": self.reasons(), "reviews": sorted(state["reviews"]), "items": state["items"],
                "files": {key: value for key, value in state["files"].items() if "/scratch/" not in key and "/staging/" not in key and "/.darkroom-unpacked/" not in key}}

    def whole_export(self, scenario, parts=3):
        twin = FinishWorld(self.test)
        export = twin.create(scenario(twin), parts=parts, status="downloaded", queue=False)
        twin.run_import(export)
        return twin


def burst_scenario(world):
    def build(builder):
        world.scenario(builder)
        world.original("jo", "IMG_1006.JPG", world.scratch_jpeg("b0.jpg", colour=(200, 10, 10), **MOMENT))
        builder.photo("IMG_1006.JPG", data=world.scratch_jpeg("b1.jpg", colour=(201, 12, 10), **MOMENT), gps=None, part=0)
        builder.photo("IMG_1007.JPG", data=world.scratch_jpeg("b2.jpg", colour=(202, 14, 12), **MOMENT), gps=None, part=2)

    return build


class StrayPlacementCopyTests(unittest.TestCase):
    def stray(self, world, export, name):
        stage = world.root / "_inbox/jo" / f"export-{export.id}" / paths.PLACEMENTS
        stage.mkdir(mode=0o700, parents=True, exist_ok=True)
        (stage / name).write_bytes(b"left by a crash")
        return stage

    def test_stray_placement_copies_before_and_during_a_finish_are_swept_and_nothing_else_is(self):
        world = FinishWorld(self)
        build = burst_scenario(world)
        export = world.create(build)
        world.ingest_all(export)
        stage = self.stray(world, export, "abcdef0123456789.copy")
        (stage / "notes.txt").write_bytes(b"not a placement copy")
        real = finish_export.Finish.leave_out

        def crash_left_one(finishing, canon):
            self.stray(world, export, "0123456789abcdef.copy")
            return real(finishing, canon)

        with mock.patch.object(finish_export.Finish, "leave_out", crash_left_one):
            world.finish(export)
        self.assertEqual(sorted(path.name for path in stage.iterdir()), ["notes.txt"])
        self.assertEqual((stage / "notes.txt").read_bytes(), b"not a placement copy")
        twin = FinishWorld(self)
        twin_export = twin.create(burst_scenario(twin))
        twin.ingest_all(twin_export)
        twin.finish(twin_export)
        mine = world.comparable()
        mine["files"] = {key: value for key, value in mine["files"].items() if "/.placements/" not in f"/{key}"}
        self.assertEqual(mine, twin.comparable())


class EquivalenceTests(unittest.TestCase):
    def setUp(self):
        self.world = FinishWorld(self)
        self.build = burst_scenario(self.world)

    def test_finishing_gives_the_decisions_and_files_whole_export_ingest_gives(self):
        self.world.finished(self.build)
        twin = self.world.whole_export(burst_scenario)
        self.assertEqual(self.world.comparable(), twin.comparable())
        tiers = {name: tier for name, tier, _ in self.world.reasons()}
        self.assertEqual((tiers["Photos from 2019/IMG_1006.JPG"], tiers["Photos from 2019/IMG_1007.JPG"]), (4, 4))
        self.assertEqual(tiers["Photos from 2019/IMG_1004-edited.JPG"], 3)
        self.assertEqual(tiers["Photos from 2019/IMG_1003.JPG"], 2)

    def test_the_run_counts_and_the_summary_equal_whole_export_ingest(self):
        self.world.finished(self.build)
        twin = self.world.whole_export(burst_scenario)
        mine, theirs = self.world.run_row(), twin.run_row()
        self.assertEqual(mine[:14], theirs[:14])
        self.assertEqual(self.world.sent(SUMMARY), 1)
        self.assertEqual([pair for pair in self.world.recorder.sent if pair[0] == SUMMARY], [pair for pair in twin.recorder.sent if pair[0] == SUMMARY])
        self.assertEqual((self.world.opened.get(Export, 1).status, self.world.opened.get(Export, 1).stage), ("imported", "done"))

    def test_every_held_entry_is_decided_or_left_out_and_none_waits_afterwards(self):
        self.world.finished(self.build)
        held = {name for name, outcome in self.world.outcomes().items() if outcome in ("waiting_sidecar", "waiting_export", "listed")}
        self.assertEqual(held, set())
        self.assertEqual(self.world.part(self.world.opened.get(Export, 1), 3).held, 0)


class SidecarTests(unittest.TestCase):
    def test_an_entry_with_a_provisional_sidecar_is_decided_with_it_and_one_without_any_is_decided_without(self):
        world = FinishWorld(self)

        def build(builder):
            builder.without_extension("IMG_7001", ".JPG", part=0)
            builder.photo("IMG_7002.JPG", form=None, part=1)

        export = world.create(build, parts=2)
        world.ingest_all(export, (1, 2))
        self.assertEqual({name: outcome for name, outcome in world.outcomes().items()},
                         {"Photos from 2019/IMG_7001.JPG": "waiting_sidecar", "Photos from 2019/IMG_7002.JPG": "waiting_sidecar"})
        self.assertIsNotNone(world.entry("Photos from 2019/IMG_7001.JPG").sidecar_entry_id)
        world.finish(export)
        twin = world.whole_export(lambda twin: build, parts=2)
        self.assertEqual(world.comparable(), twin.comparable())
        items = {row.path.rsplit("/", 1)[-1]: row for row in world.opened.execute(select(MediaItem).where(MediaItem.source == "takeout")).scalars()}
        self.assertEqual((items["IMG_7001.JPG"].lat, items["IMG_7001.JPG"].lon), (40.7, -74.0))
        self.assertIsNone(items["IMG_7002.JPG"].sidecar_json)

    def test_a_tier_1_entry_with_a_provisional_sidecar_is_filled_at_completion(self):
        world = FinishWorld(self)
        original = "jo/originals/IMG_8001.JPG"

        def build(builder):
            exact = builder.image_bytes(".JPG")
            world.original("jo", "IMG_8001.JPG", exact)
            builder.add("IMG_8001.JPG", exact, "Photos from 2019", 0, None, None, (46.0, 11.0), "a provisional description", sidecar_name="IMG_8001.json")

        export = world.create(build, parts=2)
        world.ingest_all(export, (1, 2))
        row = world.entry("Photos from 2019/IMG_8001.JPG")
        self.assertEqual((row.outcome, row.metadata_pending), ("skipped", True))
        self.assertFalse(world.xmp_of(original).exists())
        world.finish(export)
        self.assertFalse(world.entry("Photos from 2019/IMG_8001.JPG").metadata_pending)
        self.assertIn("a provisional description", world.xmp_of(original).read_text())

    def test_a_live_photo_video_without_its_own_sidecar_is_decided_without_one_and_borrows_nothing(self):
        world = FinishWorld(self)

        def build(builder):
            builder.live_photo("IMG_6001", part=0, video_sidecar=False)

        export = world.create(build, parts=2)
        world.ingest_all(export, (1, 2))
        self.assertEqual(world.entry("Photos from 2019/IMG_6001.MOV").outcome, "waiting_sidecar")
        world.finish(export)
        items = {row.path.rsplit("/", 1)[-1]: row for row in world.opened.execute(select(MediaItem).where(MediaItem.source == "takeout")).scalars()}
        self.assertEqual(sorted(items), ["IMG_6001.HEIC", "IMG_6001.MOV"])
        self.assertIsNotNone(items["IMG_6001.HEIC"].sidecar_json)
        self.assertIsNone(items["IMG_6001.MOV"].sidecar_json)
        self.assertFalse(items["IMG_6001.MOV"].sidecar_from_still)


class CanonicalTests(unittest.TestCase):
    def test_an_album_copy_seen_first_has_its_decision_renamed_to_the_year_folder_copy_and_d2_is_counted(self):
        world = FinishWorld(self)

        def build(builder):
            data = builder.image_bytes(".JPG")
            builder.add("IMG_5001.JPG", data, "Holiday", 0, "json", None, (45.4, 12.3), "", taken=1_500_000_000)
            builder.add("IMG_5001.JPG", data, "Photos from 2019", 1, "json", None, (45.4, 12.3), "", taken=1_500_000_000)

        export = world.create(build, parts=2)
        world.ingest_all(export, (1, 2))
        self.assertEqual([short(row.source_path) for row in world.opened.execute(select(DedupeDecision)).scalars()], ["Holiday/IMG_5001.JPG"])
        world.finish(export)
        self.assertEqual([short(row.source_path) for row in world.opened.execute(select(DedupeDecision)).scalars()], ["Photos from 2019/IMG_5001.JPG"])
        self.assertEqual(world.run_row()[-2:], (1, 0))
        self.assertTrue(world.import_done().endswith("1 paths renamed to their canonical copy, 0 sidecar choices differ"), world.import_done())
        self.assertEqual({key.rsplit("/", 1)[-1] for key in world.google_only()}, {"IMG_5001.JPG"})
        self.assertEqual(world.comparable()["decisions"][0][0], "Photos from 2019/IMG_5001.JPG")
        self.assertEqual(world.opened.execute(select(Event).where(Event.type == "export_sidecar_choices_differ")).first(), None)

    def test_a_copy_whose_sidecar_choice_differs_from_the_canonical_one_is_counted_as_d3_with_its_own_event(self):
        world = FinishWorld(self)

        def build(builder):
            data = builder.image_bytes(".JPG")
            builder.add("IMG_6002.JPG", data, "Holiday", 0, "json", None, (10.0, 20.0), "", taken=1_500_000_000)
            builder.add("IMG_6002.JPG", data, "Photos from 2019", 1, "json", None, (30.0, 40.0), "", taken=1_500_000_000)

        export = world.create(build, parts=2)
        with no_leaks(self, "IMG_6002.JPG"):
            world.ingest_all(export, (1, 2))
            world.finish(export)
        self.assertEqual(world.run_row()[-2:], (1, 1))
        event = world.opened.execute(select(Event).where(Event.type == "export_sidecar_choices_differ")).scalar_one()
        self.assertEqual((event.subject, event.detail), (f"export {export.id}", "1 items"))
        self.assertTrue(world.import_done().endswith("1 paths renamed to their canonical copy, 1 sidecar choices differ"), world.import_done())
        self.assertNotIn("IMG_6002", world.import_done())

    def test_copies_with_the_same_sidecar_text_are_not_counted_as_d3(self):
        world = FinishWorld(self)

        def build(builder):
            data = builder.image_bytes(".JPG")
            builder.add("IMG_6003.JPG", data, "Holiday", 0, "json", None, (10.0, 20.0), "", taken=1_500_000_000)
            builder.add("IMG_6003.JPG", data, "Photos from 2019", 1, "json", None, (10.0, 20.0), "", taken=1_500_000_000)

        export = world.create(build, parts=2)
        world.ingest_all(export, (1, 2))
        world.finish(export)
        self.assertEqual(world.run_row()[-2:], (1, 0))


class LeftOutTests(unittest.TestCase):
    def build(self, builder):
        builder.photo("IMG_9001.JPG", part=0)
        builder.add("IMG_9002.JPG", builder.image_bytes(".JPG"), "Trash", 0, "json", None, (45.4, 12.3), "")
        builder.add("IMG_9003.JPG", builder.image_bytes(".JPG"), "Bin", 1, "json", None, (45.4, 12.3), "")
        builder.add("IMG_9004.JPG", builder.image_bytes(".JPG"), "Photos from 2019", 1, None, None, None, "")
        builder.orphan_sidecar("IMG_9999.JPG", part=1)
        builder.loose_file("notes-canary-jo.txt", part=0)

    def test_trash_and_bin_items_are_left_out_and_counted_and_the_never_paired_counts_are_reported_as_whole_export_ingest_does(self):
        world = FinishWorld(self)
        export = world.create(self.build, parts=2)
        world.ingest_all(export, (1, 2))
        self.assertEqual({name: outcome for name, outcome in world.outcomes().items() if "Trash" in name or "Bin" in name},
                         {"Trash/IMG_9002.JPG": "waiting_export", "Bin/IMG_9003.JPG": "waiting_export"})
        with no_leaks(self, "notes-canary-jo.txt", "IMG_9004.JPG"):
            world.finish(export)
        twin = world.whole_export(lambda twin: self.build, parts=2)
        done = world.opened.execute(select(ImportRun)).scalar_one()
        twin_done = twin.opened.execute(select(ImportRun)).scalar_one()
        self.assertEqual((json.loads(done.not_imported), done.orphan_sidecars, done.unpaired_media),
                         (json.loads(twin_done.not_imported), twin_done.orphan_sidecars, twin_done.unpaired_media))
        self.assertEqual(json.loads(done.not_imported), {".txt": 1, "trash": 2})
        self.assertEqual((done.orphan_sidecars, done.unpaired_media), (1, 1))
        self.assertTrue(world.import_done().startswith(twin.import_done()[:-1].split(", 0 ")[0]))
        self.assertIn(", 3 not imported (1 .txt, 2 in Trash or Bin), 1 sidecar matched no file, 1 without a sidecar", world.import_done())
        self.assertEqual(world.decisions(), twin.decisions())
        self.assertEqual(world.google_only(), twin.google_only())
        self.assertEqual({name for name, *_ in world.decisions()}, {"Photos from 2019/IMG_9001.JPG", "Photos from 2019/IMG_9004.JPG"})
        self.assertEqual({outcome for name, outcome in world.outcomes().items() if "Trash" in name or "Bin" in name}, {"skipped"})
        self.assertNotIn("canary", world.import_done())


def small_scenario(world):
    def build(builder):
        exact = builder.image_bytes(".JPG")
        world.original("jo", "IMG_1001.JPG", exact)
        builder.photo("IMG_1001.JPG", data=exact, gps=None, part=0)
        builder.photo("IMG_2002.JPG", gps=(47.0, 12.0), part=0, sidecar_part=1)
        builder.photo("IMG_2003.JPG", form=None, part=0)
        base = builder.image_bytes(".JPG")
        world.original("sam", "IMG_1004.JPG", base)
        builder.photo("IMG_1004.JPG", data=base, part=0)
        builder.add("IMG_1004-edited.JPG", builder.image_bytes(".JPG"), "Photos from 2019", 1, None, None, None, "")
        data = builder.image_bytes(".JPG")
        builder.add("IMG_5001_album.JPG", data, "Holiday", 0, "json", None, (45.4, 12.3), "", taken=1_500_000_000)
        builder.add("IMG_5001.JPG", data, "Photos from 2019", 1, "json", None, (46.4, 12.3), "", taken=1_500_000_000)
        builder.add("IMG_9002.JPG", builder.image_bytes(".JPG"), "Trash", 1, "json", None, (45.4, 12.3), "")

    return build


def crashing_at(target):
    seen = [0]

    def hook():
        seen[0] += 1
        if seen[0] == target:
            raise Crash

    return hook


class ReviewFindingTests(unittest.TestCase):
    def test_placing_held_items_keeps_scratch_in_bounded_batches_and_empties_it(self):
        world = FinishWorld(self)

        def build(builder):
            for number in range(5):
                builder.photo(f"IMG_85{number}.JPG", form=None, part=0)

        export = world.create(build, parts=2)
        world.ingest_all(export, (1, 2))
        self.assertEqual(sum(1 for outcome in world.outcomes().values() if outcome == "waiting_sidecar"), 5)
        scratch = world.photos.scratch("jo", f"export-{export.id}")
        high = [0]
        real_stream = part_reader.stream_one

        def stream(*args, **kwargs):
            streamed = real_stream(*args, **kwargs)
            high[0] = max(high[0], len(list(scratch.glob("*"))))
            return streamed

        with mock.patch.object(part_reader, "BATCH_FILES", 2), mock.patch.object(part_reader, "stream_one", side_effect=stream):
            world.finish(export)
        self.assertEqual(sum(1 for outcome in world.outcomes().values() if outcome == "placed"), 5)
        self.assertGreater(high[0], 0)
        self.assertLessEqual(high[0], 2)
        self.assertEqual(list(scratch.glob("*")), [])

    def test_a_pending_tier_1_entry_is_filled_from_the_canonical_copys_sidecar_not_the_arrival_copys(self):
        world = FinishWorld(self)
        original = "jo/originals/IMG_8101.JPG"

        def build(builder):
            exact = builder.image_bytes(".JPG")
            world.original("jo", "IMG_8101.JPG", exact)
            builder.add("IMG_8101.JPG", exact, "Holiday", 0, None, None, (10.0, 20.0), "from the album", sidecar_name="IMG_8101.json")
            builder.add("IMG_8101.JPG", exact, "Photos from 2019", 1, "json", None, (30.0, 40.0), "from the year folder")

        export = world.create(build, parts=2)
        world.ingest_all(export, (1, 2))
        self.assertTrue(world.entry("Holiday/IMG_8101.JPG").metadata_pending)
        world.finish(export)
        self.assertFalse(world.entry("Holiday/IMG_8101.JPG").metadata_pending)
        written = world.xmp_of(original).read_text()
        self.assertIn("from the year folder", written)
        self.assertNotIn("from the album", written)

    def test_an_edit_that_arrived_under_an_album_alias_is_decided_as_an_edit_of_its_canonical_name(self):
        world = FinishWorld(self)

        def scenario(each):
            def build(builder):
                base = builder.image_bytes(".JPG")
                each.original("jo", "IMG_4000.JPG", base)
                builder.photo("IMG_4000.JPG", data=base, part=0)
                edit = builder.image_bytes(".JPG")
                builder.add("alias.JPG", edit, "Holiday", 0, None, None, None, "")
                builder.add("IMG_4000-edited.JPG", edit, "Photos from 2019", 1, None, None, None, "")

            return build

        export = world.create(scenario(world), parts=2)
        world.ingest_all(export, (1, 2))
        world.finish(export)
        tiers = {name: tier for name, tier, _ in world.reasons()}
        self.assertEqual(tiers["Photos from 2019/IMG_4000-edited.JPG"], 3)
        self.assertEqual(world.reasons(), world.whole_export(scenario, parts=2).reasons())

    def test_a_decided_item_whose_late_sidecar_gives_it_a_time_is_a_rival_of_a_held_candidate(self):
        world = FinishWorld(self)

        def scenario(each):
            def build(builder):
                camera = {"make": "Apple", "model": "iPhone 11"}
                undated = each.scratch_jpeg("q.jpg", colour=(5, 5, 5), **camera)
                each.original("jo", "IMG_Q.JPG", undated)
                builder.add("IMG_Q.JPG", undated, "Photos from 2019", 0, "json", 1, None, "", taken=1562221815)
                moment = {"taken": "2019:07:04 08:30:15", "offset": "+02:00", **camera}
                each.original("jo", "IMG_X.JPG", each.scratch_jpeg("x.jpg", colour=(10, 120, 30), **moment))
                builder.photo("IMG_C.JPG", data=each.scratch_jpeg("c.jpg", colour=(12, 118, 33), **moment), gps=None, taken=1562221815, part=0)

            return build

        export = world.create(scenario(world), parts=2)
        world.ingest_all(export, (1, 2))
        self.assertEqual(world.entry("Photos from 2019/IMG_C.JPG").outcome, "waiting_export")
        world.finish(export)
        tiers = {name: tier for name, tier, _ in world.reasons()}
        self.assertEqual(tiers["Photos from 2019/IMG_C.JPG"], 4)
        self.assertEqual(world.reasons(), world.whole_export(scenario, parts=2).reasons())

    def test_a_failed_remeasurement_sweeps_its_scratch_copies(self):
        world = FinishWorld(self)

        def build(builder):
            for number in range(3):
                builder.photo(f"IMG_86{number}.JPG", form=None, part=0)

        export = world.create(build, parts=2)
        world.ingest_all(export, (1, 2))

        def broken(paths):
            raise TimeoutError("exiftool is slow")

        found = world.opened.get(Export, export.id)
        tools = run.Tools(read=broken, notifier=world.recorder)
        finished = finish_export.finish(world.opened, world.settings, found, NOW, tools)
        self.assertEqual(finished.status, "failed")
        self.assertEqual(world.opened.get(Export, export.id).status, "failed")
        scratch = world.photos.scratch("jo", f"export-{export.id}")
        self.assertEqual(list(scratch.glob("*")) if scratch.exists() else [], [])

    def test_identical_bytes_under_several_names_in_one_folder_leave_no_root_or_slot_behind(self):
        world = FinishWorld(self)

        def build(twin):
            def add(builder):
                data = builder.image_bytes(".JPG")
                builder.add("Z.JPG", data, "Holiday", 0, "json", None, (10.0, 20.0), "", taken=1_500_000_000)
                builder.add("B.JPG", data, "Photos from 2019", 1, "json", None, (10.0, 20.0), "", taken=1_500_000_000)
                builder.add("A.JPG", data, "Photos from 2019", 2, "json", None, (10.0, 20.0), "", taken=1_500_000_000)

            return add

        export = world.create(build(world), parts=3)
        world.ingest_all(export, (1, 2, 3))
        world.finish(export)
        self.assertEqual(world.opened.get(Export, export.id).status, "imported")
        self.assertEqual({outcome for name, outcome in world.outcomes().items()} & {"waiting_sidecar", "waiting_export", "listed"}, set())
        twin = world.whole_export(build, parts=3)
        self.assertEqual([row[:3] for row in world.decisions()], [row[:3] for row in twin.decisions()])
        self.assertEqual(len(world.google_only()), len(twin.google_only()))


class UnreadableSidecarTests(unittest.TestCase):
    def build(self, builder):
        broken = builder.photo("IMG_9101.JPG", part=0, sidecar_part=1)
        broken.sidecar_data = b"{not json at all"
        replaced = builder.photo("IMG_9102.JPG", part=1)
        replaced.sidecar_data = b'{"title": "x\xff", "photoTakenTime": {"timestamp": "1500000000"}}'
        builder.photo("IMG_9103.JPG", part=0, sidecar_part=1)
        builder.photo("IMG_9104.JPG", part=1)

    def counts(self, world):
        row = world.opened.execute(select(ImportRun)).scalar_one()
        return row.sidecars_unreadable, row.sidecars_repaired

    def test_unreadable_and_repaired_sidecars_are_counted_as_whole_export_ingest_counts_them_and_named_nowhere(self):
        world = FinishWorld(self)
        export = world.create(self.build, parts=2)
        world.ingest_all(export, (1, 2))
        with no_leaks(self, "IMG_9101.JPG", "IMG_9102.JPG"):
            world.finish(export)
        twin = world.whole_export(lambda each: self.build, parts=2)
        self.assertEqual(self.counts(world), self.counts(twin))
        self.assertEqual(self.counts(world), (1, 1))
        done = world.import_done()
        self.assertIn(", 1 sidecar could not be read, so imported without Google metadata, 1 sidecar read with characters replaced", done)
        self.assertNotIn("IMG_91", done)
        self.assertEqual(world.run_row()[:14], twin.run_row()[:14])


class CrashTests(unittest.TestCase):
    def ready(self):
        world = FinishWorld(self)
        export = world.create(small_scenario(world), parts=2)
        world.ingest_all(export, (1, 2))
        return world, export

    def uninterrupted(self):
        world, export = self.ready()
        commits = [0]
        with mock.patch.object(finish_export, "COMMIT_HOOK", lambda: commits.__setitem__(0, commits[0] + 1)):
            world.finish(export)
        return commits[0], world.comparable(), world.run_row(), world.import_done()

    def test_a_crash_after_any_commit_resumes_to_the_same_result_and_writes_no_file_twice(self):
        commits, expected, counts, done = self.uninterrupted()
        self.assertGreater(commits, 5)
        for target in range(1, commits + 1):
            with self.subTest(commit=target):
                world, export = self.ready()
                writes = []
                with mock.patch.object(finish_export, "COMMIT_HOOK", crashing_at(target)), recording_writes(writes):
                    with self.assertRaises(Crash):
                        world.finish(export)
                world.opened.rollback()
                self.assertNotEqual(world.opened.get(Export, export.id).status, "imported")
                resumed = []
                with recording_writes(resumed):
                    world.finish(export)
                self.assertEqual(set(writes) & set(resumed), set())
                self.assertEqual(world.comparable(), expected)
                self.assertEqual(world.run_row(), counts)
                self.assertEqual(world.import_done(), done)
                self.assertEqual(world.count(ImportRun), 1)
                self.assertEqual(world.sent(SUMMARY), 1)

    def test_finishing_twice_changes_nothing(self):
        world, export = self.ready()
        world.finish(export)
        before = (world.comparable(), world.run_row(), world.count(Event), world.count(Alert))
        with self.assertRaises(run.AlreadyImported):
            world.finish(export)
        self.assertEqual(finish_export.finish_ready(world.opened, world.settings, NOW + 5, world.tools), [])
        self.assertEqual((world.comparable(), world.run_row(), world.count(Event), world.count(Alert)), before)
        self.assertEqual(world.count(ImportRun), 1)


def failing_place(name):
    real = place.place

    def broken(opened, photos, export, entries, *args, **kwargs):
        if any(entry.named is not None and entry.named.name == name for entry in entries):
            raise place.PlaceFailed("both names are taken by other files")
        return real(opened, photos, export, entries, *args, **kwargs)

    return mock.patch.object(place, "place", side_effect=broken)


class PerPartRetryTests(unittest.TestCase):
    def setUp(self):
        self.world = FinishWorld(self)
        self.export = self.world.create(self.world.scenario)

    def imported_with_a_failed_item(self, name="IMG_2001.JPG"):
        with failing_place(name):
            self.world.ingest_all(self.export)
            self.world.finish(self.export)
        self.world.opened.expire_all()
        self.assertEqual((self.world.opened.get(Export, self.export.id).status, self.world.run_row()[8]), ("imported", 1))
        self.assertEqual(self.world.outcomes()[f"Photos from 2019/{name}"], "failed")

    def request(self, at=NOW + DAY):
        found = run.request_import(self.world.opened, self.export.id, self.world.photos, at)
        self.world.opened.commit()
        return found

    def finish_ready(self, at=NOW + DAY + 5):
        return finish_export.finish_ready(self.world.opened, self.world.settings, at, self.world.tools)

    def twin(self):
        return self.world.whole_export(lambda twin: twin.scenario)

    def test_a_failed_item_is_held_for_the_export_and_finishing_again_imports_it_as_whole_export_ingest_does(self):
        self.imported_with_a_failed_item()
        found = self.request()
        self.assertEqual((found.status, found.stage), ("importing", "unpacked"))
        self.assertEqual(self.world.outcomes()["Photos from 2019/IMG_2001.JPG"], "waiting_export")
        self.assertEqual((self.world.part(self.export, 1).failed_items, self.world.part(self.export, 1).held), (0, 1))
        self.assertEqual(self.world.count(ImportRun), 2)
        self.assertEqual(run.latest_run(self.world.opened, self.export.id).status, "queued")
        self.assertEqual(run.waiting_exports(self.world.opened), [])
        self.assertEqual(self.finish_ready(), [self.export.id])
        self.world.opened.expire_all()
        self.assertEqual((self.world.opened.get(Export, self.export.id).status, self.world.opened.get(Export, self.export.id).stage), ("imported", "done"))
        self.assertEqual(self.world.outcomes()["Photos from 2019/IMG_2001.JPG"], "placed")
        self.assertEqual((self.world.run_row()[0], self.world.run_row()[8]), ("done", 0))
        self.assertEqual([self.world.part(self.export, n).failed_items for n in (1, 2, 3)], [0, 0, 0])
        self.assertEqual(self.world.comparable(), self.twin().comparable())
        with self.assertRaises(run.AlreadyImported):
            self.request(NOW + 2 * DAY)

    def test_an_item_that_could_not_be_measured_is_retried(self):
        real = metadata.candidate

        def candidate(item, *args, **kwargs):
            if item.path.name == "IMG_2003.JPG":
                raise ValueError("unreadable")
            return real(item, *args, **kwargs)

        with mock.patch.object(metadata, "candidate", side_effect=candidate):
            self.world.ingest_all(self.export)
            self.world.finish(self.export)
        self.assertEqual(self.world.outcomes()["Photos from 2019/IMG_2003.JPG"], "failed")
        self.request()
        self.assertEqual(self.finish_ready(), [self.export.id])
        self.assertEqual(self.world.outcomes()["Photos from 2019/IMG_2003.JPG"], "placed")
        self.assertEqual(self.world.comparable(), self.twin().comparable())

    def test_a_late_sidecar_fill_that_failed_is_filled_again_without_deciding_again(self):
        self.world.ingest_all(self.export, (1, 2))
        decided = self.world.entry("Photos from 2019/IMG_1002.JPG")
        self.assertTrue(decided.metadata_pending)
        with failing_place("IMG_1002.JPG"):
            self.world.ingest(self.export, 3)
        self.world.finish(self.export)
        self.assertEqual(self.world.outcomes()["Photos from 2019/IMG_1002.JPG"], "failed")
        before = self.world.decisions()
        self.request()
        self.assertEqual((self.world.entry("Photos from 2019/IMG_1002.JPG").outcome, self.world.entry("Photos from 2019/IMG_1002.JPG").metadata_pending),
                         ("skipped", True))
        self.assertEqual(self.finish_ready(), [self.export.id])
        self.assertEqual(self.world.outcomes()["Photos from 2019/IMG_1002.JPG"], "skipped")
        self.assertFalse(self.world.entry("Photos from 2019/IMG_1002.JPG").metadata_pending)
        self.assertEqual(self.world.decisions(), before)
        self.assertEqual(self.world.comparable(), self.twin().comparable())

    def test_nothing_is_freed_while_the_retry_waits(self):
        self.imported_with_a_failed_item()
        self.request()
        report = part_free.free_ready(self.world.opened, self.world.settings, NOW + DAY + 1)
        self.assertEqual((report.parts, report.held), (0, 0))
        self.assertEqual([self.world.part(self.export, n).free_state for n in (1, 2, 3)], [None, None, None])

    def assert_refused_and_unchanged(self, words):
        before = (self.world.outcomes(), self.world.count(ImportRun), self.world.opened.get(Export, self.export.id).status)
        with self.assertRaises(run.NotReady) as raised:
            self.request()
        self.world.opened.rollback()
        self.assertIn(words, str(raised.exception))
        self.world.opened.expire_all()
        self.assertEqual((self.world.outcomes(), self.world.count(ImportRun), self.world.opened.get(Export, self.export.id).status), before)

    def test_a_failed_item_whose_part_was_freed_cannot_be_retried_from_it(self):
        self.imported_with_a_failed_item()
        self.world.part(self.export, 1).free_state = "freed"
        self.world.opened.commit()
        self.assert_refused_and_unchanged("part 1 of this export was deleted to free space, so its failed items cannot be retried from it")

    def test_a_failed_item_whose_part_file_is_gone_cannot_be_retried(self):
        self.imported_with_a_failed_item()
        (Path(self.export.inbox_dir) / self.world.part(self.export, 1).filename).unlink()
        self.assert_refused_and_unchanged("part 1 of this export is not in the inbox, so its failed items cannot be retried")

    def test_a_failed_item_with_no_inbox_to_read_cannot_be_retried(self):
        self.imported_with_a_failed_item()
        with self.assertRaises(run.NotReady):
            run.request_import(self.world.opened, self.export.id, None, NOW + DAY)

    def test_a_failed_item_kept_in_the_held_store_is_read_from_there_once_its_part_is_freed(self):
        self.imported_with_a_failed_item()
        entry = self.world.entry("Photos from 2019/IMG_2001.JPG")
        person = run.owner_person(self.world.opened, self.world.opened.get(Export, self.export.id))
        zipped = Path(self.export.inbox_dir) / self.world.part(self.export, 1).filename
        with zipfile.ZipFile(zipped) as archive:
            data = archive.read(entry.name)
        written = paths.write_held(self.world.photos, person.slug, f"export-{self.export.id}", part_reader.scratch_name(entry), [data], entry.sha1)
        entry.held_path = written.relative_to(self.world.photos.root).as_posix()
        self.world.part(self.export, 1).free_state = "freed"
        zipped.rename(zipped.with_suffix(".gone"))
        self.world.opened.commit()
        self.request()
        self.assertEqual(self.finish_ready(), [self.export.id])
        self.assertEqual(self.world.outcomes()["Photos from 2019/IMG_2001.JPG"], "placed")

    def test_an_export_whose_failed_items_have_no_bytes_recorded_has_nothing_to_retry(self):
        self.imported_with_a_failed_item()
        entry = self.world.entry("Photos from 2019/IMG_2001.JPG")
        entry.sha1 = None
        self.world.opened.commit()
        self.assert_refused_and_unchanged("none of this export's failed items can be retried")

    def test_no_entry_name_reaches_the_log_or_the_database_events(self):
        self.imported_with_a_failed_item()
        with no_leaks(self, "IMG_2001.JPG"):
            self.request()
            self.finish_ready()
        events = self.world.opened.execute(select(Event)).scalars().all()
        self.assertTrue(events)
        self.assertEqual([row.id for row in events if "IMG_2001" in f"{row.subject} {row.detail}"], [])


class NeverCompletesTests(unittest.TestCase):
    def setUp(self):
        self.world = FinishWorld(self)
        self.export = self.world.create(self.world.scenario)
        self.world.ingest_all(self.export, (1, 2))

    def zips(self):
        return sorted((path.name, path.stat().st_size) for path in self.world.photos.inbox("jo").rglob("*.zip"))

    def test_a_part_that_never_downloads_decides_and_deletes_nothing_and_the_export_is_not_finished(self):
        last = self.world.part(self.export, 3)
        last.status, last.ingest_state = "failed", "not_queued"
        self.world.opened.commit()
        before = (self.world.state(), self.zips())
        with self.assertRaises(run.NotReady):
            self.world.finish(self.export)
        self.assertEqual(finish_export.finish_ready(self.world.opened, self.world.settings, NOW), [])
        self.assertEqual((self.world.state(), self.zips()), before)
        held = {name for name, outcome in self.world.outcomes().items() if outcome in ("waiting_sidecar", "waiting_export")}
        self.assertTrue(held)
        self.assertEqual(self.world.opened.get(Export, self.export.id).status, "downloading")
        self.assertEqual(self.world.count(ImportRun), 0)
        self.assertEqual(len(self.zips()), 3)

    def test_an_export_that_gave_up_downloading_keeps_its_held_items_and_zips_and_raises_one_alert_with_counts_only(self):
        last = self.world.part(self.export, 3)
        last.status, last.ingest_state = "failed", "not_queued"
        exports.give_up_downloading(self.world.opened, self.world.opened.get(Export, self.export.id), NOW)
        self.world.opened.commit()
        before = (self.world.comparable(), self.zips())
        with no_leaks(self, "IMG_1003.JPG", "IMG_2003.JPG"):
            self.assertEqual(finish_export.finish_ready(self.world.opened, self.world.settings, NOW + 1, self.world.tools), [])
            self.assertEqual(finish_export.finish_ready(self.world.opened, self.world.settings, NOW + 2, self.world.tools), [])
        found = list(self.world.opened.execute(select(Alert).where(Alert.key == f"export:unfinished:{self.export.id}")).scalars())
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0].kind, found[0].account_id), ("failure", self.export.account_id))
        self.assertIn("did not download in full", found[0].message)
        self.assertNotIn("IMG_", found[0].message)
        self.assertEqual(self.world.sent("Darkroom needs a look"), 1)
        self.assertEqual((self.world.comparable(), self.zips()), before)
        self.assertEqual(self.world.opened.get(Export, self.export.id).status, "failed")

    def test_the_alert_check_takes_the_write_lock_before_it_reads_any_alert_and_again_for_each_export(self):
        last = self.world.part(self.export, 3)
        last.status, last.ingest_state = "failed", "not_queued"
        exports.give_up_downloading(self.world.opened, self.world.opened.get(Export, self.export.id), NOW)
        self.world.opened.commit()
        seen = []
        real = finish_export.db.write_lock

        def locking(opened):
            seen.append("lock")
            real(opened)

        def statement(connection, cursor, text, *rest):
            if "FROM alerts" in text:
                seen.append("read")

        event.listen(self.world.engine, "before_cursor_execute", statement)
        try:
            with mock.patch.object(finish_export.db, "write_lock", side_effect=locking):
                finish_export.alert_unfinished(self.world.opened, self.world.settings, NOW + 1, self.world.tools)
        finally:
            event.remove(self.world.engine, "before_cursor_execute", statement)
        self.assertIn("read", seen)
        self.assertEqual(seen[0], "lock")
        self.assertLess(seen.index("lock"), seen.index("read"))

    def test_an_export_another_worker_already_alerted_about_is_not_alerted_again(self):
        last = self.world.part(self.export, 3)
        last.status, last.ingest_state = "failed", "not_queued"
        exports.give_up_downloading(self.world.opened, self.world.opened.get(Export, self.export.id), NOW)
        self.world.opened.commit()
        real = finish_export.db.write_lock

        def rival(opened):
            real(opened)
            if not self.world.opened.execute(select(Alert.id).where(Alert.key == f"export:unfinished:{self.export.id}")).first():
                other = Alert(key=f"export:unfinished:{self.export.id}", kind="failure", subject="x", message="x", opened=NOW, closed=NOW)
                self.world.opened.add(other)
                self.world.opened.commit()

        with mock.patch.object(finish_export.db, "write_lock", side_effect=rival):
            finish_export.alert_unfinished(self.world.opened, self.world.settings, NOW + 1, self.world.tools)
        self.assertEqual(self.world.count(Alert), 1)
        self.assertEqual(self.world.sent("Darkroom needs a look"), 0)

    def test_an_export_nothing_is_held_for_raises_no_alert(self):
        other = FinishWorld(self)
        export = other.create(lambda builder: builder.photo("IMG_3001.JPG", part=0), parts=2)
        other.ingest_all(export, (1, 2))
        other.part(export, 2).status = "failed"
        exports.give_up_downloading(other.opened, other.opened.get(Export, export.id), NOW)
        other.opened.commit()
        finish_export.finish_ready(other.opened, other.settings, NOW + 1, other.tools)
        self.assertEqual(other.count(Alert), 0)


class TickTests(unittest.TestCase):
    def test_the_tick_finishes_an_export_whose_parts_are_all_ingested(self):
        world = FinishWorld(self)
        export = world.create(small_scenario(world), parts=2)
        world.ingest_all(export, (1, 2))
        self.assertEqual(finish_export.finish_ready(world.opened, world.settings, NOW, world.tools), [export.id])
        self.assertEqual(finish_export.finish_ready(world.opened, world.settings, NOW, world.tools), [])
        self.assertEqual(world.opened.get(Export, export.id).status, "imported")

    def test_the_whole_export_worker_leaves_an_export_with_per_part_rows_alone(self):
        world = FinishWorld(self)
        export = world.create(small_scenario(world), parts=2)
        world.ingest_all(export, (1, 2))
        found = world.opened.get(Export, export.id)
        found.status, found.stage = "downloaded", "queued"
        world.opened.commit()
        self.assertEqual(run.waiting_exports(world.opened), [])
        self.assertEqual(run.run_waiting(world.opened, world.settings, NOW, world.tools), [])
        self.assertEqual(world.count(ImportRun), 0)
        with mock.patch.object(finish_export, "COMMIT_HOOK", crashing_at(3)):
            with self.assertRaises(Crash):
                world.finish(export)
        world.opened.rollback()
        self.assertEqual(world.opened.get(Export, export.id).status, "downloaded")
        self.assertEqual(run.waiting_exports(world.opened), [])
        self.assertEqual(run.run_waiting(world.opened, world.settings, NOW + 1, world.tools), [])
        world.finish(export)
        self.assertEqual(world.opened.get(Export, export.id).status, "imported")

    def test_a_whole_export_without_per_part_rows_is_still_picked_up(self):
        world = FinishWorld(self)
        export = world.create(small_scenario(world), parts=2, status="downloaded", queue=False)
        found = world.opened.get(Export, export.id)
        found.stage = "queued"
        world.opened.commit()
        self.assertEqual(run.waiting_exports(world.opened), [export.id])

    def test_a_busy_account_is_skipped_and_the_export_stays_unfinished(self):
        world = FinishWorld(self)
        export = world.create(small_scenario(world), parts=2)
        world.ingest_all(export, (1, 2))
        with run.account_lock(world.settings, export.account_id):
            self.assertEqual(finish_export.finish_ready(world.opened, world.settings, NOW, world.tools), [])
            with self.assertRaises(run.Busy):
                world.finish(export)
        self.assertEqual(world.count(ImportRun), 0)

    def test_an_export_waits_for_an_earlier_export_of_the_account(self):
        world = FinishWorld(self)
        earlier = world.create(lambda builder: builder.photo("IMG_3001.JPG", part=0), parts=1, google_id="google-456", created=NOW - 2 * DAY)
        later = world.create(small_scenario(world), parts=2)
        world.ingest_all(later, (1, 2))
        earlier_export = world.opened.get(Export, earlier.id)
        self.assertEqual(finish_export.ready(world.opened, world.opened.get(Export, later.id)), False)
        world.ingest_all(earlier, (1,))
        world.finish(earlier)
        self.assertEqual(earlier_export.status, "imported")
        self.assertEqual(finish_export.ready(world.opened, world.opened.get(Export, later.id)), True)


if __name__ == "__main__":
    unittest.main()
