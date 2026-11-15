import os
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree

from darkroom import config as app_config
from darkroom_init import __main__ as init_main
from darkroom_init import plan, supervisor, syncthing
from tests.fakes.syncthing import FakeSyncthing
from tests.privacy import no_leaks

ROOT = Path(__file__).resolve().parent.parent.parent
OLD = "generated-key-canary-0123456789abcdef"
NEW = "rotated-key-canary-0123456789abcdef0123456"
OWN = "PP2OO7B-2FRGILR-MSIJEZX-6KTCQJN-2VUJME3-NY3V3OD-5XW7TBJ-Y4OF4A2"
GENERATED = textwrap.dedent(f"""\
    <configuration version="51">
        <device id="{OWN}" name="made-up-host" compression="metadata" introducer="false">
            <address>dynamic</address>
            <paused>false</paused>
        </device>
        <gui enabled="true" tls="false" sendBasicAuthPrompt="false">
            <address>127.0.0.1:8384</address>
            <apikey>{OLD}</apikey>
            <theme>default</theme>
        </gui>
        <options>
            <listenAddress>default</listenAddress>
            <globalAnnounceServer>default</globalAnnounceServer>
            <globalAnnounceEnabled>true</globalAnnounceEnabled>
            <localAnnounceEnabled>true</localAnnounceEnabled>
            <relaysEnabled>true</relaysEnabled>
            <startBrowser>true</startBrowser>
            <natEnabled>true</natEnabled>
            <urAccepted>0</urAccepted>
            <autoUpgradeIntervalH>12</autoUpgradeIntervalH>
            <setLowPriority>true</setLowPriority>
            <crashReportingEnabled>true</crashReportingEnabled>
        </options>
    </configuration>
    """)
GIVEN = {"PATH": "/x", "HOME": "/home/darkroom", "LANG": "C.UTF-8", "TZ": "Europe/Paris", "DARKROOM_SYNCTHING": "1", "DARKROOM_DATA_DIR": "/appdata/",
         "HTTPS_PROXY": "http://proxy.example:3128", "http_proxy": "http://proxy.example:3128", "DARKROOM_SECRET_KEY": "key-canary", "STGUIAPIKEY": "someone-set-this",
         "IMMICH_ADMIN_KEY": "immich-canary"}


def fake_generate(argv, **options):
    home = Path(next(part for part in argv if part.startswith("--home=")).removeprefix("--home="))
    (home / "config.xml").write_text(GENERATED)
    (home / "key.pem").write_text("made-up key")
    os.chmod(home / "config.xml", 0o600)


def options_of(config):
    found = ElementTree.parse(config).getroot()
    return ({entry.tag: entry.text for entry in found.find("options") if entry.tag != "listenAddress"},
            [entry.text for entry in found.find("options").findall("listenAddress")], {entry.tag: entry.text for entry in found.find("gui")})


