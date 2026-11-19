import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import MetaData, select, update

import run
from darkroom import config
from darkroom.domain import events, index, video_times
from darkroom.domain.ingest import run as ingest_run
from darkroom.providers import exiftool
from darkroom.storage import db, locks
from darkroom.storage.models import DedupeDecision, Event, Export, GoogleAccount, ImportRun, MediaItem, Person, ReviewItem
from darkroom.storage.paths import Photos
from tests.fixtures import media as fixtures
from tests.import_world import tree
from tests.privacy import no_leaks
from tests.shared import add_person, own_database, settings_from

NOW = 1_800_000_000.0
NEW_YORK = (40.7, -74.0)
UTC_CLOCK = 1562283000.0
TIMES = ("captured_utc", "captured_local", "offset_minutes", "time_source")
SHIFTED = {"captured_utc": UTC_CLOCK + 4 * 3600, "captured_local": "2019-07-04T23:30:00", "offset_minutes": -240, "time_source": "gps_lookup"}
SHIFTED_WITHOUT_GPS = {"captured_utc": None, "captured_local": "2019-07-04T23:30:00", "offset_minutes": None, "time_source": "unknown"}
CORRECTED = {"captured_utc": UTC_CLOCK, "captured_local": "2019-07-04T19:30:00", "offset_minutes": -240, "time_source": "quicktime_utc"}
CORRECTED_WITHOUT_GPS = {"captured_utc": UTC_CLOCK, "captured_local": None, "offset_minutes": None, "time_source": "quicktime_utc"}


def snapshot(engine, *leaving_out):
    meta = MetaData()
    found = {}
    with engine.connect() as connection:
        meta.reflect(connection)
        for table in meta.sorted_tables:
            if table.name in leaving_out:
                continue
            rows = [dict(row) for row in connection.execute(select(table)).mappings()]
            found[table.name] = sorted(rows, key=repr)
    return found


def tagged_reader(tags):
    def read(paths):
        return {path: {"SourceFile": str(path), "File:MIMEType": "video/mp4", **tags.get(path.name, {})} for path in paths}

    return read


class VideoTimesCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name).resolve()
        self.root = self.base / "photos"
        self.root.mkdir()
        self.photos = Photos(self.root)
        self.engine = own_database(self)
        self.settings = settings_from(DARKROOM_PHOTOS_ROOT=str(self.root), DARKROOM_DATA_DIR=str(self.base / "data"))
        with db.session(self.engine) as opened:
            self.person_id = add_person(opened, "jo")
            account = GoogleAccount(email="family@x.test", default_person_id=self.person_id, created=1.0)
            opened.add(account)
            opened.flush()
            self.account_id = account.id
        self.originals = self.photos.ensure_person_tree("jo")[0]

    def index(self, read=exiftool.read):
        with db.session(self.engine) as opened:
            index.index_person(opened, self.photos, opened.get(Person, self.person_id), NOW - 100, read)

    def set_times(self, name, values):
        with db.session(self.engine) as opened:
            opened.execute(update(MediaItem).where(MediaItem.path == f"jo/originals/{name}").values(**values))

    def times(self, name):
        with db.session(self.engine) as opened:
            item = opened.execute(select(MediaItem).where(MediaItem.path == f"jo/originals/{name}")).scalar_one()
            return {column: getattr(item, column) for column in TIMES}

    def reread(self, apply=True, read=exiftool.read, at=NOW):
        with db.session(self.engine) as opened:
            people = list(opened.execute(select(Person).order_by(Person.id)).scalars())
            return video_times.reread(opened, self.settings, people, at, apply=apply, read=read)

    def counts(self, report):
        return {"read": report.read, "changed": report.changed, "unchanged": report.unchanged, "no_quicktime_date": report.no_quicktime_date,
                "unreadable": report.unreadable, "changed_since_indexed": report.changed_since_indexed}

    def recorded(self):
        with db.session(self.engine) as opened:
            return [entry.detail for entry in events.recent(opened, type=video_times.EVENT)]


