import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DOCKERFILE = (ROOT / "Dockerfile.browser").read_text()
REQUIREMENTS = (ROOT / "deploy/browser/requirements.txt").read_text()


class BrowserImageTests(unittest.TestCase):
    def test_both_images_and_compose_default_to_the_unraid_user_and_group(self):
        for name in ("Dockerfile", "Dockerfile.browser"):
            contents = (ROOT / name).read_text()
            with self.subTest(image=name):
                self.assertIn("ARG PUID=99", contents)
                self.assertIn("ARG PGID=100", contents)
                self.assertIn('getent group "$PGID"', contents)
                self.assertIn('--uid "$PUID" --gid "$PGID"', contents)
        compose = (ROOT / "docker-compose.yml").read_text()
        self.assertEqual(compose.count('user: "${PUID:-99}:${PGID:-100}"'), 2)
        self.assertEqual(compose.count("PUID: ${PUID:-99}"), 2)
        self.assertEqual(compose.count("PGID: ${PGID:-100}"), 2)

    def test_the_seccomp_profile_allows_chromiums_namespace_and_chroot_sandbox(self):
        profile = json.loads((ROOT / "deploy/browser/seccomp.json").read_text())
        allowed = {name for rule in profile["syscalls"] if rule["action"] == "SCMP_ACT_ALLOW" and not rule.get("args") for name in rule["names"]}
        self.assertTrue({"clone", "unshare", "setns", "chroot", "seccomp", "prctl"} <= allowed)
        self.assertEqual(profile["defaultAction"], "SCMP_ACT_ERRNO")
        self.assertNotIn("mount", allowed)
        self.assertNotIn("ptrace", allowed)

    def test_ci_launches_a_real_renderer_under_the_deployment_policy(self):
        workflow = (ROOT / ".github/workflows/docker.yml").read_text()
        self.assertIn("--security-opt seccomp=deploy/browser/seccomp.json", workflow)
        self.assertIn("--cap-drop=ALL --security-opt no-new-privileges:true", workflow)
        self.assertIn("/app/smoke.sh", workflow)
        smoke = (ROOT / "deploy/browser/smoke.sh").read_text()
        self.assertIn("for channel in chrome chromium", smoke)
        self.assertIn("browser.content()", smoke)
        self.assertIn("chrome_launch_failed", smoke)

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
        self.assertIn("--ignore-installed", DOCKERFILE, "the base image ships its own copies of some packages, which pip cannot uninstall")

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

    def test_google_chrome_is_installed_as_root_on_amd64_only(self):
        install = re.search(r"^RUN .*playwright install --with-deps chrome.*$", DOCKERFILE, re.M)
        self.assertIsNotNone(install, "the image installs Google Chrome with the pinned Playwright's own installer")
        self.assertIn('dpkg --print-architecture)" = "amd64"', DOCKERFILE, "Chrome for Linux is amd64 only")
        self.assertLess(DOCKERFILE.index("--require-hashes"), install.start(), "the pinned playwright package provides the installer")
        self.assertLess(install.start(), DOCKERFILE.index("USER darkroom-browser"), "Chrome is installed as root")
        self.assertNotIn("--no-sandbox", DOCKERFILE)
        self.assertNotRegex(DOCKERFILE, r"curl[^\n]*\|\s*(ba)?sh")

    def test_the_image_stays_non_root(self):
        self.assertIn("USER darkroom-browser", DOCKERFILE)
        self.assertLess(DOCKERFILE.index("--require-hashes"), DOCKERFILE.index("USER darkroom-browser"), "packages are installed before dropping root")


if __name__ == "__main__":
    unittest.main()
