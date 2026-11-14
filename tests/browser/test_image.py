import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DOCKERFILE = (ROOT / "Dockerfile.browser").read_text()
REQUIREMENTS = (ROOT / "deploy/browser/requirements.txt").read_text()
SINGLE = (ROOT / "Dockerfile.single").read_text()
SINGLE_REQUIREMENTS = (ROOT / "deploy/single/requirements.txt").read_text()
SANDBOX = "--cap-drop=ALL --security-opt no-new-privileges:true --security-opt seccomp=deploy/browser/seccomp.json"


def requirement_blocks(requirements):
    blocks = [block for block in requirements.replace("\\\n", " ").splitlines() if block.strip()]
    names = [block.split()[0].split("==")[0] for block in blocks]
    if len(names) != len(set(names)):
        raise AssertionError("a requirement is listed twice")
    return dict(zip(names, blocks, strict=True))


def locked_package(name):
    lock = (ROOT / "poetry.lock").read_text()
    start = lock.index(f'[[package]]\nname = "{name}"\n')
    end = lock.find("[[package]]", start + 1)
    return lock[start:end if end != -1 else len(lock)]


class BrowserImageTests(unittest.TestCase):
    def test_a_requirement_listed_twice_is_not_hidden_from_the_hash_test(self):
        with self.assertRaises(AssertionError):
            requirement_blocks("pkg==1.0 --hash=sha256:aa\npkg==2.0\n")

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
        for requirements in (REQUIREMENTS, SINGLE_REQUIREMENTS):
            blocks = list(requirement_blocks(requirements).values())
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