class ShiftedVideoTests(VideoTimesCase):
    def setUp(self):
        super().setUp()
        fixtures.make_video(self.originals / "v.mp4", quicktime_utc="2019:07:04 23:30:00", gps=NEW_YORK)
        fixtures.make_video(self.originals / "w.mp4", quicktime_utc="2019:07:04 23:30:00")
        fixtures.make_video(self.originals / "apple.mov", creation="2021:05:06 07:08:09-04:00")
        fixtures.make_jpeg(self.originals / "a.jpg", taken="2019:07:04 23:30:00", gps=NEW_YORK)
        self.index()
        self.apple = self.times("apple.mov")
        self.photo = self.times("a.jpg")
        self.set_times("v.mp4", SHIFTED)
        self.set_times("w.mp4", SHIFTED_WITHOUT_GPS)

    def test_shifted_rows_are_corrected_and_only_their_time_columns_change(self):
        before, files = snapshot(self.engine), tree(self.root)
        report = self.reread()
        self.assertEqual(self.counts(report), {"read": 3, "changed": 2, "unchanged": 0, "no_quicktime_date": 1, "unreadable": 0, "changed_since_indexed": 0})
        self.assertEqual(self.times("v.mp4"), CORRECTED)
        self.assertEqual(self.times("w.mp4"), CORRECTED_WITHOUT_GPS)
        self.assertEqual(self.times("apple.mov"), self.apple)
        self.assertEqual(self.apple["time_source"], "exif_offset")
        self.assertEqual(self.times("a.jpg"), self.photo)
        after = snapshot(self.engine)
        self.assertEqual(len(after.pop("events")), len(before.pop("events")) + 1)
        changed = {"jo/originals/v.mp4", "jo/originals/w.mp4"}

        def without_times(rows):
            return [{column: value for column, value in row.items() if not (row["path"] in changed and column in TIMES)} for row in rows]

        self.assertEqual(without_times(after.pop("media_items")), without_times(before.pop("media_items")))
        self.assertEqual(after, before)
        self.assertEqual(tree(self.root), files)

    def test_the_dry_run_counts_and_writes_nothing(self):
        before, files = snapshot(self.engine), tree(self.root)
        report = self.reread(apply=False)
        self.assertEqual(self.counts(report), {"read": 3, "changed": 2, "unchanged": 0, "no_quicktime_date": 1, "unreadable": 0, "changed_since_indexed": 0})
        self.assertEqual(snapshot(self.engine), before)
        self.assertEqual(tree(self.root), files)
        self.assertEqual(self.times("v.mp4"), SHIFTED)

    def test_a_second_run_changes_nothing_and_reports_them_unchanged(self):
        self.reread()
        before = snapshot(self.engine, "events")
        report = self.reread(at=NOW + 10)
        self.assertEqual(self.counts(report), {"read": 3, "changed": 0, "unchanged": 2, "no_quicktime_date": 1, "unreadable": 0, "changed_since_indexed": 0})
        self.assertEqual(snapshot(self.engine, "events"), before)
        self.assertEqual(self.recorded(), ["0 corrected, 2 unchanged, 1 without a QuickTime date, 0 unreadable, 0 changed since indexed",
                                           "2 corrected, 0 unchanged, 1 without a QuickTime date, 0 unreadable, 0 changed since indexed"])

    def test_the_apply_records_one_event_with_counts_only(self):
        self.reread()
        with db.session(self.engine) as opened:
            entry = opened.execute(select(Event).where(Event.type == video_times.EVENT)).scalar_one()
            self.assertEqual((entry.subject, entry.detail, entry.time),
                             (video_times.SUBJECT, "2 corrected, 0 unchanged, 1 without a QuickTime date, 0 unreadable, 0 changed since indexed", NOW))

    def test_recorded_decisions_and_review_items_stay_as_they_were(self):
        with db.session(self.engine) as opened:
            item = opened.execute(select(MediaItem).where(MediaItem.path == "jo/originals/v.mp4")).scalar_one()
            export = Export(account_id=self.account_id, kind="initial", status="imported", stage="done")
            opened.add(export)
            opened.flush()
            decision = DedupeDecision(export_id=export.id, account_id=self.account_id, person_id=self.person_id, source_path="Takeout/v.mp4", tier=4,
                                      reason="perceptual distance 3, 1 s apart", decided_by="auto", decided_at=1.0, matched_item_id=item.id)
            opened.add(decision)
            opened.flush()
            opened.add(ReviewItem(decision_id=decision.id, status="pending"))
        decisions, reviews = snapshot(self.engine)["dedupe_decisions"], snapshot(self.engine)["review_items"]
        self.assertEqual(self.reread().changed, 2)
        self.assertEqual(snapshot(self.engine)["dedupe_decisions"], decisions)
        self.assertEqual(snapshot(self.engine)["review_items"], reviews)

    def test_an_unreadable_or_changed_file_is_counted_and_left_alone(self):
        (self.originals / "w.mp4").unlink()
        fixtures.make_video(self.originals / "v.mp4", quicktime_utc="2020:01:01 00:00:00", gps=NEW_YORK)
        os.utime(self.originals / "v.mp4", (1.0, 1.0))
        with db.session(self.engine) as opened:
            opened.execute(update(MediaItem).where(MediaItem.path == "jo/originals/v.mp4").values(mtime=1.0))
        report = self.reread()
        self.assertEqual(self.counts(report), {"read": 3, "changed": 0, "unchanged": 0, "no_quicktime_date": 1, "unreadable": 1, "changed_since_indexed": 1})
        self.assertEqual(self.times("w.mp4"), SHIFTED_WITHOUT_GPS)
        self.assertEqual(self.times("v.mp4"), SHIFTED)

    def test_a_file_exiftool_cannot_read_is_counted_unreadable(self):
        def refuses(paths):
            raise exiftool.Unreadable("made-up failure")

        report = self.reread(read=refuses)
        self.assertEqual((report.read, report.unreadable, report.changed), (3, 3, 0))
        self.assertEqual(self.times("v.mp4"), SHIFTED)

    def test_a_row_changed_while_the_files_were_read_is_left_to_the_other_writer(self):
        def meanwhile(paths):
            with db.session(self.engine) as opened:
                opened.execute(update(MediaItem).where(MediaItem.path == "jo/originals/v.mp4").values(captured_local="2019-07-04T23:30:01"))
            return exiftool.read(paths)

        report = self.reread(read=meanwhile)
        self.assertEqual((report.changed, report.changed_since_indexed), (1, 1))
        self.assertEqual(self.times("v.mp4"), {**SHIFTED, "captured_local": "2019-07-04T23:30:01"})
        self.assertEqual(self.times("w.mp4"), CORRECTED_WITHOUT_GPS)

    def test_a_run_stopped_after_a_batch_keeps_that_batch_and_a_second_run_finishes(self):
        calls = []

        def stops(paths):
            calls.append(paths)
            if len(calls) == 3:
                raise KeyboardInterrupt
            return exiftool.read(paths)

        with mock.patch.object(video_times, "BATCH", 1), self.assertRaises(KeyboardInterrupt):
            self.reread(read=stops)
        self.assertEqual(sum(self.times(name) == done for name, done in (("v.mp4", CORRECTED), ("w.mp4", CORRECTED_WITHOUT_GPS))), 1)
        self.assertEqual(self.recorded(), [])
        with mock.patch.object(video_times, "BATCH", 1):
            report = self.reread()
        self.assertEqual((report.changed, report.unchanged), (1, 1))
        self.assertEqual((self.times("v.mp4"), self.times("w.mp4")), (CORRECTED, CORRECTED_WITHOUT_GPS))

    def test_only_the_named_people_are_read(self):
        with db.session(self.engine) as opened:
            add_person(opened, "sam")
        sam = self.photos.ensure_person_tree("sam")[0]
        fixtures.make_video(sam / "s.mp4", quicktime_utc="2019:07:04 23:30:00", gps=NEW_YORK)
        with db.session(self.engine) as opened:
            index.index_person(opened, self.photos, opened.execute(select(Person).where(Person.slug == "sam")).scalar_one(), NOW - 100)
            opened.execute(update(MediaItem).where(MediaItem.path == "sam/originals/s.mp4").values(**SHIFTED))
        with db.session(self.engine) as opened:
            report = video_times.reread(opened, self.settings, [opened.get(Person, self.person_id)], NOW, apply=True)
            sam_item = opened.execute(select(MediaItem).where(MediaItem.path == "sam/originals/s.mp4")).scalar_one()
            self.assertEqual({column: getattr(sam_item, column) for column in TIMES}, SHIFTED)
        self.assertEqual(report.changed, 2)