class Folders(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.data = Path(folder.name)
        self.home = self.data / "syncthing"
        self.run_folder = self.data / "run"
        self.run_folder.mkdir(mode=0o700)
        self.socket = self.run_folder / "syncthing.sock"


class FirstStartTests(Folders):
    def prepare(self, run=fake_generate):
        recorded = mock.Mock(side_effect=run)
        with no_leaks(self, OLD):
            syncthing.prepare(self.home, self.socket, "/opt/made-up/syncthing", recorded)
        return recorded

    def test_a_fresh_identity_gets_every_outward_option_off_and_the_api_on_a_private_socket_before_the_first_serve(self):
        generate = self.prepare()
        argv = generate.call_args.args[0]
        self.assertEqual(argv[:2], ["/opt/made-up/syncthing", "generate"])
        self.assertEqual(argv[3:], ["--no-port-probing"])
        self.assertTrue(argv[2].startswith(f"--home={self.data}/.syncthing-"))
        self.assertEqual(generate.call_args.kwargs | {"stdin": None}, {"stdin": None, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "check": True,
                                                                        "timeout": syncthing.GENERATE_SECONDS})
        options, listen, gui = options_of(self.home / "config.xml")
        self.assertEqual({name: options[name] for name in syncthing.FIRST_START}, syncthing.FIRST_START)
        self.assertEqual(options["globalAnnounceServer"], "default")
        self.assertEqual(listen, ["tcp://0.0.0.0:22000", "quic://0.0.0.0:22000"])
        self.assertEqual((gui["address"], gui["unixSocketPermissions"], gui["apikey"]), (str(self.socket), "0600", OLD))
        self.assertEqual(sorted(path.name for path in self.data.iterdir()), ["run", "syncthing"])
        self.assertEqual(stat.S_IMODE(os.stat(self.home).st_mode), 0o700)

    def test_an_existing_identity_is_kept_and_never_generated_again(self):
        self.prepare()
        before = (self.home / "config.xml").read_bytes()
        self.assertFalse(self.prepare().called)
        self.assertEqual((self.home / "config.xml").read_bytes(), before)

    def test_an_empty_home_folder_is_filled_and_one_with_files_but_no_configuration_is_refused(self):
        self.home.mkdir(mode=0o700)
        self.prepare()
        self.assertTrue((self.home / "key.pem").exists())
        (self.home / "config.xml").unlink()
        with self.assertRaisesRegex(syncthing.Refused, "holds files but no configuration"):
            self.prepare()
        self.assertTrue((self.home / "key.pem").exists())

    def test_a_failed_generate_leaves_no_half_made_home(self):
        def broken(argv, **options):
            fake_generate(argv)
            raise subprocess.CalledProcessError(1, argv)

        self.assertRaises(subprocess.CalledProcessError, self.prepare, broken)
        self.assertEqual([path.name for path in self.data.iterdir()], ["run"])

    def test_a_configuration_that_turns_an_outward_option_back_on_or_moves_the_api_off_the_socket_is_refused_at_every_start(self):
        self.prepare()
        config = self.home / "config.xml"
        for name in syncthing.OFF:
            with self.subTest(name=name):
                original = config.read_text()
                config.write_text(original.replace(f"<{name}>{syncthing.OFF[name]}</{name}>", f"<{name}>1</{name}>"))
                with self.assertRaisesRegex(syncthing.Refused, f"differs from what Darkroom keeps: {name}$"):
                    self.prepare()
                config.write_text(original)
        config.write_text(config.read_text().replace("<localAnnounceEnabled>false", "<localAnnounceEnabled>true"))
        self.prepare()
        original = config.read_text()
        config.write_text(original.replace(f"<address>{self.socket}</address>", "<address>0.0.0.0:8384</address>").replace(">0600<", ">0666<"))
        self.assertRaisesRegex(syncthing.Refused, "differs from what Darkroom keeps: gui address, gui unixSocketPermissions$", self.prepare)
        config.write_text(original)
        config.write_text("<configuration><gui/></configuration>")
        self.assertRaisesRegex(syncthing.Refused, "no options or GUI section", self.prepare)

    def test_the_socket_folder_must_be_private_and_a_stale_socket_is_cleared(self):
        os.chmod(self.run_folder, 0o750)
        self.assertRaisesRegex(syncthing.Refused, "not private", self.prepare)
        self.assertFalse(self.home.exists())
        os.chmod(self.run_folder, 0o700)
        with socket.socket(socket.AF_UNIX) as stale:
            stale.bind(str(self.socket))
        self.prepare()
        self.assertFalse(self.socket.exists())
        self.socket.write_text("not a socket")
        self.assertRaisesRegex(syncthing.Refused, "something other than a socket", self.prepare)
        self.socket.unlink()
        self.socket.parent.rmdir()
        self.socket.parent.symlink_to(self.data)
        self.assertRaisesRegex(syncthing.Refused, "not private", self.prepare)

    def test_syncthing_is_served_paused_on_the_socket_with_no_key_on_its_command_line_or_in_its_environment(self):
        argv = syncthing.serve_argv(self.home)
        self.assertEqual(argv, [syncthing.BINARY, "serve", f"--home={self.home}", "--no-browser", "--no-upgrade", "--no-restart", "--paused"])
        env = plan.syncthing_env(GIVEN)
        self.assertEqual(env, {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/darkroom", "LANG": "C.UTF-8", "TZ": "Europe/Paris", "PYTHONPATH": plan.INIT_HOME,
                               "PYTHONDONTWRITEBYTECODE": "1", "STNOUPGRADE": "1", "STNODEFAULTFOLDER": "1"})
        source = (ROOT / "darkroom_init/syncthing.py").read_text() + (ROOT / "darkroom_init/plan.py").read_text()
        self.assertNotIn("gui-apikey", source)
        self.assertNotIn("STGUIAPIKEY", source)


class WrapperTests(Folders):
    def test_the_wrapper_prepares_then_becomes_syncthing_with_its_output_dropped(self):
        with mock.patch.object(syncthing, "prepare") as prepare, mock.patch("os.execv") as execv, mock.patch("os.dup2") as dup2, \
                mock.patch("os.open", return_value=99):
            syncthing.main(["wrapper", str(self.home)])
        prepare.assert_called_once_with(self.home)
        self.assertEqual([call.args for call in dup2.call_args_list], [(99, 1), (99, 2)])
        execv.assert_called_once_with(syncthing.BINARY, syncthing.serve_argv(self.home))

    def test_a_refused_or_failed_start_exits_with_a_fixed_reason_for_the_init_to_retry(self):
        with no_leaks(self, "unusable-home-canary") as watched, mock.patch("os.execv") as execv:
            self.assertEqual(syncthing.main(["wrapper", "unusable-home-canary"]), 2)
            with mock.patch.object(syncthing, "prepare", side_effect=syncthing.Refused("a fixed reason")):
                self.assertEqual(syncthing.main(["wrapper", str(self.home)]), 1)
            with mock.patch.object(syncthing, "prepare", side_effect=PermissionError(13, "denied", "/appdata/unusable-home-canary")):
                self.assertEqual(syncthing.main(["wrapper", str(self.home)]), 1)
        self.assertTrue(watched)
        execv.assert_not_called()


class RotationTests(Folders):
    def setUp(self):
        super().setUp()
        self.home.mkdir(mode=0o700)
        (self.home / "config.xml").write_text(GENERATED)

    def test_the_key_is_rotated_over_the_socket_and_the_old_one_stops_working_at_once(self):
        fake = FakeSyncthing(self, OLD, OWN, str(self.socket))
        with no_leaks(self, OLD, NEW):
            self.assertTrue(syncthing.key_rotated(self.home, {"syncthing": NEW}, self.socket)())
        self.assertEqual(fake.key, NEW)
        self.assertEqual(syncthing.status(self.socket, "GET", "/rest/system/version", OLD), 403)
        patched = [received for received in fake.requests if received.method == "PATCH"]
        self.assertEqual([(received.path, received.headers["x-api-key"], received.body) for received in patched],
                         [("/rest/config/gui", OLD, b'{"apiKey": "' + NEW.encode() + b'"}')])

    def test_not_ready_until_syncthing_answers_and_not_rotated_when_it_refuses_the_old_key(self):
        ready = syncthing.key_rotated(self.home, {"syncthing": NEW}, self.socket)
        self.assertFalse(ready())
        fake = FakeSyncthing(self, "another-key-canary-0123456789", OWN, str(self.socket))
        self.assertFalse(ready())
        self.assertEqual(fake.key, "another-key-canary-0123456789")
        fake.key = OLD
        fake.status = 500
        self.assertFalse(ready())
        fake.status = 0
        with no_leaks(self, OLD, NEW):
            self.assertTrue(ready())
            patches = sum(received.method == "PATCH" for received in fake.requests)
            self.assertTrue(ready())
        self.assertEqual(sum(received.method == "PATCH" for received in fake.requests), patches)

    def test_a_key_that_cannot_go_in_a_header_and_a_reply_that_is_not_http_are_refused(self):
        (self.home / "config.xml").write_text(GENERATED.replace(OLD, "bad key\r\nX-Other: 1"))
        self.assertFalse(syncthing.key_rotated(self.home, {"syncthing": NEW}, self.socket)())
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(self.socket))
            listener.listen(1)

            def answer():
                connection, _ = listener.accept()
                connection.recv(4096)
                connection.sendall(b"nonsense\r\n")
                connection.close()

            threading.Thread(target=answer, daemon=True).start()
            self.assertRaisesRegex(syncthing.Refused, "did not answer in HTTP", syncthing.status, self.socket, "GET", "/rest/system/version", NEW)