class SingleImageTests(unittest.TestCase):
    def test_the_worker_venv_pins_the_same_playwright_as_the_browser_image(self):
        version = re.search(r"^FROM mcr\.microsoft\.com/playwright/python:v(\d+\.\d+\.\d+)-\w+$", DOCKERFILE, re.M).group(1)
        pinned = re.search(r"^playwright==(\d+\.\d+\.\d+)", SINGLE_REQUIREMENTS, re.M)
        self.assertIsNotNone(pinned, "deploy/single/requirements.txt must pin playwright==<version>")
        self.assertEqual(pinned.group(1), version)
        self.assertIn("COPY deploy/single/requirements.txt /tmp/requirements.txt", SINGLE)
        self.assertIn("/opt/worker/bin/pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt", SINGLE)
        self.assertIn(f"metadata.version('playwright') == '{version}'", SINGLE)
        self.assertIn("import playwright.sync_api", SINGLE)

    def test_only_greenlet_differs_from_the_browser_images_pins(self):
        single = requirement_blocks(SINGLE_REQUIREMENTS)
        browser = requirement_blocks(REQUIREMENTS)
        self.assertEqual(set(single), set(browser))
        for name in sorted(set(single) - {"greenlet"}):
            with self.subTest(package=name):
                self.assertEqual(single[name], browser[name])
        self.assertNotEqual(single["greenlet"], browser["greenlet"])

    def test_greenlet_is_poetry_locks_with_its_python_314_linux_wheels(self):
        locked = locked_package("greenlet")
        version = re.search(r'^version = "(\d+\.\d+\.\d+)"$', locked, re.M).group(1)
        block = requirement_blocks(SINGLE_REQUIREMENTS)["greenlet"]
        self.assertTrue(block.startswith(f"greenlet=={version} "))
        wheels = dict(re.findall(r'file = "(greenlet-[^"]+\.whl)", hash = "sha256:([0-9a-f]{64})"', locked))
        expected = {
            wheels[f"greenlet-{version}-cp314-cp314-manylinux_2_24_x86_64.manylinux_2_28_x86_64.whl"],
            wheels[f"greenlet-{version}-cp314-cp314-manylinux_2_24_aarch64.manylinux_2_28_aarch64.whl"],
        }
        self.assertEqual(set(re.findall(r"--hash=sha256:([0-9a-f]{64})", block)), expected)

    def test_the_worker_has_its_own_venv_on_the_apps_python(self):
        bases = re.findall(r"^FROM (\S+)", SINGLE, re.M)
        self.assertEqual(bases, ["node:22-slim", "python:3.14-slim", "python:3.14-slim", "python:3.14-slim"])
        self.assertIn("python -m venv /opt/worker", SINGLE)
        self.assertIn("COPY --from=worker-deps /opt/worker /opt/worker", SINGLE)
        self.assertIn("COPY --from=deps /app/.venv ./.venv", SINGLE)
        self.assertIn("COPY darkroom_browser ./darkroom_browser", SINGLE)
        self.assertIn("COPY deploy/browser/smoke.sh ./smoke.sh", SINGLE)
        self.assertIn("WORKDIR /opt/worker/app", SINGLE)

    def test_one_user_with_the_unraid_defaults_and_never_root(self):
        self.assertEqual(SINGLE.count("ARG PUID=99"), 1)
        self.assertEqual(SINGLE.count("ARG PGID=100"), 1)
        self.assertIn('if ! getent group "$PGID" >/dev/null; then groupadd --system --gid "$PGID" darkroom-share; fi', SINGLE)
        self.assertEqual(len(re.findall(r"\buseradd\b", SINGLE)), 1)
        self.assertIn('useradd --system --uid "$PUID" --gid "$PGID"', SINGLE)
        users = re.findall(r"^USER (\S+)$", SINGLE, re.M)
        self.assertEqual(users, ["darkroom"])
        runtime = SINGLE[SINGLE.index("AS runtime"):]
        self.assertLess(runtime.index("playwright install"), runtime.index("USER darkroom"), "browsers are installed as root")
        self.assertLess(runtime.index("useradd"), runtime.index("USER darkroom"))
        self.assertNotRegex(runtime[runtime.index("USER darkroom"):], r"(?m)^RUN ")

    def test_chrome_on_amd64_only_and_chromium_without_the_headless_shell(self):
        self.assertIn("/opt/worker/bin/python -m playwright install --with-deps --no-shell chromium", SINGLE)
        self.assertIn('if [ "$(dpkg --print-architecture)" = "amd64" ]; then /opt/worker/bin/python -m playwright install --with-deps chrome; fi', SINGLE)
        self.assertNotIn("firefox", SINGLE)
        self.assertNotIn("webkit", SINGLE)
        self.assertNotIn("--no-sandbox", SINGLE)
        self.assertNotRegex(SINGLE, r"curl[^\n]*\|\s*(ba)?sh")

    def test_it_serves_the_app_on_8000_only_under_tini(self):
        self.assertEqual(re.findall(r"^EXPOSE .*$", SINGLE, re.M), ["EXPOSE 8000"])
        self.assertIn('ENTRYPOINT ["/usr/bin/tini", "--"]', SINGLE)
        self.assertIn('CMD ["python", "run.py", "serve"]', SINGLE)
        self.assertIn("http://127.0.0.1:8000/healthz", SINGLE)

    def test_ci_checks_healthz_and_both_browsers_in_it_under_the_deployment_policy(self):
        workflow = (ROOT / ".github/workflows/docker.yml").read_text()
        job = workflow[workflow.index("\n  single:\n"):workflow.index("\n  publish:\n")]
        self.assertIn("file: Dockerfile.single", job)
        self.assertIn("tags: darkroom-single:smoke", job)
        run_lines = [line.strip() for line in job.splitlines() if line.strip().startswith("docker run")]
        self.assertEqual(len(run_lines), 2)
        healthz, smoke = run_lines
        self.assertIn(SANDBOX, healthz)
        self.assertIn("-p 8765:8000 darkroom-single:smoke", healthz)
        self.assertIn("curl -fsS http://127.0.0.1:8765/healthz", job)
        self.assertIn(SANDBOX, smoke)
        self.assertIn("--shm-size=1g", smoke)
        self.assertIn("-e PATH=/opt/worker/bin:", smoke)
        self.assertIn("test -x /opt/google/chrome/chrome && bash /opt/worker/app/smoke.sh", smoke)
        self.assertNotIn("platforms:", job)


if __name__ == "__main__":
    unittest.main()
