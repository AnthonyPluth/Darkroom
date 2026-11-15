import dataclasses
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import run
from darkroom import config
from darkroom.domain import events, syncthing_version
from darkroom.providers import exiftool, syncthing
from darkroom.server import jobs
from darkroom.storage import db
from tests.fakes.syncthing import FakeSyncthing
from tests.privacy import no_leaks
from tests.shared import own_database, settings_from

KEY = "handed-key-canary-0123456789abcdef0123456"
OWN = "PP2OO7B-2FRGILR-MSIJEZX-6KTCQJN-2VUJME3-NY3V3OD-5XW7TBJ-Y4OF4A2"


def handed(value):
    read_end, write_end = os.pipe()
    os.write(write_end, value.encode())
    os.close(write_end)
    return read_end


def on_socket(test):
    folder = tempfile.TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    path = Path(folder.name) / "syncthing.sock"
    return FakeSyncthing(test, KEY, OWN, str(path)), path


class VersionRangeTests(unittest.TestCase):
    def test_only_a_release_inside_the_tested_range_is_tested(self):
        self.assertTrue(syncthing_version.tested(syncthing.Version("v2.1.7", True)))
        for version, release in (("v2.1.6", True), ("v2.1.8", True), ("v2.2.0", True), ("v1.29.5", True), ("v3.0.0", True), ("v2.1.7", False),
                                 ("v2.1.7-rc.1", True), ("2.1.7", True), ("v2.1.7.1", True), ("", True), ("v99999.1.7", True)):
            with self.subTest(version=version, release=release):
                self.assertFalse(syncthing_version.tested(syncthing.Version(version, release)))

    def test_the_reason_names_the_version_only_when_it_has_the_shape_of_one(self):
        self.assertEqual(syncthing_version.untested(syncthing.Version("v2.2.0", True)), "Syncthing reported v2.2.0, outside the tested range v2.1.7")
        self.assertEqual(syncthing_version.untested(syncthing.Version("v2.1.7", False)),
                         "Syncthing reported v2.1.7, not a release build, outside the tested range v2.1.7")
        self.assertEqual(syncthing_version.untested(syncthing.Version("v2.1.7 <script>", True)),
                         "Syncthing reported an unrecognised version, outside the tested range v2.1.7")
        with mock.patch.object(syncthing_version, "TESTED_UP_TO", (2, 1, 9)):
            self.assertEqual(syncthing_version.tested_range(), "v2.1.7 to v2.1.9")
            self.assertTrue(syncthing_version.tested(syncthing.Version("v2.1.9", True)))


class StartupGateTests(unittest.TestCase):
    def setUp(self):
        self.engine = own_database(self)
        self.fake, self.socket = on_socket(self)

    def gate(self, client):
        with no_leaks(self, KEY, database=self.engine), db.session(self.engine, writing=True) as opened:
            return syncthing_version.at_startup(opened, client, 100.0)

    def recorded(self):
        with db.session(self.engine) as opened:
            return [(entry.type, entry.subject, entry.detail) for entry in events.recent(opened)]

    def test_a_tested_release_lets_the_transport_start_and_records_nothing(self):
        self.assertEqual(self.gate(syncthing.Client.on_socket(str(self.socket), KEY)), syncthing_version.Gate(True))
        self.assertEqual(self.recorded(), [])
        self.assertEqual([(received.method, received.path) for received in self.fake.requests], [("GET", "/rest/system/version")])

    def test_a_version_outside_the_range_keeps_the_transport_off_with_an_event(self):
        self.fake.version = {"version": "v2.2.0", "isRelease": True}
        gate = self.gate(syncthing.Client.on_socket(str(self.socket), KEY))
        reason = "Syncthing reported v2.2.0, outside the tested range v2.1.7"
        self.assertEqual(gate, syncthing_version.Gate(False, reason))
        self.assertEqual(self.recorded(), [("syncthing_transport_off", "syncthing", reason)])

    def test_a_refused_key_a_silent_syncthing_and_no_key_each_keep_the_transport_off_with_their_reason(self):
        silent = syncthing.Client.on_socket(str(self.socket.with_name("missing.sock")), KEY, timeout=1.0)
        for client, reason in ((syncthing.Client.on_socket(str(self.socket), "another-key-canary-0123"), syncthing_version.KEY_REFUSED),
                               (silent, syncthing_version.NOT_ANSWERING), (None, syncthing_version.NO_KEY)):
            with self.subTest(reason=reason):
                self.assertEqual(self.gate(client), syncthing_version.Gate(False, reason))
        self.assertEqual([detail for _, _, detail in self.recorded()], [syncthing_version.NO_KEY, syncthing_version.NOT_ANSWERING, syncthing_version.KEY_REFUSED])


class ClientOnSocketTests(unittest.TestCase):
    def test_the_provider_speaks_to_syncthing_on_its_unix_socket(self):
        fake, path = on_socket(self)
        client = syncthing.Client.on_socket(str(path), KEY)
        with no_leaks(self, KEY):
            self.assertEqual(client.version(), syncthing.Version("v2.1.7", True))
            self.assertNotIn("apiKey", client.gui())
            self.assertRaises(syncthing.KeyRefused, syncthing.Client.on_socket(str(path), "another-key-canary-0123").version)
            fake.status = 500
            with self.assertRaisesRegex(syncthing.SyncthingFailed, "^Syncthing answered 500$"):
                client.version()
        self.assertEqual({received.headers.get("x-api-key") for received in fake.requests if received.path == "/rest/config/gui"}, {KEY})
        self.assertNotIn(KEY, repr(client))

    def test_a_socket_client_needs_an_absolute_path_and_no_other_address(self):
        for url, path in ((syncthing.ON_SOCKET, "relative.sock"), (syncthing.ON_SOCKET, "/run/x\x00.sock"), ("http://127.0.0.1:8384", "/run/darkroom/syncthing.sock")):
            with self.subTest(url=url, path=path), self.assertRaisesRegex(syncthing.SyncthingFailed, "^the Syncthing address is not valid$"):
                syncthing.Client(url, KEY, socket=path)