class SupervisedTests(Folders):
    def test_syncthing_starts_before_the_app_and_the_app_gets_the_rotated_key_on_a_pipe(self):
        (self.home).mkdir(mode=0o700)
        (self.home / "config.xml").write_text(GENERATED)
        fake = FakeSyncthing(self, OLD, OWN, str(self.socket))
        held = {"token": "t" * 43, "viewer": "v" * 43, "syncthing": NEW}
        out = self.data / "app-got"
        app = plan.Spec("app", (sys.executable, "-c", f"import os; open({str(out)!r}, 'w').write(os.read(int(os.environ['FD']), 100).decode())"),
                        {"PATH": "/usr/bin:/bin", "FD": plan.PIPE["syncthing"]}, secrets=("syncthing",))
        st = plan.Spec("syncthing", (sys.executable, "-c", "import time; time.sleep(30)"), {"PATH": "/usr/bin:/bin"},
                       ready=syncthing.key_rotated(self.home, held, self.socket))
        running = supervisor.Supervisor([st, app], held, base=5.0, stop_seconds=1.0, tick=0.02)
        thread = threading.Thread(target=running.run)
        with no_leaks(self, OLD, NEW):
            thread.start()
            deadline = time.monotonic() + 10
            while not out.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            running.stop()
            thread.join(10)
        self.assertEqual(out.read_text(), NEW)
        self.assertEqual(fake.key, NEW)


