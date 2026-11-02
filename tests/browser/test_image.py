import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DOCKERFILE = (ROOT / "Dockerfile.browser").read_text()
REQUIREMENTS = (ROOT / "deploy/browser/requirements.txt").read_text()


class BrowserImageTests(unittest.TestCase):
    def test_the_playwright_package_is_pinned_to_the_image_version(self):
        image = re.search(r"^FROM mcr\.microsoft\.com/playwright/python:v(\d+\.\d+\.\d+)-\w+$", DOCKERFILE, re.M)
        self.assertIsNotNone(image, "the base image must be a pinned Playwright Python image")
        version = image.group(1)
        pinned = re.search(r"^playwright==(\d+\.\d+\.\d+)", REQUIREMENTS, re.M)
        self.assertIsNotNone(pinned, "deploy/browser/requirements.txt must pin playwright==<version>")
        self.assertEqual(pinned.group(1), version, "the pip pin and the image tag must name the same Playwright release")
        self.assertIn(f"metadata.version('playwright') == '{version}'", DOCKERFILE, "the build must check the installed version")
        self.assertIn("import playwright.sync_api", DOCKERFILE, "the build must import the package it installed")
        self.assertIn("--require-hashes", DOCKERFILE)

    def test_every_requirement_is_pinned_with_hashes(self):
        blocks = [block for block in REQUIREMENTS.replace("\\\n", " ").splitlines() if block.strip()]
        self.assertTrue(blocks)
        for block in blocks:
            with self.subTest(block=block.split()[0]):
                self.assertRegex(block.split()[0], r"^[a-z][a-z0-9-]*==\d+\.\d+\.\d+$")
                self.assertRegex(block, r"--hash=sha256:[0-9a-f]{64}")

    def test_the_pyproject_extra_admits_the_pinned_version(self):
        pinned = re.search(r"^playwright==(\d+)\.(\d+)\.", REQUIREMENTS, re.M)
        major, minor = int(pinned.group(1)), int(pinned.group(2))
        extra = re.search(r'browser = \["playwright \(>=(\d+)\.(\d+),<(\d+)\.0\)"\]', (ROOT / "pyproject.toml").read_text())
        self.assertIsNotNone(extra)
        self.assertGreaterEqual((major, minor), (int(extra.group(1)), int(extra.group(2))))
        self.assertLess(major, int(extra.group(3)))

    def test_the_image_stays_non_root(self):
        self.assertIn("USER darkroom-browser", DOCKERFILE)
        self.assertLess(DOCKERFILE.index("--require-hashes"), DOCKERFILE.index("USER darkroom-browser"), "packages are installed before dropping root")


if __name__ == "__main__":
    unittest.main()