class MadeUpTagTests(VideoTimesCase):
    def put(self, name, tags, values):
        (self.originals / name).write_bytes(name.encode() * 64)
        self.tags[name] = tags
        self.index(read=tagged_reader(self.tags))
        self.set_times(name, values)

    def setUp(self):
        super().setUp()
        self.tags = {}

    def test_a_quicktime_clock_with_a_separate_offset_tag_is_corrected_from_exif_offset(self):
        self.put("x.mp4", {"QuickTime:CreateDate": "2019:07:04 23:30:00", "ExifIFD:OffsetTime": "-04:00"},
                 {"captured_utc": UTC_CLOCK + 4 * 3600, "captured_local": "2019-07-04T23:30:00", "offset_minutes": -240, "time_source": "exif_offset"})
        report = self.reread(read=tagged_reader(self.tags))
        self.assertEqual(report.changed, 1)
        self.assertEqual(self.times("x.mp4"), CORRECTED_WITHOUT_GPS)

    def test_an_apple_video_with_its_own_offset_is_never_written(self):
        apple = {"QuickTime:CreateDate": "2019:07:04 23:30:00", "Keys:CreationDate": "2019:07:04 19:30:00-04:00"}
        self.put("apple.mov", apple, {"captured_utc": UTC_CLOCK, "captured_local": "2019-07-04T19:30:00", "offset_minutes": -240, "time_source": "exif_offset"})
        before = snapshot(self.engine)
        report = self.reread(read=tagged_reader(self.tags))
        self.assertEqual((report.changed, report.no_quicktime_date), (0, 1))
        self.assertEqual(snapshot(self.engine, "events"), {name: rows for name, rows in before.items() if name != "events"})

    def test_a_video_without_any_date_is_not_written_even_when_its_stored_time_differs(self):
        self.put("none.mp4", {}, {"captured_utc": None, "captured_local": "2019-07-04T23:30:00", "offset_minutes": None, "time_source": "unknown"})
        report = self.reread(read=tagged_reader(self.tags))
        self.assertEqual((report.changed, report.no_quicktime_date), (0, 1))
        self.assertEqual(self.times("none.mp4")["captured_local"], "2019-07-04T23:30:00")

    def test_a_file_exiftool_finds_no_media_in_is_counted_unreadable(self):
        self.put("odd.mp4", {"QuickTime:CreateDate": "2019:07:04 23:30:00"}, SHIFTED_WITHOUT_GPS)
        report = self.reread(read=lambda paths: {path: {"SourceFile": str(path), "File:MIMEType": "text/plain"} for path in paths})
        self.assertEqual((report.unreadable, report.changed), (1, 0))
        report = self.reread(read=lambda paths: {path: {"SourceFile": str(path), "Error": "made-up"} for path in paths})
        self.assertEqual((report.unreadable, report.changed), (1, 0))
        self.assertEqual(self.times("odd.mp4"), SHIFTED_WITHOUT_GPS)

    def test_a_row_whose_contents_are_unknown_is_not_read(self):
        self.put("set-aside.mp4", {"QuickTime:CreateDate": "2019:07:04 23:30:00"}, {**SHIFTED_WITHOUT_GPS, "sha1": ""})
        report = self.reread(read=lambda paths: self.fail("a row with unknown contents was read"))
        self.assertEqual(report.read, 0)


