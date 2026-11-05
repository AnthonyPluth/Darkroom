import contextlib
import dataclasses
import hashlib
import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from sqlalchemy import MetaData, select

import run
from darkroom import tls
from darkroom.dates import utc_now
from darkroom.domain import accounts, alerts, browser_jobs, demo, events, exports, renewal
from darkroom.providers import exiftool
from darkroom.server import auth
from darkroom.storage import db
from darkroom.storage.models import Alert, AuthSession, DedupeDecision, Device, Export, ExportPart, GoogleAccount, ImportRun, MediaItem, Person, Pixel, PixelPerson, ReviewItem
from darkroom.storage.paths import Photos
from tests.shared import fetch, settings_from, start_server

TODAY = date(2026, 6, 15)
NOON = demo.noon(TODAY)
DAY = 86_400.0


def no_network(*args, **kwargs):
    raise AssertionError("the demo seed reached for the network")


def seed_into(folder: Path, today: date = TODAY):
    engine = db.make_engine(f"sqlite:///{folder / 'demo.db'}")
    db.Base.metadata.create_all(engine)
    (folder / "photos").mkdir()
    with db.session(engine) as opened, mock.patch.object(tls, "urlopen", side_effect=no_network):
        summary = demo.seed(opened, Photos(folder / "photos"), settings_from(DARKROOM_DEMO="1"), today)
    return engine, summary


def table_rows(engine):
    meta = MetaData()
    found = {}
    with engine.connect() as connection:
        meta.reflect(connection)
        for table in meta.sorted_tables:
            if table.name == "alembic_version":
                continue
            columns = [column for column in table.columns if column.name != "id"]
            found[table.name] = sorted(tuple(row) for row in connection.execute(select(*columns)).all())
    return found


def file_digests(root: Path):
    return {str(path.relative_to(root)): hashlib.sha1(path.read_bytes()).hexdigest() for path in sorted(root.rglob("*")) if path.is_file()}


