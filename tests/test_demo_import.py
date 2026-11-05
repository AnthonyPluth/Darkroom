import dataclasses
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

import run
from darkroom import tls
from darkroom.dates import utc_now
from darkroom.storage import db
from darkroom.domain import demo
from darkroom.storage.models import Person
from tests.import_world import tree
from tests.shared import fetch, settings_from, start_server
from tools import demo_import
from tools import verify as tool

TIERS = {"added": 4, "skipped_t1": 5, "skipped_t2": 1, "skipped_t3": 1, "review": 2, "missing_gps": 3, "failed": 0}
REVIEWED = {"IMG_0001(1).JPG", "IMG_0102(1).JPG"}
HAND_MADE_REVIEW = "IMG_0101.JPG"


def originals_of(photos_root):
    return {slug: tree(photos_root / slug / "originals") for slug in (who.slug for who in demo.PEOPLE)}


class DemoImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        folder = tempfile.TemporaryDirectory()
        cls.addClassCleanup(folder.cleanup)
        cls.root = Path(folder.name).resolve()
        cls.hosts = []
        real = tls.urlopen

        def watched(url, **kwargs):
            cls.hosts.append(urlsplit(url).hostname)
            return real(url, **kwargs)

        values = {"DARKROOM_DEMO": "1", "DARKROOM_HOST": "127.0.0.1", "DARKROOM_DATA_DIR": str(cls.root / "data"), "DARKROOM_PHOTOS_ROOT": str(cls.root / "photos"),
                  "DARKROOM_SECRET_KEY": "demo-secret-key-not-real"}
        with mock.patch.dict(os.environ, values, clear=True), mock.patch.object(tls, "urlopen", watched):
            settings = run.demo_settings(str(cls.root))
            settings.photos_root.mkdir(parents=True)
            run.seed_demo(settings)
            cls.settings = settings
            cls.before = originals_of(settings.photos_root)
            cls.result = demo_import.seed_import(settings, utc_now().date())
            cls.engine = db.make_engine(db.database_url(settings))
        cls.addClassCleanup(cls.engine.dispose)

    def setUp(self):
        served = dataclasses.replace(settings_from(DARKROOM_ALLOW_NO_AUTH="1", DARKROOM_PUBLIC_URL="http://127.0.0.1"),
                                     photos_root=self.settings.photos_root, data_dir=self.settings.data_dir)
        self.base, _ = start_server(self, served, self.engine)

    def get(self, path):
        status, _, body = fetch(self.base, "GET", path)
        self.assertEqual(status, 200, path)
        return json.loads(body)

    def real_run(self):
        runs = self.get("/api/imports")["imports"]
        (found,) = [item for item in runs if item["export_kind"] == "manual"]
        return self.get(f"/api/imports/{found['id']}")["run"]

    def test_the_run_counts_equal_what_the_builder_made(self):
        shown = self.real_run()
        self.assertEqual({key: shown[key] for key in TIERS}, TIERS)
        self.assertEqual((shown["status"], shown["stage"], shown["error"]), ("done", "done", None))
        self.assertEqual(self.result["files"], shown["added"] + shown["skipped_t1"] + shown["skipped_t2"] + shown["skipped_t3"] + shown["review"] + shown["failed"])
        self.assertEqual((self.result["code"], shown["part_count"], len(shown["parts"])), (0, demo_import.PARTS, demo_import.PARTS))

    def test_the_review_queue_lists_the_likely_re_encodes(self):
        names = {item["name"] for item in self.get("/api/review?status=pending")["reviews"]}
        self.assertEqual(names, REVIEWED | {HAND_MADE_REVIEW})

    def test_the_fake_recorded_one_rescan_for_the_library_that_changed(self):
        with self.settings.data_dir.joinpath(demo_import.RESULT_FILE).open() as saved:
            recorded = json.load(saved)
        self.assertEqual(recorded["scans"], [{"library": "demo-library-avery", "person": "avery"}])
        self.assertEqual(self.result["scans"], recorded["scans"])

    def test_the_demo_people_were_linked_to_the_fakes_users(self):
        with db.session(self.engine) as opened:
            linked = {person.slug: person.immich_user_id for person in opened.query(Person)}
        self.assertEqual(linked, {fake.slug: fake.user_id for fake in demo_import.fake_people()})

    def test_every_request_stayed_on_127_0_0_1(self):
        self.assertTrue(self.hosts)
        self.assertEqual(set(self.hosts), {"127.0.0.1"})

    def test_the_export_folder_is_removed_and_the_originals_are_untouched(self):
        self.assertFalse((self.root / demo_import.EXPORT_FOLDER).exists())
        self.assertEqual(originals_of(self.settings.photos_root), self.before)
        self.assertEqual(sum(len(files) for files in self.before.values()), 11)

    def test_an_export_that_names_no_real_place(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = demo_import.build_export(self.settings.photos_root, Path(folder) / "export", utc_now().date())
            self.assertEqual(len(manifest.parts), demo_import.PARTS)
            self.assertEqual(len(manifest.media), self.result["files"])
            self.assertEqual(manifest.live_pairs, {"IMG_0003.MOV": "IMG_0003.HEIC", "IMG_5003.MOV": "IMG_5003.HEIC"})
            self.assertEqual(manifest.edited_of, {"IMG_0103-edited.HEIC": "IMG_0103.HEIC"})
            for part in manifest.parts:
                with zipfile.ZipFile(part) as archive:
                    for name in archive.namelist():
                        if name.endswith(".json"):
                            geo = json.loads(archive.read(name))["geoData"]
                            self.assertIn((geo["latitude"], geo["longitude"]), {(0.0, 0.0), *demo.OCEANS})


class SeedWithImportTests(unittest.TestCase):
    def test_seed_runs_the_import_only_when_asked(self):
        with mock.patch.object(tool.subprocess, "run") as ran:
            ran.return_value = mock.Mock(args=[], returncode=0, stdout="", stderr="")
            tool.seed_demo("/x", "lib")
            tool.seed_demo("/x", "lib", True)
        commands = [call.args[0] for call in ran.call_args_list]
        self.assertEqual(len(commands), 3)
        self.assertEqual(commands[2][1:3], ["-m", "tools.demo_import"])
        self.assertNotIn("tools.demo_import", commands[0] + commands[1])

    def test_a_failed_seed_skips_the_import(self):
        with mock.patch.object(tool.subprocess, "run") as ran:
            ran.return_value = mock.Mock(args=[], returncode=1, stdout="", stderr="no")
            done = tool.seed_demo("/x", "lib", True)
        self.assertEqual((done.returncode, ran.call_count), (1, 1))

    def test_the_import_is_given_the_seed_folder(self):
        self.assertEqual(tool.import_command("/r")[-2:], ["--data", "/r"])


if __name__ == "__main__":
    unittest.main()
