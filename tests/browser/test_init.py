import contextlib
import itertools
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import run
from darkroom import config as app_config
from darkroom.domain import browser_jobs
from darkroom.providers import exiftool
from darkroom.storage import db
from darkroom.storage.models import BrowserJob
from darkroom_browser import config as worker_config
from darkroom_browser import runner
from darkroom_browser.client import Client
from darkroom_init import __main__ as init_main
from darkroom_init import plan, supervisor, viewer_auth
from tests.browser import one_container
from tests.browser.test_runner import FakeBrowser
from tests.privacy import no_leaks
from tests.shared import own_database, start_server
from tests.test_browser_jobs import add_account

ROOT = Path(__file__).resolve().parent.parent.parent
TOKEN = "init-token-canary-0123456789abcdef0123456789"
VIEWER = "viewer-secret-canary-0123456789abcdef01234"
SECRETS = {"token": TOKEN, "viewer": VIEWER}
GIVEN = {"PATH": "/x", "HOME": "/home/darkroom", "LANG": "C.UTF-8", "HOSTNAME": "abc", "PYTHON_SHA256": "f" * 64, "DARKROOM_SECRET_KEY": "key-canary",
         "OIDC_CLIENT_SECRET": "oidc-canary", "IMMICH_ADMIN_KEY": "immich-canary", "DATABASE_URL": "postgresql://u:p@h/d", "RESEND_API_KEY": "resend-canary",
         "DARKROOM_BROWSER_TOKEN": "someone-set-this-token-0123456789abcdef", "DARKROOM_VNC_SECRET": "someone-set-this-secret-0123456789abc",
         "DARKROOM_PORT": "8123", "DARKROOM_PHOTOS_ROOT": "/share", "TZ": "Europe/Paris", "POLL_SECONDS": "5", "PLAYWRIGHT_BROWSERS_PATH": "/ms-playwright"}
APP_SECRETS = ("DARKROOM_SECRET_KEY", "OIDC_CLIENT_SECRET", "IMMICH_ADMIN_KEY", "DATABASE_URL", "RESEND_API_KEY")


def handed(value):
    read_end, write_end = os.pipe()
    os.write(write_end, value.encode())
    os.close(write_end)
    return read_end


class PlanTests(unittest.TestCase):
    def test_the_app_gets_every_setting_it_reads_except_the_browser_token(self):
        read = set(re.findall(r'env(?:_list|_flag)?\("([A-Z0-9_]+)"', (ROOT / "darkroom/config.py").read_text()))
        self.assertEqual(set(plan.APP_SETTINGS), read - {"DARKROOM_BROWSER_TOKEN", "DARKROOM_BROWSER_TOKEN_FD", "DARKROOM_VIEWER_SECRET_FD"})
        env = plan.app_env(GIVEN)
        self.assertEqual((env[plan.TOKEN_FD], env[plan.VIEWER_FD]), (plan.PIPE["token"], plan.PIPE["viewer"]))
        self.assertFalse({"DARKROOM_BROWSER_TOKEN", "DARKROOM_VNC_SECRET", "HOSTNAME", "PYTHON_SHA256", "POLL_SECONDS", "PLAYWRIGHT_BROWSERS_PATH"} & set(env))
        self.assertEqual(env["DARKROOM_SECRET_KEY"], "key-canary")
        self.assertEqual(env["PATH"], "/app/.venv/bin:/usr/local/bin:/usr/bin:/bin")

    def test_the_worker_gets_what_it_reads_and_no_darkroom_secret(self):
        read = set(re.findall(r'env\("([A-Z0-9_]+)"', (ROOT / "darkroom_browser/config.py").read_text())) | {"TZ", "BROWSER_CHANNEL"}
        env = plan.worker_env(GIVEN)
        self.assertEqual(read - set(env) - set(plan.WORKER_SETTINGS), {"DARKROOM_BROWSER_TOKEN"})
        self.assertEqual(env["DARKROOM_URL"], "http://127.0.0.1:8123")
        self.assertEqual((env["PHOTOS_ROOT"], env["DISPLAY"], env["TZ"], env["POLL_SECONDS"]), ("/share", ":99", "Europe/Paris", "5"))
        self.assertFalse({*APP_SECRETS, "DARKROOM_BROWSER_TOKEN", "DARKROOM_VNC_SECRET", "HOSTNAME", "DARKROOM_PORT"} & set(env))

    def test_the_display_stack_gets_almost_nothing(self):
        self.assertEqual(plan.display_env(GIVEN), {"PATH": "/usr/local/bin:/usr/bin:/bin", "DISPLAY": ":99", "HOME": "/home/darkroom", "LANG": "C.UTF-8"})
        viewer = next(spec for spec in plan.specs(GIVEN, bool, bool) if spec.name == "viewer")
        self.assertEqual(set(viewer.env) - set(plan.display_env(GIVEN)), {"PYTHONPATH", "PYTHONDONTWRITEBYTECODE"})

    def test_the_app_starts_first_and_the_secrets_go_on_pipes_never_on_a_command_line(self):
        specs = plan.specs(GIVEN, bool, bool)
        self.assertEqual([spec.name for spec in specs], ["app", "display", "vnc", "viewer", "worker"])
        self.assertEqual([spec.secrets for spec in specs], [("token", "viewer"), (), (), ("viewer",), ("token",)])
        self.assertEqual([spec.name for spec in specs if spec.strays], ["worker"])
        viewer = specs[3].argv
        self.assertEqual(viewer[viewer.index("--auth-source") + 1], plan.PIPE["viewer"])
        self.assertEqual(viewer[viewer.index("--auth-plugin") + 1], plan.VIEWER_PLUGIN)
        self.assertEqual(viewer[-2:], ("127.0.0.1:6080", "127.0.0.1:5900"))
        self.assertIn("-localhost", specs[2].argv)
        for spec in specs:
            self.assertFalse(any(TOKEN in part or VIEWER in part for part in (*spec.argv, *spec.env.values())))