class PlanAndMainTests(unittest.TestCase):
    def test_with_the_setting_on_syncthing_comes_first_and_only_the_app_is_handed_its_key(self):
        specs = plan.specs(GIVEN, bool, bool, bool)
        self.assertEqual([spec.name for spec in specs], ["syncthing", "app", "display", "vnc", "viewer", "worker"])
        self.assertEqual([spec.secrets for spec in specs], [(), ("token", "viewer", "syncthing"), (), (), ("viewer",), ("token",)])
        self.assertEqual(specs[0].argv, ("python3", "-P", "-m", "darkroom_init.syncthing", "/appdata/syncthing"))
        self.assertIs(specs[0].ready, bool)
        self.assertEqual(specs[1].env[plan.SYNCTHING_KEY_FD], plan.PIPE["syncthing"])
        self.assertEqual(specs[1].env["DARKROOM_SYNCTHING"], "1")
        self.assertFalse({plan.SYNCTHING_KEY_FD} & {name for spec in specs[2:] for name in spec.env})
        self.assertNotIn(plan.SYNCTHING_KEY_FD, plan.app_env(GIVEN | {"DARKROOM_SYNCTHING": "0"}))

    def test_with_the_setting_off_nothing_about_syncthing_is_planned(self):
        for value in ("", "0", "yes", "true"):
            with self.subTest(value=value):
                specs = plan.specs(GIVEN | {"DARKROOM_SYNCTHING": value}, bool, bool, bool)
                self.assertEqual([spec.name for spec in specs], ["app", "display", "vnc", "viewer", "worker"])
                self.assertEqual(specs[0].secrets, ("token", "viewer"))

    def test_the_init_and_the_app_agree_on_the_socket_and_the_home_is_under_appdata(self):
        self.assertEqual(syncthing.SOCKET, app_config.SYNCTHING_SOCKET)
        self.assertEqual(plan.syncthing_home({}), "/data/syncthing")
        image = (ROOT / "Dockerfile.single").read_text()
        self.assertIn("mkdir -p /data /photos /tmp/.X11-unix /run/darkroom", image)
        self.assertIn("&& chmod 0700 /run/darkroom", image)
        self.assertIn("COPY --from=syncthing /syncthing /usr/local/bin/syncthing", image)

    def test_main_holds_a_third_fresh_key_only_with_the_setting_on(self):
        for given, names in ((GIVEN, ["syncthing", "token", "viewer"]), (GIVEN | {"DARKROOM_SYNCTHING": ""}, ["token", "viewer"])):
            held = {}

            def run(self, held=held):
                held.update(self.secrets)
                return 0

            with self.subTest(names=names), no_leaks(self, "never-a-real-key-value") as watched, mock.patch.object(supervisor, "prctl", return_value=True), \
                    mock.patch.object(supervisor.Supervisor, "run", run), mock.patch("signal.signal"):
                self.assertEqual(init_main.main(given), 0)
                watched.update(held.values())
            self.assertEqual(sorted(held), names)
            self.assertEqual(len(set(held.values())), len(names))
            self.assertTrue(all(len(value) == 43 for value in held.values()))


if __name__ == "__main__":
    unittest.main()
