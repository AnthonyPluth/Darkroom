import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from tests.shared import fetch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import verify

REAL_POPEN = subprocess.Popen
NEVER = {"OIDC_ISSUER": "https://idp.example", "OIDC_CLIENT_ID": "real", "OIDC_CLIENT_SECRET": "oidc-secret-canary-1a2b3c4d", "IMMICH_URL": "https://immich",
         "IMMICH_ADMIN_KEY": "immich-key-canary-5e6f7a8b", "DARKROOM_BROWSER_TOKEN": "browser-token-canary-9c0d1e2f", "DATABASE_URL": "postgresql://real",
         "DARKROOM_ALLOW_NO_AUTH": "yes-canary", "VNC_URL": "https://vnc.example", "HOME_ASSISTANT_TOKEN": "ha-canary-7e8f9a0b", "GITHUB_TOKEN": "ghp_canary",
         "AWS_SECRET_ACCESS_KEY": "aws-canary", "ANTHROPIC_API_KEY": "sk-ant-canary", "SOMETHING_NEW_TOKEN": "t", "CANARY_SECRET": "parent-secret-canary-0f1e2d3c",
         "ALL_PROXY": "socks5://proxy", "all_proxy": "socks5://proxy"}
REPLACED = {"DARKROOM_SECRET_KEY": "real-secret-canary-3a4b5c6d", "DARKROOM_DATA_DIR": "/real/data", "DARKROOM_PHOTOS_ROOT": "/real/photos",
            "DARKROOM_HOST": "0.0.0.0", "DARKROOM_PUBLIC_URL": "https://darkroom.example", "DARKROOM_PORT": "80", "HTTPS_PROXY": "http://proxy:3128"}
SECRETS = {**NEVER, **REPLACED}
NEEDED = {"PATH": "/bin", "HOME": "/root", "LANG": "C.UTF-8", "LC_ALL": "C", "PYTHONPATH": "/x", "PLAYWRIGHT_BROWSERS_PATH": "/opt/pw", "VIRTUAL_ENV": "/venv",
          "SSL_CERT_FILE": "/etc/ssl/cert.pem", "XDG_CACHE_HOME": "/cache"}


