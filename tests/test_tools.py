import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import api_contract
import coverage_floor
import feature_map
import fleet_checks
import no_comments


class NoCommentsTests(unittest.TestCase):
    def test_comments_and_docstrings_are_found(self):
        self.assertEqual(no_comments.python_comments("x = 1  # why\n"), [1])
        self.assertEqual(no_comments.python_comments("x = 1  # noqa: E402\n"), [])
        self.assertEqual(no_comments.python_docstrings('def f():\n    "doc"\n    return 1\n'), [2])

    def test_problems_name_the_file_and_line(self):
        self.assertEqual(len(no_comments.problems("a.py", "# no\nx = 1\n")), 1)
        self.assertEqual(no_comments.problems("a.txt", "# fine"), [])


class CoverageFloorTests(unittest.TestCase):
    def report(self, total, domain):
        return {"totals": {"percent_covered": total}, "files": {"darkroom/domain/x.py": {"summary": {"percent_covered": domain, "num_statements": 10}}}}

    def test_floor_met(self):
        self.assertEqual(coverage_floor.problems(self.report(90, 95)), [])

    def test_overall_and_module_floors(self):
        self.assertEqual(len(coverage_floor.problems(self.report(80, 95))), 1)
        self.assertEqual(len(coverage_floor.problems(self.report(90, 85))), 1)

    def test_main_reads_a_report_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "coverage.json"
            path.write_text(json.dumps(self.report(90, 95)))
            self.assertEqual(coverage_floor.main(["x", str(path)]), 0)
            path.write_text(json.dumps(self.report(10, 10)))
            self.assertEqual(coverage_floor.main(["x", str(path)]), 1)


class FleetChecksTests(unittest.TestCase):
    def test_one_head_and_a_test_per_migration(self):
        good = [{"revision": "0001", "down": None, "file": "0001_a.py"}, {"revision": "0002", "down": "0001", "file": "0002_b.py"}]
        tests = "def test_0001_a(self): pass\ndef test_0002_b(self): pass\n"
        self.assertEqual(fleet_checks.check_migrations(good, tests), [])
        self.assertEqual(len(fleet_checks.check_migrations(good, "def test_0001_a(self): pass\n")), 1)
        forked = [*good, {"revision": "0003", "down": "0001", "file": "0003_c.py"}]
        self.assertTrue(any("heads" in p for p in fleet_checks.check_migrations(forked, tests + "def test_0003_c(self): pass\n")))

    def test_migrations_are_read_from_files(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "0001_baseline.py").write_text('down_revision = None\n')
            (Path(folder) / "0002_more.py").write_text('down_revision = "0001"\n')
            found = fleet_checks.migrations(Path(folder))
        self.assertEqual([m["down"] for m in found], [None, "0001"])

    def test_commit_trailers(self):
        good = "feat: x\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\nClaude-Session: https://example/1"
        self.assertEqual(fleet_checks.check_commit("a" * 40, good), [])
        self.assertEqual(fleet_checks.check_commit("a" * 40, "feat: by a person"), [])
        self.assertEqual(len(fleet_checks.check_commit("a" * 40, "feat: x\n\nClaude-Session: https://example/1")), 1)
        self.assertEqual(len(fleet_checks.check_commit("a" * 40, "x\n\nCo-Authored-By: Claude <noreply@anthropic.com>")), 1)
        two = good + "\nCo-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
        self.assertEqual(len(fleet_checks.check_commit("a" * 40, two)), 1)

    def test_workflow_conventions(self):
        pinned = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
        good = f"defaults:\n  run:\n    shell: bash\njobs:\n  a:\n    steps:\n      - uses: {pinned}   # v7.0.1\n        with:\n          persist-credentials: false\n"
        self.assertEqual(fleet_checks.check_workflow("w.yml", good), [])
        self.assertTrue(any("shell: bash" in p for p in fleet_checks.check_workflow("w.yml", good.replace("shell: bash", "shell: sh"))))
        self.assertTrue(any("pinned" in p for p in fleet_checks.check_workflow("w.yml", good.replace(pinned, "actions/checkout@v7"))))
        self.assertTrue(any("version in a comment" in p for p in fleet_checks.check_workflow("w.yml", good.replace("   # v7.0.1", ""))))
        self.assertTrue(any("persist-credentials" in p for p in fleet_checks.check_workflow("w.yml", good.replace("persist-credentials: false", "fetch-depth: 0"))))
        listing = good + "      - run: gh api repos/x/y/pulls?per_page=100\n"
        self.assertTrue(any("paginate" in p for p in fleet_checks.check_workflow("w.yml", listing)))

    def test_this_repositorys_workflows_pass(self):
        self.assertEqual(fleet_checks.check_workflows(), [])

    def test_removed_tests_need_a_reason(self):
        self.assertEqual(fleet_checks.test_names("def test_a(self): pass\n    def test_b(self): pass"), {"test_a", "test_b"})
        self.assertTrue(fleet_checks.is_test_file("tests/test_x.py"))
        self.assertFalse(fleet_checks.is_test_file("tools/test_x.py"))


class ContractAndFeatureMapTests(unittest.TestCase):
    def test_the_committed_contract_is_current(self):
        self.assertEqual((ROOT / "docs/openapi.json").read_text(), api_contract.render())

    def test_the_committed_feature_map_is_current_and_every_route_has_a_test(self):
        self.assertEqual((ROOT / "docs/feature-map.json").read_text(), feature_map.render())
        self.assertEqual(feature_map.untested(feature_map.build()), [])

    def test_healthz_is_in_the_contract(self):
        built = api_contract.build()
        self.assertIn("/healthz", built["paths"])

    def test_lists_become_arrays_in_the_contract(self):
        from typing import TypedDict

        class Reply(TypedDict):
            names: list[str]
            counts: list[int]
            nested: list[list[str]]
            anything: list
            other: dict

        schema = api_contract.schema_of(Reply)["properties"]
        self.assertEqual(schema["names"], {"type": "array", "items": {"type": "string"}})
        self.assertEqual(schema["counts"], {"type": "array", "items": {"type": "integer"}})
        self.assertEqual(schema["nested"], {"type": "array", "items": {"type": "array", "items": {"type": "string"}}})
        self.assertEqual(schema["anything"], {"type": "array"})
        self.assertEqual(schema["other"], {"type": "object"})
        me = api_contract.build()["paths"]["/api/me"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(me["properties"]["groups"], {"type": "array", "items": {"type": "string"}})


if __name__ == "__main__":
    unittest.main()
