import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from darkroom_browser import config
from darkroom_browser.context import Stagger
from darkroom_browser.runner import Runner
from darkroom_init import plan

TOKEN = "t" * 40


class StaggerSettingsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = folder.name

    def loaded(self, **values):
        given = {"PHOTOS_ROOT": self.root, **values}
        with mock.patch.dict(os.environ, given, clear=True):
            return config.load()

    def stagger_problems(self, settings):
        return [problem for problem in config.problems(settings) if "STAGGER" in problem]

    def test_off_by_default_with_ten_minutes_and_two_parts(self):
        settings = self.loaded()
        self.assertEqual((settings.stagger, settings.stagger_minutes, settings.stagger_parts), ("0", "10", "2"))
        self.assertIsNone(config.stagger_of(settings))
        self.assertEqual(self.stagger_problems(settings), [])

    def test_turned_on_it_takes_the_minutes_and_the_most_parts(self):
        self.assertEqual(config.stagger_of(self.loaded(TAKEOUT_STAGGER="1")), Stagger(600.0, 2))
        self.assertEqual(config.stagger_of(self.loaded(TAKEOUT_STAGGER="1", TAKEOUT_STAGGER_MINUTES="1", TAKEOUT_STAGGER_MAX_PARTS="4")), Stagger(60.0, 4))
        self.assertEqual(config.stagger_of(self.loaded(TAKEOUT_STAGGER="1", TAKEOUT_STAGGER_MINUTES=" 120 ", TAKEOUT_STAGGER_MAX_PARTS="3")), Stagger(7200.0, 3))
        self.assertIsNone(config.stagger_of(self.loaded(TAKEOUT_STAGGER="0", TAKEOUT_STAGGER_MINUTES="5")))

    def test_bad_values_are_refused_without_turning_staggering_on(self):
        cases = {
            "TAKEOUT_STAGGER": ("yes", "2", "on", "-1"),
            "TAKEOUT_STAGGER_MINUTES": ("0", "121", "1.5", "-3", "ten", "٣"),
            "TAKEOUT_STAGGER_MAX_PARTS": ("1", "5", "0", "two", "2.0"),
        }
        for name, values in cases.items():
            for value in values:
                with self.subTest(name=name, value=value):
                    settings = self.loaded(**{"TAKEOUT_STAGGER": "1", name: value})
                    problems = self.stagger_problems(settings)
                    self.assertEqual(len(problems), 1)
                    self.assertTrue(problems[0].startswith(name))
                    self.assertIsNone(config.stagger_of(settings))

    def test_the_init_hands_the_settings_to_the_worker(self):
        env = plan.worker_env({"TAKEOUT_STAGGER": "1", "TAKEOUT_STAGGER_MINUTES": "15", "TAKEOUT_STAGGER_MAX_PARTS": "3", "DARKROOM_PHOTOS_ROOT": "/share"})
        self.assertEqual((env["TAKEOUT_STAGGER"], env["TAKEOUT_STAGGER_MINUTES"], env["TAKEOUT_STAGGER_MAX_PARTS"]), ("1", "15", "3"))
        self.assertNotIn("TAKEOUT_STAGGER", plan.app_env({"TAKEOUT_STAGGER": "1"}))

    def test_the_runner_gives_each_job_the_setting(self):
        settings = config.Settings(TOKEN, Path(self.root), ":99", 15.0, "worker", stagger="1", stagger_minutes="5", stagger_parts="3")
        runner = Runner(settings, mock.Mock(), lambda profile: None)
        self.assertEqual(runner.context_for({"id": 1, "kind": "download", "account": "1"}).stagger, Stagger(300.0, 3))
        off = Runner(config.Settings(TOKEN, Path(self.root), ":99", 15.0, "worker"), mock.Mock(), lambda profile: None)
        self.assertIsNone(off.context_for({"id": 1, "kind": "download", "account": "1"}).stagger)

    def test_every_deployment_lists_the_settings_and_their_defaults(self):
        root = Path(__file__).resolve().parents[2]
        example = (root / ".env.example").read_text()
        compose = (root / "docker-compose.yml").read_text()
        template = (root / "deploy/unraid/darkroom.xml").read_text()
        docs = (root / "docs/deploy/browser.md").read_text()
        for name, default in (("TAKEOUT_STAGGER", "0"), ("TAKEOUT_STAGGER_MINUTES", "10"), ("TAKEOUT_STAGGER_MAX_PARTS", "2")):
            self.assertIn(f"{name}={default}\n", example)
            self.assertIn(f"{name}: ${{{name}:-{default}}}", compose)
            self.assertIn(f'Target="{name}" Default="{default}"', template)
            self.assertIn(f"`{name}`", docs)


if __name__ == "__main__":
    unittest.main()