def child(name, script, **extra):
    return plan.Spec(name, (sys.executable, "-c", textwrap.dedent(script)), {"PATH": "/usr/bin:/bin"} | extra.pop("env", {}), **extra)


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def run_for(self, seconds, specs, during=None, **options):
        running = supervisor.Supervisor(specs, SECRETS, **{"base": 0.1, "cap": 0.4, "stop_seconds": 1.0, "tick": 0.02} | options)
        thread = threading.Thread(target=running.run)
        with no_leaks(self, TOKEN, VIEWER) as watched:
            thread.start()
            time.sleep(seconds)
            if during is not None:
                during()
            running.stop()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertTrue(watched)
        return running

    def lines(self, name):
        path = self.folder / name
        return path.read_text().splitlines() if path.exists() else []

    def test_backoff_doubles_up_to_its_cap(self):
        self.assertEqual([supervisor.backoff(n, 1.0, 60.0) for n in range(8)], [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0])
        self.assertEqual(supervisor.backoff(10**6), supervisor.CAP_SECONDS)

    def test_a_child_that_keeps_exiting_is_restarted_with_a_capped_backoff(self):
        script = f"import time; open({str(self.folder / 'starts')!r}, 'a').write(f'{{time.monotonic()}}\\n'); raise SystemExit(4)"
        self.run_for(2.5, [child("flaky", script)])
        starts = [float(line) for line in self.lines("starts")]
        gaps = [later - earlier for earlier, later in itertools.pairwise(starts)]
        self.assertGreaterEqual(len(gaps), 4)
        self.assertGreater(gaps[0], 0.15)
        self.assertGreater(gaps[2], gaps[0])
        self.assertTrue(all(gap < 0.9 for gap in gaps), gaps)

    def test_a_child_that_ran_long_enough_restarts_at_the_base_delay_and_one_that_cannot_start_is_retried(self):
        running = self.run_for(1.0, [child("steady", "raise SystemExit(0)"), plan.Spec("missing", ("/nonexistent/program",), {}, secrets=("token",))], stable=0.0)
        self.assertEqual(running.children[0].failures, 0)
        self.assertGreaterEqual(running.children[1].failures, 2)

    def test_sigterm_stops_the_children_in_the_reverse_of_their_start_order(self):
        script = """
            import signal, sys, time
            def stopped(*_):
                open(sys.argv[1], "a").write(sys.argv[2] + "\\n"); raise SystemExit(0)
            signal.signal(signal.SIGTERM, stopped)
            time.sleep(60)
        """
        specs = [plan.Spec(name, (sys.executable, "-c", textwrap.dedent(script), str(self.folder / "stopped"), name), {}) for name in ("app", "display", "worker")]
        self.run_for(1.0, specs)
        self.assertEqual(self.lines("stopped"), ["worker", "display", "app"])

    def test_a_child_that_ignores_sigterm_is_killed(self):
        running = self.run_for(0.5, [child("stubborn", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)")], stop_seconds=0.3)
        self.assertEqual(running.children[0].process.returncode, -signal.SIGKILL)

    def test_a_restart_stops_the_whole_process_group_first(self):
        script = f"""
            import subprocess, sys
            left = subprocess.Popen(["sleep", "60"])
            open({str(self.folder / 'left')!r}, "a").write(f"{{left.pid}}\\n")
            raise SystemExit(3)
        """
        self.run_for(1.0, [child("worker", script)])
        left = [int(pid) for pid in self.lines("left")]
        self.assertGreaterEqual(len(left), 2)
        time.sleep(0.2)
        self.assertFalse(set(left) & {process["pid"] for process in one_container.processes()}, left)

    def test_a_restarted_child_gets_the_same_secret_on_a_pipe_and_nowhere_else(self):
        script = f"""
            import json, os, sys, time
            fd = int(sys.argv[1] if len(sys.argv) > 1 else os.environ["DARKROOM_BROWSER_TOKEN_FD"])
            secret = os.read(fd, 4096).decode(); os.close(fd)
            fds = sorted(os.listdir("/proc/self/fd"))
            with open({str(self.folder / 'seen')!r}, "a") as out:
                out.write(json.dumps({{"secret": secret, "fds": fds, "pid": os.getpid(), "argv": sys.argv[1:]}}) + "\\n")
            time.sleep(float(os.environ["STAY"]))
        """

        def scanned():
            deadline = time.monotonic() + 10
            while len(self.lines("seen")) < 3 and time.monotonic() < deadline:
                time.sleep(0.05)
            for pid in {json.loads(line)["pid"] for line in self.lines("seen")}:
                for part in ("environ", "cmdline"):
                    with contextlib.suppress(FileNotFoundError, ProcessLookupError, PermissionError):
                        self.assertNotIn(TOKEN.encode(), Path(f"/proc/{pid}/{part}").read_bytes())
        worker = child("worker", script, env={plan.TOKEN_FD: plan.PIPE["token"], "STAY": "0.3"}, secrets=("token",))
        viewer = plan.Spec("viewer", (sys.executable, "-c", textwrap.dedent(script), plan.PIPE["viewer"]), {"STAY": "60"}, secrets=("viewer",))
        self.run_for(0, [viewer, worker], base=0.05, during=scanned)
        seen = [json.loads(line) for line in self.lines("seen")]
        self.assertEqual({(entry["secret"], bool(entry["argv"])) for entry in seen}, {(TOKEN, False), (VIEWER, True)})
        self.assertTrue(len({entry["pid"] for entry in seen}) >= 3 and all(arg.isdigit() for entry in seen for arg in entry["argv"]))
        self.assertEqual({len(entry["fds"]) for entry in seen}, {4}, seen)

    def test_the_next_child_waits_until_the_one_before_is_ready(self):
        marker = self.folder / "ready"
        first = child("app", f"import time, pathlib; time.sleep(0.3); pathlib.Path({str(marker)!r}).touch(); time.sleep(60)", ready=marker.exists)
        second = child("worker", f"import pathlib; open({str(self.folder / 'order')!r}, 'w').write(str(pathlib.Path({str(marker)!r}).exists())); import time; time.sleep(60)")
        self.run_for(1.2, [first, second])
        self.assertEqual(self.lines("order"), ["True"])


ORPHAN_HARNESS = """
import os, subprocess, sys, threading
from darkroom_init import plan, supervisor
assert supervisor.prctl(supervisor.PR_SET_CHILD_SUBREAPER, 1)
leave = "import subprocess, sys; open(sys.argv[1], 'a').write(str(subprocess.Popen(['sleep', '60'], start_new_session=True).pid) + chr(10))"
stay = f"import subprocess, sys, time; subprocess.run([sys.executable, '-c', {leave!r}, sys.argv[1]]); time.sleep(60)"
worker = plan.Spec("worker", (sys.executable, "-c", stay if sys.argv[2] == "stay" else leave, sys.argv[1]), {}, strays=True)
running = supervisor.Supervisor([worker, plan.Spec("vnc", (sys.executable, "-c", "pass"), {})], {}, adopt_orphans=True, base=0.2, tick=0.02, stop_seconds=1.0)
def stop():
    print(sorted({os.path.exists(f"/proc/{pid}") for pid in open(sys.argv[1]).read().split()}))
    running.stop()
threading.Timer(1.5, stop).start()
running.run()
"""


class OrphanTests(unittest.TestCase):
    def strays(self, mode):
        with tempfile.TemporaryDirectory() as folder:
            record = Path(folder) / "left"
            done = subprocess.run([sys.executable, "-c", ORPHAN_HARNESS, str(record), mode], cwd=ROOT, capture_output=True, text=True, timeout=30, check=False)
            self.assertEqual(done.returncode, 0, done.stderr)
            left = [int(pid) for pid in record.read_text().split()]
        self.assertFalse(set(left) & {process["pid"] for process in one_container.processes()}, left)
        return left, done

    def test_a_browser_that_left_its_workers_group_is_stopped_when_the_worker_exits(self):
        left, done = self.strays("exit")
        self.assertGreaterEqual(len(left), 2)
        self.assertIn("stopping a process its parent left behind", done.stderr)

    def test_a_running_workers_strays_outlive_other_restarts_until_shutdown(self):
        left, done = self.strays("stay")
        self.assertEqual((len(left), done.stdout), (1, "[True]\n"))

    def test_the_image_check_counts_only_browsers_outside_every_worker_as_strays(self):
        every = [{"pid": 277, "ppid": 7, "args": ["python", "-m", "darkroom_browser"], "comm": "python"}, {"pid": 300, "ppid": 277, "args": [], "comm": "node"},
                 {"pid": 328, "ppid": 300, "args": [], "comm": "chrome"}, {"pid": 339, "ppid": 328, "args": [], "comm": "chrome"}, {"pid": 150, "ppid": 7, "args": [], "comm": "chrome"}]
        self.assertEqual(one_container.pids(every), {"workers": [277], "browsers": [328, 339, 150], "strays": [150]})

    def test_adopted_processes_and_listening_ports_are_read_from_proc(self):
        with tempfile.TemporaryDirectory() as folder:
            proc = Path(folder)
            for pid, stat in ((10, "10 (python3) S 7 10"), (11, "11 (a) b) S 8 11"), (12, "12 (x) Z 7 12"), (13, "broken")):
                (proc / str(pid)).mkdir()
                (proc / str(pid) / "stat").write_text(stat)
            self.assertEqual(sorted(supervisor.adopted_by(7, proc)), [10, 12])
            self.assertEqual([supervisor.group_alive(group, proc) for group in (10, 11, 12, 99)], [True, True, False, False])
            (proc / "net").mkdir()
            (proc / "net/tcp").write_text("  sl  local_address rem_address   st\n   0: 00000000:1F40 00000000:0000 0A\n   1: 0100007F:1F41 0100007F:9999 01\n")
            self.assertEqual([supervisor.listening(port, proc) for port in (8000, 8001)] + [supervisor.listening(8000, proc / "missing")], [True, False, False])


class ViewerAuthTests(unittest.TestCase):
    def test_websockify_gets_the_viewer_secret_from_its_pipe(self):
        plugins = types.ModuleType("websockify.auth_plugins")
        plugins.BasicHTTPAuth = mock.Mock()
        with mock.patch.dict(sys.modules, {"websockify": types.ModuleType("websockify"), "websockify.auth_plugins": plugins}):
            fd = handed(VIEWER)
            viewer_auth.SecretOnPipe(str(fd)).authenticate({"h": "v"}, "127.0.0.1", 5900)
            self.assertRaises(OSError, os.fstat, fd)
            self.assertRaises(ValueError, viewer_auth.SecretOnPipe, str(handed("short")))
        plugins.BasicHTTPAuth.assert_called_once_with(f"darkroom:{VIEWER}")
        plugins.BasicHTTPAuth.return_value.authenticate.assert_called_once_with({"h": "v"}, "127.0.0.1", 5900)


class InitMainTests(unittest.TestCase):
    def test_main_hands_two_fresh_secrets_and_logs_neither(self):
        held = {}

        def run(self):
            held.update(self.secrets)
            return 0

        with no_leaks(self, "never-a-real-token-value") as watched, mock.patch.object(supervisor, "prctl", return_value=True) as prctl, \
                mock.patch.object(supervisor.Supervisor, "run", run), mock.patch("signal.signal"):
            self.assertEqual(init_main.main(GIVEN), 0)
            watched.update(held.values())
        self.assertEqual(sorted(held), ["token", "viewer"])
        self.assertNotEqual(held["token"], held["viewer"])
        self.assertTrue(all(len(value) == 43 and app_config.browser_token_usable(value) for value in held.values()))
        self.assertEqual([call.args for call in prctl.call_args_list], [(supervisor.PR_SET_DUMPABLE, 0), (supervisor.PR_SET_CHILD_SUBREAPER, 1)])
        with mock.patch.object(supervisor, "listening", return_value=False) as listening:
            self.assertFalse(init_main.display_ready({"DISPLAY": ":12345"})() or init_main.app_ready({"DARKROOM_PORT": "nope"})())
        listening.assert_called_once_with(8000)


class TokenOnPipeTests(unittest.TestCase):
    def setUp(self):
        app_config.handed_tokens.clear()
        self.addCleanup(app_config.handed_tokens.clear)

    def test_the_app_reads_the_token_once_from_its_pipe_and_check_config_never_shows_it(self):
        fd = handed(TOKEN + "\n")
        with tempfile.TemporaryDirectory() as folder, no_leaks(self, TOKEN), mock.patch.object(exiftool, "version", return_value="13.0"), \
                mock.patch.dict(os.environ, {"DARKROOM_BROWSER_TOKEN_FD": str(fd), "DARKROOM_PHOTOS_ROOT": folder, "DARKROOM_DATA_DIR": folder,
                                             "DARKROOM_PUBLIC_URL": "https://d.example", "DARKROOM_ALLOW_NO_AUTH": "1"}, clear=True):
            self.assertEqual(app_config.load().browser_token, TOKEN)
            self.assertRaises(OSError, os.fstat, fd)
            self.assertEqual(app_config.load().browser_token, TOKEN)
            self.assertEqual(run.main(["check-config"]), 0)
            self.assertNotIn(TOKEN, "".join(os.environ.values()))

    def test_the_environment_token_still_works_and_a_bad_pipe_gives_none(self):
        with mock.patch.dict(os.environ, {"DARKROOM_BROWSER_TOKEN": TOKEN}, clear=True):
            self.assertEqual((app_config.load().browser_token, worker_config.load().token), (TOKEN, TOKEN))
        for raw in ("1", "x", "999999"):
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {"DARKROOM_BROWSER_TOKEN_FD": raw, "DARKROOM_BROWSER_TOKEN": TOKEN}, clear=True):
                self.assertEqual((app_config.load().browser_token, worker_config.load().token), ("", ""))
                self.assertIn("DARKROOM_BROWSER_TOKEN is not set; the worker will not start without the shared token", worker_config.problems(worker_config.load()))

    def test_a_worker_and_the_app_both_given_the_token_on_pipes_run_a_noop_job_without_logging_it(self):
        engine = own_database(self)
        photos = tempfile.TemporaryDirectory()
        self.addCleanup(photos.cleanup)
        app_env = {"DARKROOM_BROWSER_TOKEN_FD": str(handed(TOKEN)), "DARKROOM_ALLOW_NO_AUTH": "1", "DARKROOM_PHOTOS_ROOT": photos.name, "DARKROOM_PUBLIC_URL": "http://127.0.0.1"}
        with mock.patch.dict(os.environ, app_env, clear=True):
            settings = app_config.load()
        with db.session(engine) as opened:
            job = browser_jobs.enqueue(opened, add_account(opened), "noop", None, 1.0).id
        with no_leaks(self, TOKEN, database=engine):
            base, _ = start_server(self, settings, engine)
            with mock.patch.dict(os.environ, {"DARKROOM_BROWSER_TOKEN_FD": str(handed(TOKEN)), "DARKROOM_URL": base, "PHOTOS_ROOT": photos.name}, clear=True):
                worker = worker_config.load()
            self.assertEqual(worker_config.problems(worker), [])
            self.assertTrue(runner.Runner(worker, Client(worker.darkroom_url, worker.token), lambda folder: FakeBrowser(), sleep=lambda seconds: None).tick())
        with db.session(engine) as opened:
            self.assertEqual(opened.get(BrowserJob, job).status, "done")


if __name__ == "__main__":
    unittest.main()
