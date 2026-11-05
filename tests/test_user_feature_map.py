import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import user_feature_map as ufm


class UserFeatureMapTests(unittest.TestCase):
    def test_the_committed_map_is_what_the_generator_writes_from_the_pages_and_flows(self):
        text, problems = ufm.generate()
        self.assertEqual(problems, [])
        self.assertEqual(ufm.check(ufm.OUT.read_text(), text), [])
        for page in ("sign-in", "status", "people"):
            self.assertIn(f"(`{page}`)", text)
        self.assertIn("#### `open-people`", text)

    def test_a_stale_or_missing_map_is_named(self):
        self.assertIn("missing", ufm.check(None, "x")[0])
        self.assertIn("isn't what", ufm.check("old", "new")[0])
        self.assertEqual(ufm.check("same", "same"), [])

    def test_a_gotcha_under_an_unknown_heading_or_a_loose_line_is_a_problem(self):
        _, problems = ufm.gotchas("## nope\n- a\n## people\nloose\n- b\n## people\n", {"people"})
        self.assertEqual(len(problems), 3)

    def test_routes_and_labels_are_read_from_the_sources(self):
        self.assertEqual(ufm.routes('export const ROUTES = { "sign-in": "/", people: "/people" };'), {"sign-in": "/", "people": "/people"})
        self.assertEqual(ufm.nav_labels('{ path: "/people", label: "People" },'), {"/people": "People"})
