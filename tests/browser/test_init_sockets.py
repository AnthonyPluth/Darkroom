import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from darkroom_init import plan, sockets
from tests.browser import host_network

ROOT = Path(__file__).resolve().parent.parent.parent
GIVEN = {"HOME": "/home/darkroom", "LANG": "C.UTF-8"}


def after(argv, flag):
    return argv[list(argv).index(flag) + 1]


class ListenerPlanTests(unittest.TestCase):
    def setUp(self):
        self.specs = {spec.name: spec for spec in plan.specs(GIVEN, bool, bool)}

    def test_x11vnc_serves_only_its_socket_in_the_private_folder(self):
        argv = self.specs["vnc"].argv
        self.assertEqual(argv[:5], ("python3", "-P", "-m", "darkroom_init.sockets", str(plan.VNC_SOCKET)))
        self.assertEqual(argv[5], "/usr/bin/x11vnc")
        self.assertEqual((after(argv, "-unixsock"), after(argv, "-rfbport"), after(argv, "-rfbportv6")), (str(plan.VNC_SOCKET), "0", "0"))
        self.assertFalse({"-localhost", "-listen", "-listen6", "-http", "-httpport", "-6"} & set(argv))

    def test_websockify_listens_only_on_its_socket_and_reaches_x11vnc_on_its_socket(self):
        argv = self.specs["viewer"].argv
        self.assertEqual(argv[:6], ("python3", "-P", "-m", "darkroom_init.sockets", str(plan.VIEWER_SOCKET), "/usr/bin/websockify"))
        self.assertEqual((after(argv, "--unix-listen"), after(argv, "--unix-target")), (str(plan.VIEWER_SOCKET), str(plan.VNC_SOCKET)))
        self.assertFalse([part for part in argv if ":" in part and part.rsplit(":", 1)[1].isdigit()])
        self.assertFalse([part for part in argv[6:] if part.isdigit()])
        self.assertNotIn("--unix-listen-mode", argv)
        self.assertEqual(after(argv, "--auth-source"), plan.PIPE["viewer"])
        self.assertEqual(self.specs["viewer"].secrets, ("viewer",))

    def test_xvfb_has_no_tcp_and_no_abstract_socket_only_its_file_socket(self):
        argv = self.specs["display"].argv
        refused = [argv[index + 1] for index, part in enumerate(argv) if part == "-nolisten"]
        self.assertEqual(sorted(refused), ["local", "tcp"])
        self.assertNotIn("-listen", argv)

    def test_every_socket_is_directly_in_the_one_private_folder_and_its_wrapper_checks_it(self):
        for path in (plan.VNC_SOCKET, plan.VIEWER_SOCKET, plan.SYNCTHING_SOCKET):
            self.assertTrue(sockets.in_folder(path), path)
        for name in ("vnc", "viewer"):
            spec = self.specs[name]
            self.assertEqual(spec.env["PYTHONPATH"], plan.INIT_HOME)
            self.assertFalse(spec.quiet)
            self.assertTrue(sockets.in_folder(Path(spec.argv[4])))
        image = (ROOT / "Dockerfile").read_text()
        self.assertIn("chown darkroom /data /home/darkroom /run/darkroom", image)
        self.assertIn("&& chmod 0700 /run/darkroom", image)

    def test_the_worker_reaches_the_app_on_its_socket_not_a_port_with_the_token_on_a_pipe(self):
        worker = self.specs["worker"]
        self.assertFalse({"DARKROOM_PORT", "DARKROOM_URL"} & set(worker.env))
        self.assertTrue(sockets.in_folder(plan.WORKER_SOCKET))
        app = self.specs["app"]
        self.assertEqual(app.argv, ("/app/.venv/bin/python", "run.py", "serve"))
        self.assertEqual(app.env[plan.ONE_CONTAINER], "1")
        self.assertEqual((worker.secrets, worker.env[plan.TOKEN_FD]), (("token",), plan.PIPE["token"]))


class SocketFolderTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def test_only_a_folder_of_this_user_that_nobody_else_can_enter_or_read_is_private(self):
        self.assertTrue(sockets.private_folder(self.folder))
        for mode in (0o701, 0o710, 0o704, 0o740, 0o750, 0o755, 0o777, 0o1777):
            with self.subTest(mode=oct(mode)):
                self.folder.chmod(mode)
                self.assertFalse(sockets.private_folder(self.folder))
                self.assertRaisesRegex(sockets.Refused, "not private", sockets.prepare, self.folder / "vnc.sock", self.folder)
        self.folder.chmod(0o700)
        with mock.patch.object(os, "getuid", return_value=os.lstat(self.folder).st_uid + 1):
            self.assertFalse(sockets.private_folder(self.folder))

    def test_a_link_to_a_private_folder_a_file_or_a_missing_folder_is_not_private(self):
        link = self.folder / "link"
        (self.folder / "real").mkdir(mode=0o700)
        link.symlink_to(self.folder / "real")
        (self.folder / "file").write_text("")
        (self.folder / "file").chmod(0o600)
        self.assertFalse(sockets.private_folder(link))
        self.assertFalse(sockets.private_folder(self.folder / "file"))
        for folder in (link, self.folder / "missing"):
            with self.subTest(folder=folder):
                self.assertRaisesRegex(sockets.Refused, "not private", sockets.prepare, folder / "vnc.sock", folder)

    def test_a_socket_path_outside_the_folder_is_refused(self):
        folder = self.folder
        for path in (Path("/tmp/vnc.sock"), folder / "nested" / "vnc.sock", folder / ".." / "vnc.sock", Path("vnc.sock"),
                     folder, folder.parent / f"{folder.name}x" / "vnc.sock"):
            with self.subTest(path=path):
                self.assertFalse(sockets.in_folder(path, folder))
                self.assertRaisesRegex(sockets.Refused, "outside the private socket folder", sockets.prepare, path, folder)
        self.assertTrue(sockets.in_folder(folder / "vnc.sock", folder))
        self.assertFalse(sockets.in_folder(plan.VNC_SOCKET, folder))

    def test_a_stale_socket_is_cleared_and_anything_else_in_its_place_is_left_and_refused(self):
        stale = self.folder / "vnc.sock"
        with socket.socket(socket.AF_UNIX) as left:
            left.bind(str(stale))
        sockets.prepare(stale, self.folder)
        self.assertFalse(stale.exists())
        sockets.prepare(stale, self.folder)
        for make in (lambda: stale.write_text("kept"), lambda: stale.mkdir(), lambda: stale.symlink_to(self.folder / "elsewhere")):
            with self.subTest():
                make()
                self.assertRaisesRegex(sockets.Refused, "something other than a socket", sockets.prepare, stale, self.folder)
                self.assertTrue(stale.is_symlink() or stale.exists())
                if stale.is_dir() and not stale.is_symlink():
                    stale.rmdir()
                else:
                    stale.unlink()


class WrapperTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name) / "run"
        self.folder.mkdir(mode=0o700)
        self.record = Path(folder.name) / "record"

    def wrapped(self, socket_path):
        script = f"import sys; from pathlib import Path; from darkroom_init import sockets; sys.exit(sockets.main(sys.argv, Path({str(self.folder)!r})))"
        program = ["/bin/sh", "-c", f"echo printed-canary; echo started > {self.record}"]
        return subprocess.run([sys.executable, "-P", "-c", script, str(socket_path), *program], env={"PYTHONPATH": plan.INIT_HOME, "PATH": "/usr/bin:/bin"},
                              capture_output=True, text=True, timeout=30, check=False)

    def test_with_a_private_folder_it_becomes_the_program_with_its_output_dropped(self):
        done = self.wrapped(self.folder / "vnc.sock")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.record.read_text(), "started\n")
        self.assertNotIn("printed-canary", done.stdout + done.stderr)

    def test_it_refuses_with_a_fixed_reason_and_starts_nothing_when_the_folder_is_open_or_the_socket_is_elsewhere(self):
        for name, socket_path, mode in (("open folder", self.folder / "vnc.sock", 0o755), ("outside", self.folder.parent / "vnc.sock", 0o700)):
            with self.subTest(name):
                self.folder.chmod(mode)
                done = self.wrapped(socket_path)
                self.assertEqual(done.returncode, 1)
                self.assertFalse(self.record.exists())
                event = json.loads(done.stderr.strip().splitlines()[-1])
                self.assertEqual(event["event"], "listener not started")
                self.assertNotIn(str(self.folder), done.stderr)
        self.folder.chmod(0o700)

    def test_it_needs_a_socket_and_an_absolute_program(self):
        for argv in (["wrapper"], ["wrapper", str(self.folder / "vnc.sock")], ["wrapper", str(self.folder / "vnc.sock"), "x11vnc"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()) as logged:
                self.assertEqual(sockets.main(argv, self.folder), 2)
                self.assertIn("listener not started", logged.getvalue())


LISTENING = """tcp   LISTEN 0 5    0.0.0.0:8000  0.0.0.0:* users:(("python",pid=41,fd=3))
tcp   LISTEN 0 4096 *:22000       *:*       users:(("syncthing",pid=40,fd=9))
udp   UNCONN 0 0    *:22000       *:*       users:(("syncthing",pid=40,fd=10))
udp   UNCONN 0 0    [::]:21027    [::]:*    users:(("syncthing",pid=40,fd=12))
tcp   LISTEN 0 128  0.0.0.0:22    0.0.0.0:* users:(("sshd",pid=7,fd=3))
"""
UNIX = """u_str LISTEN 0 32   /run/darkroom/vnc.sock 11 * 0 users:(("x11vnc",pid=42,fd=8))
u_str LISTEN 0 100  /run/darkroom/viewer.sock 12 * 0 users:(("python3",pid=43,fd=3))
u_str LISTEN 0 4096 /run/darkroom/syncthing.sock 13 * 0 users:(("syncthing",pid=40,fd=7))
u_str LISTEN 0 5    /run/darkroom/worker-api.sock 16 * 0 users:(("python",pid=41,fd=7))
u_str LISTEN 0 4096 /tmp/.X11-unix/X99 14 * 0 users:(("Xvfb",pid=44,fd=6))
u_str LISTEN 0 4096 @/tmp/.X11-unix/X0 15 * 0 users:(("Xorg",pid=8,fd=6))
"""


class HostProbeTests(unittest.TestCase):
    def run_probe(self, command, given, *pids):
        with mock.patch.object(sys, "stdin", io.StringIO(given)), contextlib.redirect_stdout(io.StringIO()):
            return host_network.main(command, *map(str, pids))

    def test_only_the_containers_own_listeners_count_and_only_the_three_published_ports_pass(self):
        self.assertEqual(self.run_probe("listeners", LISTENING, 40, 41, 42), 0)
        self.assertEqual(self.run_probe("listeners", LISTENING, 7, 40, 41), 1)
        self.assertEqual(self.run_probe("listeners", LISTENING + 'tcp LISTEN 0 5 127.0.0.1:6080 0.0.0.0:* users:(("python3",pid=43,fd=3))\n', 40, 41, 43), 1)
        self.assertEqual(self.run_probe("listeners", LISTENING, 41), 1)

    def probe_with(self, command, reachable, connected, *hosts, worker_route=404):
        def answers(host, port):
            return (host, port) in reachable

        with mock.patch.object(host_network, "answers", answers), mock.patch.object(host_network, "unix_answers", lambda path: connected.get(path, "PermissionError")), \
                mock.patch.object(host_network, "worker_route_status", lambda host: worker_route), contextlib.redirect_stdout(io.StringIO()):
            return host_network.main(command, *hosts)

    def test_from_outside_every_host_must_reach_a_published_port_and_no_internal_one(self):
        published = {("127.0.0.1", 8000), ("::1", 22000), ("10.0.0.5", 8000)}
        hosts = ("127.0.0.1", "::1", "10.0.0.5")
        self.assertEqual(self.probe_with("refused", published, {}, *hosts), 0)
        self.assertEqual(self.probe_with("refused", set(), {}, *hosts), 1)
        self.assertEqual(self.probe_with("refused", published - {("10.0.0.5", 8000)}, {}, *hosts), 1)
        self.assertEqual(self.probe_with("refused", published | {("::1", 5900)}, {}, *hosts), 1)
        self.assertEqual(self.probe_with("refused", published, {host_network.DISPLAY: "connected"}, *hosts), 1)

    def test_from_inside_the_app_must_answer_and_no_internal_port_on_either_loopback(self):
        self.assertEqual(self.probe_with("inside", {("127.0.0.1", 8000)}, {}), 0)
        self.assertEqual(self.probe_with("inside", set(), {}), 1)
        self.assertEqual(self.probe_with("inside", {("127.0.0.1", 8000), ("::1", 5900)}, {}), 1)
        self.assertEqual(self.probe_with("inside", {("127.0.0.1", 8000)}, {"/run/darkroom/vnc.sock": "connected"}), 1)
        self.assertEqual(self.probe_with("inside", {("127.0.0.1", 8000)}, {"/run/darkroom/worker-api.sock": "connected"}), 1)
        for answered in (401, 200, "ConnectionRefusedError"):
            with self.subTest(worker_route=answered):
                self.assertEqual(self.probe_with("inside", {("127.0.0.1", 8000)}, {}, worker_route=answered), 1)

    def test_the_worker_route_probe_reads_the_status_the_web_port_gives(self):
        import http.server
        import threading

        class Answer(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(404 if self.path == host_network.WORKER_ROUTE and self.headers["Authorization"].startswith("Bearer ") else 500)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format, *args):
                return None

        server = http.server.HTTPServer(("127.0.0.1", 0), Answer)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.assertEqual(host_network.worker_route_status("127.0.0.1", server.server_address[1]), 404)
        server.shutdown()
        server.server_close()
        self.assertEqual(host_network.worker_route_status("127.0.0.1", server.server_address[1]), "ConnectionRefusedError")

    def test_an_abstract_socket_or_a_missing_internal_socket_fails(self):
        self.assertEqual(self.run_probe("unix-listeners", UNIX, 40, 41, 42, 43, 44), 0)
        self.assertEqual(self.run_probe("unix-listeners", UNIX, 8, 40, 41, 42, 43, 44), 1)
        self.assertEqual(self.run_probe("unix-listeners", UNIX, 40, 41, 42, 44), 1)
        self.assertEqual(self.run_probe("unix-listeners", UNIX, 40, 42, 43, 44), 1)
        self.assertEqual(self.run_probe("unix-listeners", UNIX.replace("/tmp/.X11-unix/X99", "@/tmp/.X11-unix/X99"), 40, 41, 42, 43, 44), 1)


if __name__ == "__main__":
    unittest.main()
