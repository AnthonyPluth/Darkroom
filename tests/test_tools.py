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
        built = api_contract.build()
        reply = built["paths"]["/api/me"]["get"]["responses"]["2XX"]["content"]["application/json"]["schema"]
        self.assertEqual(reply, {"$ref": "#/components/schemas/Me"})
        me = built["components"]["schemas"]["Me"]
        self.assertEqual(me["properties"]["groups"], {"type": "array", "items": {"type": "string"}})

    def test_the_committed_api_types_are_current(self):
        self.assertEqual(api_contract.stale(api_contract.wanted()), [])

    def test_a_reply_and_a_body_are_read_from_the_handlers_annotations(self):
        built = api_contract.build()
        enqueue = built["paths"]["/api/browser/jobs"]["post"]
        self.assertEqual(enqueue["requestBody"]["content"]["application/json"]["schema"], {"$ref": "#/components/schemas/EnqueueBody"})
        self.assertEqual(enqueue["responses"]["2XX"]["content"]["application/json"]["schema"], {"$ref": "#/components/schemas/BrowserJobReply"})
        self.assertEqual(enqueue["x-auth"], "session")
        screenshot = built["paths"]["/api/browser/failures/{id}/screenshot.png"]["get"]
        self.assertNotIn("content", screenshot["responses"]["2XX"])
        self.assertIn("BrowserJobView", built["components"]["schemas"])

    def test_optional_fields_and_unions_reach_the_typescript(self):
        text = api_contract.render_ts(api_contract.build())
        self.assertIn("  failure_dir?: string;\n", text)
        self.assertIn("  message: string;\n", text)
        self.assertIn("  job: WorkerJob | null;\n", text)
        self.assertIn("  started: number | null;\n", text)
        self.assertIn("  payload: Record<string, unknown>;\n", text)
        self.assertIn('  "GET /api/me": { body: never; reply: Me };\n', text)
        self.assertIn('  "POST /api/browser/jobs": { body: EnqueueBody; reply: BrowserJobReply };\n', text)
        self.assertNotIn("screenshot.png", text)

    def test_literals_and_dicts_have_schemas(self):
        from typing import Literal

        self.assertEqual(api_contract.type_schema(Literal["takeout"]), {"const": "takeout"})
        self.assertEqual(api_contract.type_schema(Literal["a", "b"]), {"enum": ["a", "b"]})
        self.assertEqual(api_contract.type_schema(dict[str, int]), {"type": "object", "additionalProperties": {"type": "integer"}})
        self.assertEqual(api_contract.ts_type({"enum": ["a", "b"]}), '"a" | "b"')
        self.assertEqual(api_contract.ts_type({"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "null"}]}}), "(string | null)[]")
        self.assertEqual(api_contract.ts_type({"type": "array"}), "unknown[]")
        self.assertEqual(api_contract.ts_type({}), "unknown")

    def test_a_typed_body_without_a_typed_reply_is_refused(self):
        from typing import Annotated
        from unittest import mock

        from darkroom.server import contract, routes
        from darkroom.server.common import Request, Response

        def half_typed(request: Annotated[Request, contract.EnqueueBody]) -> Response:
            raise AssertionError

        def get_with_body(request: Annotated[Request, contract.EnqueueBody]) -> Annotated[Response, contract.Me]:
            raise AssertionError

        for route, wanted in ((routes.Route("POST", "/api/half", half_typed, "session"), "type both"),
                              (routes.Route("GET", "/api/odd", get_with_body, "session"), "no body")):
            with self.subTest(route=route.path), mock.patch.object(routes, "ROUTES", (route,)):
                with self.assertRaisesRegex(api_contract.ContractError, wanted):
                    api_contract.build()

    def test_a_reply_type_must_live_in_the_contract(self):
        from typing import Annotated, TypedDict
        from unittest import mock

        from darkroom.server import routes
        from darkroom.server.common import Request, Response

        class Elsewhere(TypedDict):
            n: int

        def handler(request: Request) -> Annotated[Response, Elsewhere]:
            raise AssertionError

        with mock.patch.object(routes, "ROUTES", (routes.Route("GET", "/api/x", handler, "session"),)):
            with self.assertRaisesRegex(api_contract.ContractError, "contract.py"):
                api_contract.build()

    def test_the_check_fails_when_api_types_is_stale_and_writing_fixes_it(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as folder:
            openapi, types = Path(folder) / "openapi.json", Path(folder) / "api-types.ts"
            with mock.patch.object(api_contract, "OPENAPI_OUT", openapi), mock.patch.object(api_contract, "TS_OUT", types), \
                    mock.patch.object(api_contract, "ROOT", Path(folder)):
                self.assertEqual(api_contract.main([]), 0)
                self.assertEqual(api_contract.main(["--check"]), 0)
                types.write_text(types.read_text().replace("groups: string[];", "groups: number[];"))
                self.assertEqual(api_contract.main(["--check"]), 1)
                types.unlink()
                self.assertEqual(api_contract.main(["--check"]), 1)
                self.assertEqual(api_contract.main([]), 0)
                self.assertEqual(api_contract.main(["--check"]), 0)

    def test_a_contract_error_fails_the_tool_with_its_reason(self):
        from unittest import mock

        with mock.patch.object(api_contract, "wanted", side_effect=api_contract.ContractError("broken")):
            self.assertEqual(api_contract.main(["--check"]), 1)

    def test_the_feature_map_finds_apicall_callers_but_not_tests(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            src = root / "frontend/src"
            (src / "lib").mkdir(parents=True)
            (src / "App.svelte").write_text('<script lang="ts">\n  void apiCall<"GET /api/me">("/api/me");\n</script>\n')
            (src / "lib/jobs.ts").write_text('apiCall< "POST /api/browser/jobs" >("/api/browser/jobs", { method: "POST", body });\napiCall<\'GET /api/me\'>("/api/me");\n')
            (src / "lib/jobs.test.ts").write_text('apiCall<"GET /api/status">("/api/status");\n')
            with mock.patch.object(feature_map, "ROOT", root):
                found = feature_map.callers(src)
            self.assertEqual(found, {"GET /api/me": ["frontend/src/App.svelte", "frontend/src/lib/jobs.ts"], "POST /api/browser/jobs": ["frontend/src/lib/jobs.ts"]})
            self.assertEqual(feature_map.callers(root / "missing"), {})

    def test_the_shell_is_a_caller_of_api_me_in_the_feature_map(self):
        entries = {entry["route"]: entry for entry in feature_map.build()["routes"]}
        self.assertEqual(entries["GET /api/me"]["frontend"], ["frontend/src/lib/session.svelte.ts"])


if __name__ == "__main__":
    unittest.main()