class CleanEnvTests(unittest.TestCase):
    def test_the_demo_server_gets_an_allowlist_never_a_credential_or_the_real_data_settings(self):
        env = verify.clean_env("/tmp/demo", {**SECRETS, **NEEDED})
        self.assertFalse(set(NEVER) & set(env), set(NEVER) & set(env))
        self.assertEqual({k: env[k] for k in NEEDED}, NEEDED)
        for name, value in SECRETS.items():
            self.assertNotIn(value, env.values(), name)
        with mock.patch.dict(os.environ, SECRETS, clear=False):
            frozen = verify.frozen_env(verify.clean_env("/tmp/demo"), "/lib/f.so")
        self.assertFalse(set(NEVER) & set(frozen))
        self.assertFalse(set(SECRETS.values()) & set(frozen.values()))

    def test_the_demo_server_is_told_exactly_where_its_data_is_and_that_it_is_a_demo_on_loopback(self):
        env = verify.clean_env("/tmp/demo", {"PATH": "/bin"})
        self.assertEqual(env, {"PATH": "/bin", "DARKROOM_DEMO": "1", "DARKROOM_HOST": "127.0.0.1", "DARKROOM_DATA_DIR": "/tmp/demo/data",
                               "DARKROOM_PHOTOS_ROOT": "/tmp/demo/photos", "DARKROOM_PUBLIC_URL": "http://127.0.0.1", "DARKROOM_SECRET_KEY": verify.DEMO_SECRET,
                               "PYTHONUNBUFFERED": "1", "HTTP_PROXY": verify.NO_OUTSIDE, "HTTPS_PROXY": verify.NO_OUTSIDE, "http_proxy": verify.NO_OUTSIDE,
                               "https_proxy": verify.NO_OUTSIDE, "NO_PROXY": verify.LOOPBACK_ONLY, "no_proxy": verify.LOOPBACK_ONLY})
        with_port = verify.clean_env("/tmp/demo", {}, port=8123)
        self.assertEqual((with_port["DARKROOM_PORT"], with_port["DARKROOM_PUBLIC_URL"]), ("8123", "http://127.0.0.1:8123"))

    def test_the_demo_server_reaches_nobody_outside_loopback(self):
        base = {"HTTPS_PROXY": "http://proxy:3128", "HTTP_PROXY": "http://proxy:3128", "NO_PROXY": "everything", "all_proxy": "socks5://proxy"}
        env = verify.clean_env("/tmp/demo", base)
        self.assertEqual({env[k] for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")}, {"http://127.0.0.1:9"})
        self.assertEqual((env["NO_PROXY"], env["no_proxy"]), ("127.0.0.1,localhost", "127.0.0.1,localhost"))
        self.assertFalse({"ALL_PROXY", "all_proxy"} & set(env))


class FrozenClockTests(unittest.TestCase):
    def test_the_library_is_found_where_debian_puts_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = os.path.join(tmp, "libfaketime.so.1")
            self.assertIsNone(verify.find_faketime((library,)))
            open(library, "w").close()
            self.assertEqual(verify.find_faketime(("/nowhere/x.so", library)), library)

    def test_the_server_and_the_seed_run_on_one_fixed_noon_in_utc(self):
        env = verify.frozen_env({"PATH": "/bin", "TZ": "America/Denver"}, "/lib/libfaketime.so.1")
        self.assertEqual((env["LD_PRELOAD"], env["FAKETIME"], env["TZ"], env["FAKETIME_DONT_FAKE_MONOTONIC"]), ("/lib/libfaketime.so.1", verify.FROZEN_NOW, "UTC", "1"))
        self.assertEqual(env["PATH"], "/bin")
        self.assertEqual(verify.FROZEN_NOW_ISO, verify.FROZEN_NOW.replace(" ", "T") + "Z")
        self.assertEqual(datetime.fromisoformat(verify.FROZEN_NOW_ISO).hour, 12)


class CommandTests(unittest.TestCase):
    def test_seed_runs_run_py_demo_frozen_in_a_new_folder(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(verify, "find_faketime", return_value="/lib/f.so"), \
                mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)) as ran, mock.patch("builtins.print"):
            target = os.path.join(tmp, "seed")
            self.assertEqual(verify.seed(["--data", target]), 0)
            argv, env = ran.call_args.args[0], ran.call_args.kwargs["env"]
            self.assertEqual(argv[1:], [os.path.join(verify.ROOT, "run.py"), "demo", "--data", target])
            self.assertEqual((env["FAKETIME"], env["DARKROOM_DATA_DIR"], env["LD_PRELOAD"], env["DARKROOM_DEMO"]), (verify.FROZEN_NOW, os.path.join(target, "data"), "/lib/f.so", "1"))
            self.assertTrue(os.path.isdir(target))

    def test_seed_never_goes_on_top_of_data_that_is_there_and_needs_the_clock(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("subprocess.run") as ran, mock.patch("sys.stderr"):
            with mock.patch.object(verify, "find_faketime", return_value="/lib/f.so"):
                self.assertEqual(verify.seed(["--data", tmp]), 2)
            with mock.patch.object(verify, "find_faketime", return_value=None):
                self.assertEqual(verify.seed(["--data", os.path.join(tmp, "new")]), 2)
            ran.assert_not_called()

    def test_a_seed_that_fails_leaves_no_folder(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(verify, "find_faketime", return_value="/lib/f.so"), \
                mock.patch("subprocess.run", return_value=mock.Mock(returncode=1, stdout="", stderr="boom")), mock.patch("sys.stderr"):
            self.assertEqual(verify.seed(["--data", os.path.join(tmp, "seed")]), 2)
            self.assertFalse(os.path.exists(os.path.join(tmp, "seed")))

    def test_serve_copies_the_seed_runs_the_server_on_demo_data_only_and_removes_the_copy_after(self):
        copied = {}

        def run_server(command, cwd, env, stdout, stderr):
            with open(os.path.join(env["DARKROOM_DATA_DIR"], "darkroom.db")) as f:
                copied["db"] = f.read()
            copied["log"] = os.path.exists(os.path.join(os.path.dirname(env["DARKROOM_DATA_DIR"]), "server.log"))
            return mock.Mock(spec=REAL_POPEN, wait=mock.Mock(return_value=7))

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(verify, "find_faketime", return_value="/lib/f.so"), \
                mock.patch.object(verify, "die_with_parent") as dies, mock.patch("subprocess.Popen", side_effect=run_server) as popen, \
                mock.patch("signal.signal") as handlers, mock.patch.dict("os.environ", SECRETS):
            seed = os.path.join(tmp, "seed", "data")
            os.makedirs(seed)
            with open(os.path.join(seed, "darkroom.db"), "w") as f:
                f.write("seeded")
            data = os.path.join(tmp, "live")
            self.assertEqual(verify.serve(["--parent", "42", "--seed", os.path.join(tmp, "seed"), "--data", data, "--port", "9200"]), 7)
            dies.assert_called_once_with(42)
            self.assertEqual(copied, {"db": "seeded", "log": True})
            self.assertFalse(os.path.exists(data))
            command, env = popen.call_args.args[0], popen.call_args.kwargs["env"]
            self.assertEqual(command[1:], [os.path.join(verify.ROOT, "run.py"), "serve"])
            self.assertEqual((env["DARKROOM_DATA_DIR"], env["DARKROOM_PORT"], env["FAKETIME"], env["HTTPS_PROXY"], env["DARKROOM_DEMO"], env["DARKROOM_HOST"]),
                             (os.path.join(data, "data"), "9200", verify.FROZEN_NOW, verify.NO_OUTSIDE, "1", "127.0.0.1"))
            self.assertFalse(set(NEVER) & set(env))
            self.assertFalse(set(SECRETS.values()) & set(env.values()))
            self.assertEqual({call.args[0] for call in handlers.call_args_list}, {signal.SIGTERM, signal.SIGINT})

    def test_a_server_that_cannot_start_leaves_no_copy_and_no_open_log(self):
        logs = []
        real_fdopen = os.fdopen

        def remembering(*args, **kwargs):
            opened = real_fdopen(*args, **kwargs)
            logs.append(opened)
            return opened

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(verify, "find_faketime", return_value="/lib/f.so"), \
                mock.patch.object(verify, "die_with_parent"), mock.patch("subprocess.Popen", side_effect=OSError(2, "No such file")), \
                mock.patch("os.fdopen", side_effect=remembering), mock.patch("signal.signal"), mock.patch("sys.stderr"):
            seed = os.path.join(tmp, "seed")
            os.makedirs(os.path.join(seed, "data"))
            data = os.path.join(tmp, "live")
            self.assertEqual(verify.serve(["--parent", "42", "--seed", seed, "--data", data, "--port", "9200"]), 2)
            self.assertFalse(os.path.exists(data))
            self.assertEqual([log.closed for log in logs], [True])
            with mock.patch("subprocess.Popen", return_value=mock.Mock(spec=REAL_POPEN, wait=mock.Mock(return_value=0))):
                self.assertEqual(verify.serve(["--parent", "42", "--seed", seed, "--data", data, "--port", "9200"]), 0)

    def test_serve_refuses_a_folder_that_exists_and_does_not_remove_it(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(verify, "find_faketime", return_value="/lib/f.so"), \
                mock.patch.object(verify, "die_with_parent"), mock.patch("subprocess.Popen") as popen, mock.patch("signal.signal"), mock.patch("sys.stderr"):
            seed = os.path.join(tmp, "seed")
            os.mkdir(seed)
            self.assertEqual(verify.serve(["--parent", "42", "--seed", seed, "--data", tmp, "--port", "9200"]), 2)
            self.assertTrue(os.path.isdir(tmp))
            popen.assert_not_called()

    def test_a_child_whose_parent_is_already_gone_is_sent_the_signal(self):
        with mock.patch("os.getppid", return_value=1), mock.patch("os.kill") as kill:
            verify.die_with_parent(parent=4242)
        kill.assert_called_once_with(os.getpid(), signal.SIGTERM)
        with mock.patch("os.getppid", return_value=4242), mock.patch("os.kill") as kill:
            verify.die_with_parent(parent=4242)
        kill.assert_not_called()

    def test_a_server_that_dies_or_never_answers_is_reported(self):
        server = mock.Mock(spec=REAL_POPEN)
        server.poll.return_value = 3
        server.returncode = 3
        with self.assertRaisesRegex(RuntimeError, "exited with code 3"):
            verify.wait_ready("http://127.0.0.1:1", server, timeout=5)
        server.poll.return_value = None
        with self.assertRaisesRegex(RuntimeError, r"http://127\.0\.0\.1:1 after 0 seconds"):
            verify.wait_ready("http://127.0.0.1:1", server, timeout=0)

    def test_the_readiness_check_skips_the_system_proxy(self):
        server = mock.Mock(spec=REAL_POPEN)
        server.poll.return_value = None
        with mock.patch.dict("os.environ", {"HTTP_PROXY": "http://proxy:3128", "http_proxy": "http://proxy:3128"}), \
                mock.patch("urllib.request.build_opener") as built:
            verify.wait_ready("http://127.0.0.1:8123", server, timeout=5)
        (handler,) = built.call_args.args
        self.assertEqual(handler.proxies, {})
        built.return_value.open.assert_called_once_with("http://127.0.0.1:8123/healthz", timeout=2)

    def test_status_reports_the_clock_the_tools_and_the_load(self):
        with mock.patch.object(verify, "busy_now", return_value=2.0), mock.patch.object(verify, "find_faketime", return_value="/lib/f.so"):
            got = verify.status()
        self.assertEqual((got["frozen_now"], got["frozen_now_iso"], got["busy"], got["busy_limit"], got["libfaketime"]),
                         (verify.FROZEN_NOW, verify.FROZEN_NOW_ISO, True, verify.BUSY, "/lib/f.so"))
        self.assertIn("exiftool", got)
        json.dumps(got)

    def test_the_commands_run_from_the_command_line(self):
        script = str(ROOT / "tools" / "verify.py")
        out = subprocess.run([sys.executable, script, "status"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        self.assertEqual(json.loads(out)["frozen_now"], verify.FROZEN_NOW)
        usage = subprocess.run([sys.executable, script], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(usage.returncode, 1)
        self.assertIn("usage: python tools/verify.py run", usage.stderr)


class FrozenDemoServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        library = verify.find_faketime()
        assert library, verify.missing_faketime_message()
        cls.folder = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.folder.cleanup)
        cls.seed = os.path.join(cls.folder.name, "seed")
        cls.live = os.path.join(cls.folder.name, "live")
        cls.parent_env = {**os.environ, "CANARY_SECRET": SECRETS["CANARY_SECRET"], "OIDC_ISSUER": SECRETS["OIDC_ISSUER"], "OIDC_CLIENT_ID": "real"}
        script = str(ROOT / "tools" / "verify.py")
        seeded = subprocess.run([sys.executable, script, "seed", "--data", cls.seed], cwd=ROOT, env=cls.parent_env, capture_output=True, text=True, timeout=300)
        assert seeded.returncode == 0, seeded.stdout + seeded.stderr
        cls.port = verify.free_port()
        cls.server = REAL_POPEN([sys.executable, script, "serve", "--parent", str(os.getpid()), "--seed", cls.seed, "--data", cls.live, "--port", str(cls.port)],
                                cwd=ROOT, env=cls.parent_env)
        cls.addClassCleanup(cls.stop)
        cls.base = f"http://127.0.0.1:{cls.port}"
        verify.wait_ready(cls.base, cls.server, timeout=90)

    @classmethod
    def stop(cls):
        if cls.server.poll() is None:
            cls.server.terminate()
            try:
                cls.server.wait(10)
            except subprocess.TimeoutExpired:
                cls.server.kill()

    def session(self):
        with open(os.path.join(verify.data_dir(self.seed), verify.DEMO_SESSION_FILE)) as f:
            saved = json.load(f)
        return {"Cookie": f"{saved['cookie']}={saved['token']}"}

    def test_the_seed_ran_on_the_frozen_clock(self):
        frozen = datetime.fromisoformat(verify.FROZEN_NOW_ISO).timestamp()
        with sqlite3.connect(os.path.join(verify.data_dir(self.seed), "darkroom.db")) as connection:
            (created,) = connection.execute("select created from auth_sessions").fetchone()
            (opened,) = connection.execute("select opened from alerts where kind = 'sign_in_needed'").fetchone()
        self.assertEqual(created, frozen)
        self.assertEqual(opened, frozen - 2 * 3600)

    def server_pid(self):
        children = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat") as f:
                    fields = f.read().rpartition(")")[2].split()
            except OSError:
                continue
            if int(fields[1]) == self.server.pid:
                children.append(int(entry))
        self.assertEqual(len(children), 1, children)
        return children[0]

    def test_the_server_sees_no_parent_secret_and_only_the_demo_settings(self):
        with open(f"/proc/{self.server_pid()}/environ", "rb") as f:
            environ = dict(part.split(b"=", 1) for part in f.read().split(b"\0") if b"=" in part)
        names = {name.decode() for name in environ}
        self.assertFalse({"CANARY_SECRET", "OIDC_ISSUER", "OIDC_CLIENT_ID"} & names, names & {"CANARY_SECRET", "OIDC_ISSUER", "OIDC_CLIENT_ID"})
        self.assertNotIn(SECRETS["CANARY_SECRET"].encode(), b"\0".join(environ.values()))
        self.assertEqual(environ[b"DARKROOM_DEMO"], b"1")
        self.assertEqual(environ[b"DARKROOM_HOST"], b"127.0.0.1")
        self.assertEqual(environ[b"HTTPS_PROXY"], verify.NO_OUTSIDE.encode())
        self.assertEqual(environ[b"FAKETIME"], verify.FROZEN_NOW.encode())
        self.assertEqual(environ[b"DARKROOM_DATA_DIR"], verify.data_dir(self.live).encode())

    def test_the_demo_member_is_signed_in_and_status_shows_the_three_people(self):
        status, _, body = fetch(self.base, "GET", "/api/me", self.session())
        self.assertEqual((status, json.loads(body)["sub"]), (200, "demo:avery"))
        status, _, body = fetch(self.base, "GET", "/api/status", self.session())
        self.assertEqual(status, 200)
        people = json.loads(body)["people"]
        self.assertEqual([p["slug"] for p in people], ["avery", "blair", "casey"])
        self.assertTrue(all(p["indexed"] > 0 for p in people), people)
        self.assertEqual(fetch(self.base, "GET", "/api/status")[0], 401)
        self.assertTrue(os.path.isfile(os.path.join(verify.photos_root(self.live), "avery", "originals", "IMG_0001.JPG")))

    def test_z_stopping_the_server_removes_its_copy_of_the_data(self):
        self.stop()
        for _ in range(50):
            if not os.path.exists(self.live):
                break
            time.sleep(0.1)
        self.assertFalse(os.path.exists(self.live))
        self.assertTrue(os.path.isdir(self.seed))


class RunTests(unittest.TestCase):
    def test_jobs_come_from_the_flag_then_the_environment_then_the_cpu_count_capped_at_four(self):
        self.assertEqual(verify.resolve_jobs(None, {}, cpus=8), 4)
        self.assertEqual(verify.resolve_jobs(None, {}, cpus=2), 2)
        self.assertEqual(verify.resolve_jobs(None, {"VERIFY_JOBS": " 3 "}, cpus=8), 3)
        self.assertEqual(verify.resolve_jobs(1, {"VERIFY_JOBS": "3"}, cpus=8), 1)
        for bad in ("0", "many", "-1"):
            with self.assertRaises(ValueError):
                verify.resolve_jobs(None, {"VERIFY_JOBS": bad})
        with self.assertRaises(ValueError):
            verify.resolve_jobs(0)

    def test_the_browser_run_gets_every_server_the_session_file_the_frozen_moment_and_the_pages(self):
        command = verify.node_command("node", "/r/frontend", ["http://127.0.0.1:1", "http://127.0.0.1:2"], "/d/demo-session.json", ["status"], ["open-people"])
        self.assertEqual(command[:2], ["node", "/r/frontend/verify/verify.mjs"])
        self.assertEqual([command[i + 1] for i, a in enumerate(command) if a == "--url"], ["http://127.0.0.1:1", "http://127.0.0.1:2"])
        self.assertEqual(command[command.index("--session") + 1], "/d/demo-session.json")
        self.assertEqual(command[command.index("--images") + 1], os.path.join(verify.ROOT, "docs", "screenshots"))
        self.assertEqual(command[command.index("--out") + 1], os.path.join(verify.ROOT, "artifacts", "verify"))
        self.assertEqual(command[command.index("--now") + 1], verify.FROZEN_NOW_ISO)
        self.assertEqual((command[command.index("--flow") + 1], command[-1]), ("open-people", "status"))

    def test_a_running_verify_is_recognised_by_its_command_line(self):
        self.assertTrue(verify.is_verify(["node", "/r/frontend/verify/verify.mjs", "--url", "x"]))
        self.assertTrue(verify.is_verify(["python", "tools/verify.py", "run", "status"]))
        self.assertFalse(verify.is_verify(["python", "tools/verify.py", "serve"]))
        self.assertFalse(verify.is_verify(["python", "run.py", "serve"]))

    def test_status_names_what_control_checks(self):
        found = verify.status()
        self.assertTrue({"libfaketime", "frozen_now_iso", "busy", "verify_running", "cpus"} <= set(found))

    def test_run_refuses_without_the_packages_the_build_or_a_job_count(self):
        with mock.patch.object(verify.os.path, "isdir", return_value=False), contextlib_redirect() as err:
            self.assertEqual(verify.run([]), 2)
        self.assertIn("npm ci", err.getvalue())

    def test_main_says_what_it_takes(self):
        with contextlib_redirect() as err:
            self.assertEqual(verify.main(["nope"]), 1)
        self.assertIn("usage:", err.getvalue())


def contextlib_redirect():
    import contextlib
    import io

    buffer = io.StringIO()
    manager = contextlib.redirect_stderr(buffer)

    class Both:
        def __enter__(self):
            manager.__enter__()
            return buffer

        def __exit__(self, *args):
            return manager.__exit__(*args)

    return Both()


class SessionStartHookTests(unittest.TestCase):
    HOOK = ROOT / ".claude" / "hooks" / "session-start.local.sh"

    def installs(self, browsers_path: str | None, cache_has_chromium: bool, preinstalled: bool) -> bool:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            work = Path(tmp) / "work"
            (work / "frontend").mkdir(parents=True)
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            marker = Path(tmp) / "installed"
            npx = bin_dir / "npx"
            npx.write_text(f"#!/bin/sh\ntouch {marker}\n")
            npx.chmod(0o755)
            if cache_has_chromium:
                (home / ".cache" / "ms-playwright" / "chromium-1" / "chrome-linux64").mkdir(parents=True)
                (home / ".cache" / "ms-playwright" / "chromium-1" / "chrome-linux64" / "chrome").write_text("")
            home.mkdir(exist_ok=True)
            pre = Path(browsers_path or tmp) / "browsers"
            if preinstalled:
                (pre / "chromium-2" / "chrome-linux").mkdir(parents=True)
                (pre / "chromium-2" / "chrome-linux" / "chrome").write_text("")
            env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(home)}
            if browsers_path is not None:
                env["PLAYWRIGHT_BROWSERS_PATH"] = str(pre)
            subprocess.run(["bash", "-c", f". {self.HOOK}"], cwd=work, env=env, check=True, capture_output=True)
            return marker.exists()

    def test_a_configured_browsers_path_is_the_only_place_looked_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(self.installs(str(Path(tmp) / "x"), cache_has_chromium=True, preinstalled=False))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(self.installs(str(Path(tmp) / "y"), cache_has_chromium=False, preinstalled=True))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(self.installs(None, cache_has_chromium=True, preinstalled=False))

    def test_chromium_in_the_configured_path_or_the_user_cache_is_enough_and_in_neither_is_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(self.installs(str(Path(tmp) / "a"), cache_has_chromium=False, preinstalled=True))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(self.installs(None, cache_has_chromium=True, preinstalled=False))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(self.installs(str(Path(tmp) / "b"), cache_has_chromium=True, preinstalled=True))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(self.installs(str(Path(tmp) / "c"), cache_has_chromium=False, preinstalled=False))

    def test_chromium_only_under_opt_pw_browsers_is_not_trusted_when_the_variable_is_unset(self):
        self.assertNotIn("/opt/pw-browsers", self.HOOK.read_text())
        self.assertTrue(self.installs(None, cache_has_chromium=False, preinstalled=False))


if __name__ == "__main__":
    unittest.main()