class SeededDemo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        folder = tempfile.TemporaryDirectory()
        cls.addClassCleanup(folder.cleanup)
        cls.root = Path(folder.name).resolve()
        alerts.RECORDED.clear()
        cls.engine, cls.summary = seed_into(cls.root)
        cls.addClassCleanup(cls.engine.dispose)

    def rows(self, model):
        with db.session(self.engine) as opened:
            return opened.query(model).order_by(model.id).all()

    def test_three_people_three_accounts_a_shared_pixel_and_one_iphone_each(self):
        people = self.rows(Person)
        self.assertEqual([p.slug for p in people], ["avery", "blair", "casey"])
        self.assertEqual((self.summary.people, self.summary.accounts, self.summary.pixels, self.summary.devices), (3, 3, 2, 3))
        by_slug = {p.slug: p.id for p in people}
        shared, single, ended = self.rows(GoogleAccount)
        self.assertEqual((ended.default_person_id, ended.session_state), (by_slug["blair"], "ok"))
        with db.session(self.engine) as opened:
            self.assertEqual(accounts.served_people(opened, shared.id), {by_slug["avery"], by_slug["blair"]})
            self.assertEqual(accounts.served_people(opened, single.id), {by_slug["casey"]})
            self.assertEqual(accounts.accounts_serving(opened, by_slug["blair"]), {shared.id, ended.id})
        self.assertEqual((shared.default_person_id, single.default_person_id), (by_slug["avery"], by_slug["casey"]))
        self.assertEqual((shared.session_state, single.session_state), ("needs_sign_in", "ok"))
        pixels = self.rows(Pixel)
        self.assertEqual([p.google_account_id for p in pixels], [shared.id, single.id])
        with db.session(self.engine) as opened:
            self.assertEqual(opened.query(PixelPerson).count(), 3)
        devices = self.rows(Device)
        self.assertEqual(sorted(d.person_id for d in devices), sorted(by_slug.values()))
        for device in devices:
            self.assertFalse(device.cleanup_enabled)
            self.assertFalse(all(c in "0123456789abcdef" for c in device.token_hash), "a demo device must have no usable token")

    def test_every_original_is_indexed_with_and_without_gps_including_a_live_photo_pair(self):
        self.assertEqual(self.summary.failed, 0)
        items = self.rows(MediaItem)
        self.assertEqual(len(items), self.summary.originals)
        self.assertEqual(len(items), self.summary.indexed)
        for person in self.rows(Person):
            mine = [item for item in items if item.person_id == person.id]
            self.assertGreater(len(mine), 0, person.slug)
            self.assertEqual({item.gps_source for item in mine}, {"exif", "none"}, person.slug)
            self.assertTrue(all(item.source == "phone" and item.sha1 and item.bytes for item in mine))
        paths = {item.path for item in items}
        self.assertIn("avery/originals/IMG_0003.HEIC", paths)
        self.assertIn("avery/originals/IMG_0003.MOV", paths)
        self.assertEqual({item.time_source for item in items}, {"exif_offset", "gps_lookup", "unknown"})
        self.assertTrue(any(item.duration_ms and item.path.endswith(".MOV") for item in items))
        self.assertEqual({(item.width, item.height) for item in items}, {demo.SIZE})
        self.assertTrue(all(path.is_file() for path in [self.root / "photos" / item.path for item in items]))

    def test_the_open_alerts_went_to_the_recorder_never_the_network(self):
        alert, unsure, failed = sorted(self.rows(Alert), key=lambda each: each.kind != "sign_in_needed")
        (shared, _, ended) = self.rows(GoogleAccount)
        self.assertEqual((unsure.kind, unsure.key, unsure.closed), ("schedule_renewal_unconfirmed", renewal.failed_key(shared.id), None))
        self.assertEqual((failed.kind, failed.key, failed.closed), ("schedule_renewal_failed", renewal.failed_key(ended.id), None))
        self.assertEqual(self.summary.alerts, 3)
        self.assertEqual((alert.kind, alert.subject, alert.key, alert.closed), ("sign_in_needed", shared.email, browser_jobs.sign_in_key(shared.id), None))
        self.assertEqual(alert.opened, NOON - 2 * 3600)
        self.assertIn(("Google sign-in needed", alerts.sign_in_needed(shared.email).message, None), alerts.RECORDED)
        with db.session(self.engine) as opened:
            types = [e.type for e in events.recent(opened)]
        self.assertEqual(types.count("alert_sent"), 3)
        self.assertEqual(types.count("device_checkin"), 3)
        self.assertEqual(types.count("originals_indexed"), 3)

    def test_nothing_reads_as_a_real_person_place_or_device(self):
        for person in self.rows(Person):
            self.assertTrue(person.email.endswith("@example.com"), person.email)
            self.assertTrue(person.name.endswith(" Example"), person.name)
        for account in self.rows(GoogleAccount):
            self.assertTrue(account.email.endswith("@example.com"), account.email)
        for pixel in self.rows(Pixel):
            self.assertTrue(pixel.syncthing_device_id.startswith("DEMO-"), pixel.syncthing_device_id)
        first_names = {p.name.split()[0] for p in self.rows(Person)}
        for device in self.rows(Device):
            self.assertIn(device.name.split("'")[0], first_names, device.name)
        for item in self.rows(MediaItem):
            if item.lat is not None:
                self.assertIn((item.lat, item.lon), demo.OCEANS, item.path)
            self.assertIn(item.make, (None, demo.MAKE))
        self.assertEqual(demo.MEMBER.email, "avery@example.com")
        self.assertTrue(demo.MEMBER.sub.startswith("demo:"))

    def test_check_ins_and_captures_are_relative_to_today(self):
        devices = {d.name: d for d in self.rows(Device)}
        self.assertEqual(devices["Avery's iPhone"].last_checkin, NOON - 3600)
        self.assertEqual(devices["Blair's iPhone"].last_checkin, NOON - 3 * DAY)
        self.assertEqual(devices["Casey's iPhone"].last_checkin, NOON - 20 * DAY)
        days = sorted(date.fromisoformat(item.captured_local[:10]) for item in self.rows(MediaItem))
        self.assertEqual((days[0], days[-1]), (TODAY - timedelta(days=400), TODAY - timedelta(days=1)))
        self.assertTrue(all(item.indexed_at == NOON for item in self.rows(MediaItem)))

    def test_two_seeds_on_the_same_day_are_byte_identical_apart_from_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            again = Path(folder).resolve()
            engine, summary = seed_into(again)
            self.addCleanup(engine.dispose)
            self.assertEqual(summary, self.summary)
            self.assertEqual(table_rows(engine), table_rows(self.engine))
            self.assertEqual(file_digests(again / "photos"), file_digests(self.root / "photos"))
            self.assertEqual(len(file_digests(again / "photos")), self.summary.originals + 1)

    def test_one_imported_export_shows_a_run_and_one_pending_review_pair_with_its_held_copy(self):
        exports = self.rows(Export)
        self.assertEqual([(e.status, e.kind) for e in exports], [("imported", "incremental"), ("downloading", "incremental")])
        runs = self.rows(ImportRun)
        self.assertEqual([(r.status, r.added, r.review, r.missing_gps) for r in runs], [("done", 2, 1, 3)])
        reviews = self.rows(ReviewItem)
        self.assertEqual([r.status for r in reviews], ["pending"])
        hold = Photos(self.root / "photos").root / reviews[0].hold_path
        self.assertTrue(hold.is_file())
        decision = self.rows(DedupeDecision)[0]
        self.assertEqual((decision.tier, decision.decided_by), (4, "auto"))
        matched = next(item for item in self.rows(MediaItem) if item.id == decision.matched_item_id)
        self.assertTrue(matched.path.endswith("IMG_0101.JPG"))
        self.assertNotEqual(hold.read_bytes(), (self.root / "photos" / matched.path).read_bytes())

    def test_keep_both_and_undo_work_on_the_seeded_pair_and_a_fresh_seed_has_it_again(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            engine, _ = seed_into(root)
            self.addCleanup(engine.dispose)
            settings = settings_from(DARKROOM_DEMO="1", DARKROOM_PUBLIC_URL="http://127.0.0.1")
            with db.session(engine) as opened:
                token = demo.sign_in(opened, settings, utc_now().timestamp())
            settings = dataclasses.replace(settings, photos_root=root / "photos", data_dir=root / "data")
            base, _ = start_server(self, settings, engine)
            headers = {"Cookie": f"{auth.SESSION_COOKIE}={token}", "Content-Type": "application/json", "Origin": base}
            status, _, body = fetch(base, "GET", "/api/review?status=pending", headers)
            self.assertEqual(status, 200)
            pending = json.loads(body)["reviews"]
            self.assertEqual(len(pending), 1)
            review_id = pending[0]["id"]
            before = file_digests(root / "photos")
            status, _, body = fetch(base, "POST", f"/api/review/{review_id}/resolve", headers, json.dumps({"action": "keep_both"}).encode())
            self.assertEqual((status, json.loads(body)["review"]["status"]), (200, "kept_both"), body)
            self.assertNotEqual(file_digests(root / "photos"), before)
            status, _, body = fetch(base, "POST", f"/api/review/{review_id}/undo", headers, b"{}")
            self.assertEqual((status, json.loads(body)["review"]["status"]), (200, "pending"), body)
            self.assertEqual(file_digests(root / "photos"), before)
            with tempfile.TemporaryDirectory() as again:
                engine2, _ = seed_into(Path(again).resolve())
                self.addCleanup(engine2.dispose)
                with db.session(engine2) as opened:
                    self.assertEqual([r.status for r in opened.query(ReviewItem).all()], ["pending"])

    def test_the_imported_export_has_its_part_downloaded(self):
        part = self.rows(ExportPart)[0]
        self.assertEqual((part.status, part.downloads_used), ("done", 1))
        self.assertGreater(part.bytes, 0)

    def test_a_second_export_is_part_way_through_its_download(self):
        parts = self.rows(ExportPart)[1:]
        self.assertEqual([(part.number, part.status) for part in parts], [(1, "done"), (2, "downloading"), (3, "pending")])
        with db.session(self.engine) as opened:
            export = opened.get(Export, parts[0].export_id)
            self.assertEqual([each["progress"] for each in exports.as_dict(opened, export)["parts"]], [100, 40, 0])

    def test_every_seeded_export_is_pinned_to_its_owner_whatever_the_accounts_default_person_becomes(self):
        with db.session(self.engine) as opened:
            seeded = list(opened.query(Export).order_by(Export.id))
            self.assertEqual(len(seeded), 2)
            for export in seeded:
                account = opened.get(GoogleAccount, export.account_id)
                self.assertIsNotNone(export.owner_person_id)
                self.assertEqual(export.owner_person_id, account.default_person_id)
            other = opened.query(Person).filter(Person.id != seeded[1].owner_person_id).first()
            opened.get(GoogleAccount, seeded[1].account_id).default_person_id = other.id
            opened.flush()
            self.assertEqual(exports.owner_of(opened, seeded[1]).id, seeded[1].owner_person_id)
            self.assertNotEqual(exports.owner_of(opened, seeded[1]).id, other.id)
            opened.rollback()

    def test_a_seed_for_another_day_moves_with_it(self):
        with tempfile.TemporaryDirectory() as folder:
            engine, _ = seed_into(Path(folder).resolve(), date(2026, 12, 30))
            self.addCleanup(engine.dispose)
            with db.session(engine) as opened:
                newest = max(item.captured_local for item in opened.query(MediaItem))
                self.assertEqual(newest[:10], "2026-12-29")


def tree_digests(root):
    return {str(path.relative_to(root)): (hashlib.sha1(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns) for path in sorted(Path(root).rglob("*")) if path.is_file()}


class RunDemoTests(unittest.TestCase):
    def run_demo(self, folder, **env):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err, \
                mock.patch.dict(os.environ, env, clear=True), mock.patch.object(tls, "urlopen", side_effect=no_network):
            code = run.main(["demo", "--data", folder] if folder else ["demo"])
        return code, out.getvalue(), err.getvalue()

    def populated(self, folder):
        root = Path(folder)
        originals = root / "photos" / "avery" / "originals"
        originals.mkdir(parents=True)
        (originals / "IMG_0001.JPG").write_bytes(b"a real original, not to be touched")
        (root / "data").mkdir()
        (root / "data" / "darkroom.db").write_bytes(b"a real database")
        return tree_digests(root)

    def test_run_demo_without_data_refuses_and_touches_neither_the_configured_photos_nor_the_database(self):
        with tempfile.TemporaryDirectory() as folder:
            before = self.populated(folder)
            code, _, err = self.run_demo(None, DARKROOM_PHOTOS_ROOT=f"{folder}/photos", DARKROOM_DATA_DIR=f"{folder}/data", DATABASE_URL=f"sqlite:///{folder}/data/darkroom.db")
            self.assertEqual(code, 1)
            self.assertIn("needs --data", err)
            self.assertEqual(tree_digests(folder), before)

    def test_run_demo_refuses_a_folder_whose_photos_or_data_already_hold_anything(self):
        with tempfile.TemporaryDirectory() as folder:
            before = self.populated(folder)
            code, _, err = self.run_demo(folder)
            self.assertEqual((code, "already holds something" in err), (1, True))
            self.assertEqual(tree_digests(folder), before)
            shutil.rmtree(Path(folder) / "data")
            before = tree_digests(folder)
            code, _, err = self.run_demo(folder)
            self.assertEqual((code, "already holds something" in err), (1, True))
            self.assertEqual(tree_digests(folder), before)
            self.assertFalse((Path(folder) / "data").exists())
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "data").mkdir()
            (Path(folder) / "data" / "notes.txt").write_text("mine")
            self.assertEqual(self.run_demo(folder)[0], 1)
            self.assertEqual(os.listdir(folder), ["data"])
            self.assertEqual(os.listdir(Path(folder) / "data"), ["notes.txt"])

    def test_the_writer_itself_never_replaces_a_file_even_without_the_guards(self):
        from darkroom.storage.paths import AlreadyExists

        with tempfile.TemporaryDirectory() as folder:
            originals = Path(folder)
            real = originals / "IMG_0003.HEIC"
            real.write_bytes(b"a real original")
            os.utime(real, (1_000_000_000, 1_000_000_000))
            before = tree_digests(folder)
            live = next(shot for shot in demo.SHOTS["avery"] if shot.stem == "IMG_0003")
            with self.assertRaises(AlreadyExists):
                demo.write_shot(originals, live, TODAY)
            self.assertEqual(tree_digests(folder), before)
            plain = next(shot for shot in demo.SHOTS["avery"] if shot.stem == "IMG_0001")
            (written,) = demo.write_shot(originals, plain, TODAY)
            self.assertEqual(written.name, "IMG_0001.JPG")
            with self.assertRaises(AlreadyExists):
                demo.write_shot(originals, plain, TODAY)

    def test_a_session_file_that_cannot_be_written_leaves_an_empty_folder_for_a_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.object(run, "write_demo_session", side_effect=OSError(28, "No space left on device")):
                code, _, err = self.run_demo(folder)
            self.assertEqual(code, 1)
            self.assertIn("No space left", err)
            self.assertIn("can be used again", err)
            self.assertEqual(tree_digests(folder), {})
            self.assertEqual(self.run_demo(folder)[0], 0)
            self.assertTrue((Path(folder) / "data" / "demo-session.json").is_file())

    def test_run_demo_seeds_an_empty_folder_and_the_status_route_shows_three_people_with_counts(self):
        with tempfile.TemporaryDirectory() as folder:
            code, out, _ = self.run_demo(folder)
            self.assertEqual(code, 0, out)
            self.assertIn("Seeded 3 made-up people", out)
            self.assertIn("DARKROOM_DEMO=1", out)
            session_file = Path(folder) / "data" / "demo-session.json"
            self.assertEqual(stat.S_IMODE(session_file.stat().st_mode), 0o600)
            saved = json.loads(session_file.read_text())
            self.assertEqual((saved["cookie"], saved["sub"], saved["name"]), (auth.SESSION_COOKIE, demo.MEMBER.sub, demo.MEMBER.name))
            self.assertNotIn(saved["token"], out)
            settings = settings_from(DARKROOM_DEMO="1", DARKROOM_DATA_DIR=f"{folder}/data", DARKROOM_PHOTOS_ROOT=f"{folder}/photos")
            engine = db.make_engine(db.database_url(settings))
            self.addCleanup(engine.dispose)
            with db.session(engine) as opened:
                (row,) = opened.query(AuthSession).all()
                self.assertEqual(row.sub, demo.MEMBER.sub)
                self.assertNotEqual(row.token_hash, saved["token"])
            base, _ = start_server(self, settings, engine)
            cookie = {"Cookie": f"{auth.SESSION_COOKIE}={saved['token']}"}
            status, _, body = fetch(base, "GET", "/api/status", cookie)
            self.assertEqual(status, 200)
            people = json.loads(body)["people"]
            self.assertEqual([p["slug"] for p in people], ["avery", "blair", "casey"])
            self.assertEqual([p["name"] for p in people], ["Avery Example", "Blair Example", "Casey Example"])
            self.assertTrue(all(p["indexed"] > 0 for p in people), people)
            status, _, body = fetch(base, "GET", "/api/me", cookie)
            self.assertEqual((status, json.loads(body)["sub"]), (200, demo.MEMBER.sub))
            self.assertEqual(fetch(base, "GET", "/api/status")[0], 401)
            code, _, err = self.run_demo(folder)
            self.assertEqual(code, 1)
            self.assertIn("already holds something", err)

    def test_run_demo_refuses_a_production_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            code, _, err = self.run_demo(folder, OIDC_ISSUER="https://id.example", OIDC_CLIENT_ID="darkroom")
            self.assertEqual(code, 1)
            self.assertIn("DARKROOM_DEMO=1 cannot be combined with OIDC sign-in", err)
            code, _, err = self.run_demo(folder, DARKROOM_HOST="0.0.0.0")
            self.assertEqual(code, 1)
            self.assertIn("loopback", err)
            self.assertEqual(os.listdir(folder), [])

    def test_run_demo_without_exiftool_says_so_fails_and_leaves_the_folder_empty(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(exiftool, "executable", side_effect=exiftool.Missing("exiftool is not installed")):
            code, _, err = self.run_demo(folder)
            self.assertEqual(tree_digests(folder), {})
        self.assertEqual(code, 1)
        self.assertIn("exiftool is not installed", err)


if __name__ == "__main__":
    unittest.main()
