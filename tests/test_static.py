import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from darkroom.server import static
from tests.shared import fetch, settings_from, start_server

ROOT = Path(__file__).resolve().parent.parent
INDEX = b'<!doctype html><title>Darkroom</title><div id="app"></div><script type="module" src="/assets/app.js"></script>'


class StaticTests(unittest.TestCase):
    def test_serves_a_file_with_its_type_and_the_security_headers(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "index.html").write_text("<p>home</p>")
            (root / "app.js").write_text("1")
            response = static.serve("/app.js", root)
            self.assertEqual(response.status, 200)
            self.assertIn("javascript", response.content_type)
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    def test_an_unknown_path_falls_back_to_the_index(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "index.html").write_text("<p>home</p>")
            self.assertEqual(static.serve("/people", root).body, b"<p>home</p>")

    def test_without_an_index_an_unknown_path_is_404(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(static.serve("/people", Path(folder)).status, 404)

    def test_traversal_never_leaves_the_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "site"
            root.mkdir()
            (Path(folder) / "secret.txt").write_text("secret")
            self.assertEqual(static.serve("/../secret.txt", root).status, 404)

    def test_a_missing_asset_is_404_rather_than_the_index(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "index.html").write_text("<p>home</p>")
            response = static.serve("/assets/gone-1234.js", root)
            self.assertEqual(response.status, 404)
            self.assertEqual(json.loads(response.body), {"error": "not found"})

    def test_the_built_app_lives_in_its_own_folder(self):
        self.assertEqual(static.ROOT, ROOT / "darkroom/static/app")


class ServedAppTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        (root / "assets").mkdir()
        (root / "index.html").write_bytes(INDEX)
        (root / "assets/app.js").write_text("export {};")
        patch = mock.patch.object(static, "ROOT", root)
        patch.start()
        self.addCleanup(patch.stop)
        settings = settings_from(OIDC_ISSUER="https://id.example", OIDC_CLIENT_ID="darkroom", DARKROOM_PUBLIC_URL="https://darkroom.example")
        self.base, _ = start_server(self, settings)

    def test_the_index_is_served_at_the_root_without_signing_in(self):
        status, headers, body = fetch(self.base, "GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(body, INDEX)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_the_index_carries_a_csp_with_no_inline_script_and_same_origin_connect(self):
        _, headers, _ = fetch(self.base, "GET", "/")
        policy = {part.split()[0]: part.split()[1:] for part in headers["Content-Security-Policy"].split("; ")}
        self.assertEqual(policy["script-src"], ["'self'"])
        self.assertEqual(policy["connect-src"], ["'self'"])
        self.assertEqual(policy["frame-ancestors"], ["'none'"])

    def test_a_page_address_serves_the_index_so_the_app_can_route_it(self):
        for path in ("/people", "/exports", "/review"):
            with self.subTest(path=path):
                status, _, body = fetch(self.base, "GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(body, INDEX)

    def test_the_built_assets_are_served(self):
        status, headers, body = fetch(self.base, "GET", "/assets/app.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers["Content-Type"])
        self.assertEqual(body, b"export {};")

    def test_unknown_api_and_internal_paths_answer_404_json_not_the_index(self):
        for method, path in (("GET", "/api/nothing"), ("GET", "/api/people/1"), ("GET", "/internal/nothing"), ("POST", "/"), ("POST", "/api/nothing")):
            with self.subTest(method=method, path=path):
                status, headers, body = fetch(self.base, method, path, {"Origin": "https://darkroom.example"}, b"{}" if method == "POST" else None)
                self.assertEqual(status, 404)
                self.assertEqual(headers["Content-Type"], "application/json")
                self.assertEqual(json.loads(body), {"error": "not found"})

    def test_a_session_route_still_needs_sign_in(self):
        status, _, body = fetch(self.base, "GET", "/api/me")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {"error": "sign in required"})


class ServerPageTests(unittest.TestCase):
    def test_a_server_page_uses_only_the_signed_off_dark_colours(self):
        from pathlib import Path

        from darkroom.server import common

        theme = (Path(__file__).resolve().parent.parent / "frontend/src/app.css").read_text()
        tokens = set(re.findall(r"--color-[\w-]+:\s*(#[0-9a-f]{6});", theme))
        used = set(re.findall(r"#[0-9a-f]{6}", common.PAGE_STYLE))
        self.assertTrue(used)
        self.assertEqual(used - tokens, set())
        self.assertIn("color-scheme:dark", common.PAGE_STYLE)

    def test_an_error_page_has_an_alert_and_an_ordinary_one_does_not(self):
        from darkroom.server import common

        failed = common.page(403, "Not allowed", "No <b>way</b>.", ("/auth/login", "Back"), hint="Ask someone.").body.decode()
        self.assertIn('role="alert"', failed)
        self.assertIn("No &lt;b&gt;way&lt;/b&gt;.", failed)
        self.assertIn("Ask someone.", failed)
        fine = common.page(200, "Signed out", "Bye.").body.decode()
        self.assertNotIn('role="alert"', fine)
        self.assertNotIn('class="button"', fine)

    def test_a_server_page_allows_its_own_style_and_nothing_else_inline(self):
        import base64
        import hashlib

        from darkroom.server import common

        response = common.page(200, "Signed out", "You have signed out of Darkroom.", ("/auth/login", "Sign in again"))
        style = re.search(r"<style>([\s\S]*?)</style>", response.body.decode())
        assert style is not None
        digest = base64.b64encode(hashlib.sha256(style.group(1).encode()).digest()).decode()
        policy = {part.split()[0]: part.split()[1:] for part in response.headers["Content-Security-Policy"].split("; ")}
        self.assertEqual(policy["style-src"], [f"'sha256-{digest}'"])
        self.assertEqual(policy["script-src"], ["'self'"])


class FrontendIndexTests(unittest.TestCase):
    def test_the_web_apps_index_has_no_inline_script(self):
        source = (ROOT / "frontend/index.html").read_text()
        scripts = re.findall(r"<script\b([^>]*)>([\s\S]*?)</script>", source)
        self.assertEqual(len(scripts), 1)
        attributes, body = scripts[0]
        self.assertIn('type="module"', attributes)
        self.assertIn("src=", attributes)
        self.assertEqual(body.strip(), "")
        self.assertIsNone(re.search(r"\son[a-z]+=", source))


if __name__ == "__main__":
    unittest.main()
