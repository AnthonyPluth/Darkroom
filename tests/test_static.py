import tempfile
import unittest
from pathlib import Path

from darkroom.server import static


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


if __name__ == "__main__":
    unittest.main()