class AppSettingTests(unittest.TestCase):
    def setUp(self):
        config.handed_tokens.clear()
        self.addCleanup(config.handed_tokens.clear)

    def test_syncthing_is_off_by_default_and_the_key_comes_only_from_its_pipe(self):
        self.assertEqual((settings_from().syncthing, settings_from().syncthing_key), (False, ""))
        self.assertEqual(settings_from(DARKROOM_SYNCTHING="yes").syncthing, False)
        fd = handed(KEY + "\n")
        settings = settings_from(DARKROOM_SYNCTHING="1", DARKROOM_SYNCTHING_KEY_FD=str(fd))
        self.assertEqual((settings.syncthing, settings.syncthing_key), (True, KEY))
        self.assertRaises(OSError, os.fstat, fd)
        self.assertNotIn(KEY, repr(settings))
        self.assertEqual(settings_from(DARKROOM_SYNCTHING_KEY="typed-by-hand-0123456789").syncthing_key, "")

    def test_check_config_reports_syncthing_never_shows_its_key_and_names_it_outside_the_one_container(self):
        fd = handed(KEY)
        with tempfile.TemporaryDirectory() as folder, no_leaks(self, KEY), mock.patch.object(exiftool, "version", return_value="13.0"), \
                mock.patch.dict(os.environ, {"DARKROOM_SYNCTHING": "1", "DARKROOM_SYNCTHING_KEY_FD": str(fd), "DARKROOM_ONE_CONTAINER": "1", "DARKROOM_PHOTOS_ROOT": folder,
                                             "DARKROOM_DATA_DIR": folder, "DARKROOM_PUBLIC_URL": "https://d.example", "DARKROOM_ALLOW_NO_AUTH": "1"}, clear=True):
            self.assertEqual(run.syncthing_line(config.load()), run.SYNCTHING_ON)
            self.assertEqual(run.main(["check-config"]), 0)
            os.environ.pop("DARKROOM_ONE_CONTAINER")
            self.assertEqual(config.syncthing_problems(config.load()), [config.SYNCTHING_TWO_CONTAINERS])
            self.assertEqual(run.syncthing_line(config.load()), run.SYNCTHING_OFF)
            self.assertEqual(run.main(["check-config"]), 1)
        self.assertEqual(config.syncthing_problems(settings_from(DARKROOM_ONE_CONTAINER="1")), [])


class SchedulerTests(unittest.TestCase):
    def test_a_gate_that_starts_off_is_rechecked_until_the_handed_key_works_with_one_event_per_reason(self):
        engine = own_database(self)
        fake, path = on_socket(self)
        fake.key = "persisted-key-canary-0123456789"
        settings = dataclasses.replace(settings_from(DARKROOM_PHOTOS_ROOT=tempfile.gettempdir()), syncthing=True, syncthing_key=KEY)
        with no_leaks(self, KEY, "persisted-key-canary-0123456789", database=engine), mock.patch.object(config, "SYNCTHING_SOCKET", path):
            scheduler = jobs.build(settings, engine, 100.0)
            self.assertEqual(scheduler.syncthing, syncthing_version.Gate(False, syncthing_version.KEY_REFUSED))
            scheduler.run_one("syncthing.gate", 160.0)
            scheduler.run_one("syncthing.gate", 220.0)
            self.assertFalse(scheduler.syncthing.on)
            fake.key = KEY
            scheduler.run_one("syncthing.gate", 280.0)
            self.assertEqual(scheduler.syncthing, syncthing_version.Gate(True))
            asked = len(fake.requests)
            scheduler.run_one("syncthing.gate", 340.0)
            self.assertEqual(len(fake.requests), asked)
        self.assertEqual(scheduler.jobs["syncthing.gate"].interval, jobs.SYNCTHING_RECHECK_SECONDS)
        self.assertNotIn("syncthing.gate", jobs.build(dataclasses.replace(settings, syncthing=False), engine, 100.0).jobs)
        with db.session(engine) as opened:
            self.assertEqual([entry.detail for entry in events.recent(opened, type=syncthing_version.EVENT)], [syncthing_version.KEY_REFUSED])


    def test_the_scheduler_holds_the_startup_gate_only_with_syncthing_on(self):
        engine = own_database(self)
        fake, path = on_socket(self)
        base = settings_from(DARKROOM_PHOTOS_ROOT=tempfile.gettempdir())
        self.assertIsNone(jobs.build(base, engine, 100.0).syncthing)
        self.assertEqual(fake.requests, [])
        with no_leaks(self, KEY, database=engine), mock.patch.object(config, "SYNCTHING_SOCKET", path):
            self.assertEqual(jobs.build(dataclasses.replace(base, syncthing=True, syncthing_key=KEY), engine, 100.0).syncthing, syncthing_version.Gate(True))
            self.assertEqual(jobs.build(dataclasses.replace(base, syncthing=True), engine, 100.0).syncthing, syncthing_version.Gate(False, syncthing_version.NO_KEY))
            self.assertEqual(jobs.build(dataclasses.replace(base, syncthing=True, syncthing_key="with space"), engine, 100.0).syncthing.reason, syncthing_version.NO_KEY)


if __name__ == "__main__":
    unittest.main()