class BusyTests(VideoTimesCase):
    def setUp(self):
        super().setUp()
        fixtures.make_video(self.originals / "v.mp4", quicktime_utc="2019:07:04 23:30:00", gps=NEW_YORK)
        self.index()
        self.set_times("v.mp4", SHIFTED)

    def refused(self, apply=True):
        before = snapshot(self.engine)
        with self.assertRaises(video_times.Busy) as raised:
            self.reread(apply=apply, read=lambda paths: self.fail("files were read while an import runs"))
        self.assertEqual(str(raised.exception), video_times.BUSY)
        self.assertEqual(snapshot(self.engine), before)

    def test_refuses_while_the_import_lock_of_an_account_serving_the_person_is_held(self):
        with locks.exclusive(ingest_run.lock_file(self.settings, self.account_id)):
            self.refused()
            self.refused(apply=False)
        self.assertEqual(self.reread().changed, 1)

    def test_refuses_while_a_legacy_import_or_an_immich_reconcile_runs_for_the_person(self):
        for name in ("legacy-import-jo.lock", "immich-reconcile-jo.lock"):
            with self.subTest(name), locks.exclusive(self.settings.data_dir / "locks" / name):
                self.refused()

    def test_refuses_while_a_takeout_import_is_in_flight_for_the_person(self):
        cases = [{"status": "importing", "stage": "deduped"}, {"status": "unpacking", "stage": "unpacked"}, {"status": "downloaded", "stage": "queued"}]
        for values in cases:
            with self.subTest(**values):
                with db.session(self.engine) as opened:
                    export = Export(account_id=self.account_id, kind="initial", **values)
                    opened.add(export)
                try:
                    self.refused()
                finally:
                    self.forget_exports()

    def test_refuses_while_an_import_run_is_queued_for_an_imported_export(self):
        with db.session(self.engine) as opened:
            export = Export(account_id=self.account_id, kind="initial", status="imported", stage="unpacked")
            opened.add(export)
            opened.flush()
            opened.add(ImportRun(export_id=export.id, started=1.0, status="queued", stage="unpacked"))
        self.refused()

    def test_an_export_owned_by_the_person_on_another_account_counts(self):
        with db.session(self.engine) as opened:
            other = add_person(opened, "sam")
            account = GoogleAccount(email="other@x.test", default_person_id=other, created=1.0)
            opened.add(account)
            opened.flush()
            opened.add(Export(account_id=account.id, kind="initial", status="importing", stage="deduped", owner_person_id=self.person_id))
        self.refused()

    def test_an_imported_or_not_yet_queued_export_does_not_stop_it(self):
        with db.session(self.engine) as opened:
            opened.add(Export(account_id=self.account_id, kind="initial", status="imported", stage="done"))
            opened.add(Export(account_id=self.account_id, kind="initial", status="downloaded"))
            opened.add(Export(account_id=self.account_id, kind="initial", status="failed", stage="deduped"))
        self.assertEqual(self.reread().changed, 1)

    def test_an_import_started_while_it_runs_is_told_it_is_busy(self):
        def starts_an_import(paths):
            with self.assertRaises(ingest_run.Busy), ingest_run.account_lock(self.settings, self.account_id):
                pass
            return exiftool.read(paths)

        self.assertEqual(self.reread(read=starts_an_import).changed, 1)
        with ingest_run.account_lock(self.settings, self.account_id):
            pass

    def forget_exports(self):
        with db.session(self.engine) as opened:
            for export in opened.execute(select(Export)).scalars():
                opened.delete(export)


class CommandTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name).resolve()
        values = {"DARKROOM_PHOTOS_ROOT": str(self.base / "photos"), "DARKROOM_DATA_DIR": str(self.base / "data"), "DARKROOM_SECRET_KEY": "k",
                  "DARKROOM_PUBLIC_URL": "https://d.example"}
        patcher = mock.patch.dict(os.environ, values, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        with contextlib.redirect_stderr(io.StringIO()):
            run.migrate()
        self.engine = db.make_engine(db.database_url(config.load()))
        self.addCleanup(self.engine.dispose)
        (self.base / "photos").mkdir()
        self.photos = Photos(self.base / "photos")
        with db.session(self.engine) as opened:
            self.person_id = add_person(opened, "canaryslugperson", "Canary Personname")
            opened.add(GoogleAccount(email="canary-account@x.test", default_person_id=self.person_id, created=1.0))
        originals = self.photos.ensure_person_tree("canaryslugperson")[0]
        fixtures.make_video(originals / "canary-video-name.mp4", quicktime_utc="2019:07:04 23:30:00", gps=NEW_YORK)
        fixtures.make_video(originals / "canary-gone-name.mp4", quicktime_utc="2019:07:05 23:30:00")
        with db.session(self.engine) as opened:
            index.index_person(opened, self.photos, opened.get(Person, self.person_id), NOW - 100)
            opened.execute(update(MediaItem).where(MediaItem.path.like("%canary-video-name.mp4")).values(**SHIFTED))
        (originals / "canary-gone-name.mp4").unlink()

    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run.main(["reread-video-times", *args])
        return code, out.getvalue(), err.getvalue()

    def stored(self):
        with db.session(self.engine) as opened:
            item = opened.execute(select(MediaItem).where(MediaItem.path.like("%canary-video-name.mp4"))).scalar_one()
            return {column: getattr(item, column) for column in TIMES}

    def test_without_apply_it_is_a_dry_run_that_prints_counts(self):
        code, out, err = self.call()
        self.assertEqual(code, 0)
        self.assertEqual([json.loads(line)["event"] for line in err.splitlines()], ["video times reread"])
        self.assertEqual(out, video_times.DRY_RUN_HEADING + "\n" + "Indexed videos read again: 2. Would change: 1. Unchanged: 0. No QuickTime date: 0. "
                         "Unreadable file: 1. Changed since indexed: 0.\n" + video_times.DRY_RUN_NOTE + "\n")
        self.assertEqual(self.stored(), SHIFTED)

    def test_apply_corrects_and_prints_counts(self):
        code, out, _ = self.call("--apply")
        self.assertEqual(code, 0)
        self.assertEqual(out, "Indexed videos read again: 2. Corrected: 1. Unchanged: 0. No QuickTime date: 0. Unreadable file: 1. Changed since indexed: 0.\n")
        self.assertEqual(self.stored(), CORRECTED)

    def test_an_unknown_person_is_refused_without_echoing_it(self):
        code, out, err = self.call("--person", "nobody-canary-slug")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "reread-video-times: no such person\n")

    def test_refuses_with_a_fixed_sentence_while_an_import_is_in_flight(self):
        with db.session(self.engine) as opened:
            account = opened.execute(select(GoogleAccount)).scalar_one()
            opened.add(Export(account_id=account.id, kind="initial", status="importing", stage="deduped"))
        code, out, err = self.call("--apply")
        self.assertEqual((code, out, err), (1, "", f"reread-video-times: {video_times.BUSY}\n"))
        self.assertEqual(self.stored(), SHIFTED)

    def test_nothing_private_is_printed_logged_or_recorded(self):
        canaries = ("canaryslugperson", "Canary Personname", "canary-video-name", "canary-gone-name", "canary-account")
        with no_leaks(self, *canaries) as watched:
            self.call("--person", "canaryslugperson")
            self.call("--person", "canaryslugperson", "--apply")
        self.assertTrue(watched)
        with db.session(self.engine) as opened:
            details = [f"{entry.subject} {entry.detail}" for entry in opened.execute(select(Event)).scalars()]
        self.assertEqual(details, [f"{video_times.SUBJECT} 1 corrected, 0 unchanged, 0 without a QuickTime date, 1 unreadable, 0 changed since indexed"])


if __name__ == "__main__":
    unittest.main()
