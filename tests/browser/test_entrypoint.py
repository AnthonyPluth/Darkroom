import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
ENTRYPOINT = ROOT / "deploy/browser/entrypoint.sh"
TOKEN = "worker-token-0123456789abcdef0123456789"
SECRET = "viewer-secret-canary-0123456789abcdef0123"

RECORD_ARGS = '#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" > "$STUB_RECORD/.{name}"\nmv "$STUB_RECORD/.{name}" "$STUB_RECORD/{name}"\n'
WORKER = """#!/usr/bin/env bash
for _ in $(seq 1 200); do
  if [ -s "$STUB_RECORD/websockify" ]; then break; fi
  sleep 0.05
done
printf '%s\\n' "$@" > "$STUB_RECORD/python"
env > "$STUB_RECORD/worker-env"
"""
STUBS = {
    "Xvfb": "#!/usr/bin/env bash\nexit 0\n",
    "xdpyinfo": "#!/usr/bin/env bash\nexit 0\n",
    "x11vnc": RECORD_ARGS.format(name="x11vnc"),
    "websockify": RECORD_ARGS.format(name="websockify"),
    "python": WORKER,
}


class Started:
    def __init__(self, returncode, stderr, record):
        self.returncode = returncode
        self.stderr = stderr
        self.record = record

    def args(self, name):
        path = self.record / name
        return path.read_text().splitlines() if path.exists() else None

    def after(self, name, flag):
        args = self.args(name)
        return args[args.index(flag) + 1]


class EntrypointTests(unittest.TestCase):
    def start(self, **extra):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        folder = Path(directory.name)
        stubs, record, photos = folder / "bin", folder / "record", folder / "photos"
        for path in (stubs, record, photos):
            path.mkdir()
        for name, body in STUBS.items():
            (stubs / name).write_text(body)
            (stubs / name).chmod(0o755)
        env = {"PATH": f"{stubs}:/usr/bin:/bin", "STUB_RECORD": str(record), "PHOTOS_ROOT": str(photos), "DARKROOM_BROWSER_TOKEN": TOKEN, **extra}
        done = subprocess.run(["bash", str(ENTRYPOINT)], env=env, capture_output=True, text=True, timeout=60, check=False)
        return Started(done.returncode, done.stderr, record)

    def test_x11vnc_listens_on_the_containers_loopback_only(self):
        started = self.start(DARKROOM_VNC_SECRET=SECRET)
        self.assertEqual(started.returncode, 0, started.stderr)
        x11vnc = started.args("x11vnc")
        self.assertIn("-localhost", x11vnc)
        self.assertEqual(started.after("x11vnc", "-rfbport"), "5900")
        self.assertNotIn("-listen", x11vnc)
        self.assertEqual(started.args("websockify")[-1], "127.0.0.1:5900")

    def test_websockify_demands_the_viewer_secret_for_the_page_and_the_socket(self):
        started = self.start(DARKROOM_VNC_SECRET=SECRET)
        self.assertEqual(started.returncode, 0, started.stderr)
        websockify = started.args("websockify")
        self.assertEqual(started.after("websockify", "--auth-plugin"), "BasicHTTPAuth")
        self.assertEqual(started.after("websockify", "--auth-source"), f"darkroom:{SECRET}")
        self.assertIn("--web-auth", websockify)
        self.assertIn("--web=/usr/share/novnc", websockify)
        self.assertEqual(websockify[-2], "6080")
        self.assertNotIn("viewer locked", started.stderr)

    def test_the_worker_never_sees_the_viewer_secret(self):
        started = self.start(DARKROOM_VNC_SECRET=SECRET)
        self.assertEqual(started.args("python"), ["-m", "darkroom_browser"])
        worker_env = (started.record / "worker-env").read_text()
        self.assertNotIn("DARKROOM_VNC_SECRET", worker_env)
        self.assertNotIn(SECRET, worker_env)
        self.assertIn(f"DARKROOM_BROWSER_TOKEN={TOKEN}", worker_env)
        self.assertNotIn(SECRET, started.stderr)

    def test_a_missing_or_weak_secret_locks_the_viewer_with_one_nobody_knows(self):
        for name, extra in (("unset", {}), ("empty", {"DARKROOM_VNC_SECRET": ""}), ("short", {"DARKROOM_VNC_SECRET": "too-short-secret"}),
                            ("not printable", {"DARKROOM_VNC_SECRET": "a" * 20 + " " + "b" * 20})):
            with self.subTest(name):
                started = self.start(**extra)
                self.assertEqual(started.returncode, 0, started.stderr)
                self.assertIn('"event": "viewer locked"', started.stderr)
                source = started.after("websockify", "--auth-source")
                self.assertRegex(source, r"^darkroom:[0-9a-f]{64}$")
                self.assertNotIn(source.split(":", 1)[1], started.stderr)
                self.assertIn("--web-auth", started.args("websockify"))
                self.assertNotIn("DARKROOM_VNC_SECRET", (started.record / "worker-env").read_text())

    def test_the_locked_viewers_secret_changes_at_every_start(self):
        first = self.start().after("websockify", "--auth-source")
        second = self.start().after("websockify", "--auth-source")
        self.assertNotEqual(first, second)

    def test_no_token_still_refuses_to_start_before_any_listener(self):
        started = self.start(DARKROOM_BROWSER_TOKEN="short", DARKROOM_VNC_SECRET=SECRET)
        self.assertEqual(started.returncode, 1)
        self.assertIn("worker refuses to start", started.stderr)
        self.assertIsNone(started.args("x11vnc"))
        self.assertIsNone(started.args("websockify"))


class ViewerPortTests(unittest.TestCase):
    def test_only_the_novnc_port_is_exposed(self):
        dockerfile = (ROOT / "Dockerfile.browser").read_text()
        self.assertEqual(re.findall(r"^EXPOSE .*$", dockerfile, re.M), ["EXPOSE 6080"])
        compose = (ROOT / "docker-compose.yml").read_text()
        browser = compose[compose.index("  darkroom-browser:"):]
        self.assertIn('    expose:\n      - "6080"\n    volumes:', browser)
        self.assertNotIn("5900", compose)
        self.assertNotRegex(browser, r"(?m)^    ports:")

    def test_compose_passes_the_viewer_secret_to_the_browser_service_only(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        browser_start = compose.index("  darkroom-browser:")
        self.assertIn("DARKROOM_VNC_SECRET: ${DARKROOM_VNC_SECRET:-}", compose[browser_start:])
        self.assertNotIn("DARKROOM_VNC_SECRET", compose[:browser_start])
        self.assertIn("DARKROOM_VNC_SECRET=", (ROOT / ".env.example").read_text())


if __name__ == "__main__":
    unittest.main()
