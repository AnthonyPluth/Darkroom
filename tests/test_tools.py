import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import api_contract
import feature_map


class ContractAndFeatureMapTests(unittest.TestCase):
    def test_the_committed_contract_is_current(self):
        self.assertEqual((ROOT / "docs/openapi.json").read_text(), api_contract.render())

    def test_the_committed_feature_map_is_current_and_every_route_has_a_test(self):
        self.assertEqual((ROOT / "docs/feature-map.json").read_text(), feature_map.render())
        self.assertEqual(feature_map.untested(feature_map.build()), [])

    def test_healthz_is_in_the_contract(self):
        built = api_contract.build()
        self.assertIn("/healthz", built["paths"])

    def test_every_worker_post_route_declares_its_json_body(self):
        paths = api_contract.build()["paths"]
        for path, methods in paths.items():
            if path.startswith("/internal/browser/jobs/") and "post" in methods:
                with self.subTest(path=path):
                    self.assertIn("requestBody", methods["post"])

    def test_a_body_with_no_fields_becomes_an_empty_record_type(self):
        text = (ROOT / "frontend/src/lib/api-types.ts").read_text()
        self.assertIn("export type HeartbeatBody = Record<string, never>;", text)
        self.assertNotIn("interface HeartbeatBody", text)

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
