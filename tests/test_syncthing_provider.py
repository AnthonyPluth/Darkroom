import json
import logging
import traceback
import unittest
from unittest import mock

from darkroom import tls
from darkroom.providers import syncthing
from tests.fakes.syncthing import FakeSyncthing
from tests.privacy import no_leaks

KEY = "st-canary-key-7f3a91c2"
OWN = "PP2OO7B-2FRGILR-MSIJEZX-6KTCQJN-2VUJME3-NY3V3OD-5XW7TBJ-Y4OF4A2"
PIXEL = "Q3MOJ6A-G5J2LNT-GLBJBJK-SRN7XPH-UQGP5F6-5O5XYBV-LRR7YIV-LSUJCQX"
PATH = "/photos/_pixels/canary-pixel-path"


class DeviceIdTests(unittest.TestCase):
    def test_made_up_certificates_give_the_known_ids(self):
        self.assertEqual(syncthing.device_id_of_certificate(b"made-up certificate of a test pixel"), PIXEL)
        self.assertEqual(syncthing.device_id_of_certificate(b"made-up certificate of darkroom"), OWN)

    def test_a_good_id_is_accepted_in_any_case_and_without_dashes(self):
        self.assertEqual(syncthing.device_id(PIXEL), PIXEL)
        self.assertEqual(syncthing.device_id(PIXEL.lower().replace("-", "")), PIXEL)
        self.assertEqual(syncthing.device_id(f" {PIXEL} "), PIXEL)
        self.assertTrue(syncthing.is_device_id(OWN))

    def test_the_check_characters_are_luhn_mod_32_over_each_chunk_of_13(self):
        characters = PIXEL.replace("-", "")
        self.assertEqual([syncthing.check_character(characters[start:start + 13]) for start in range(0, 56, 14)],
                         [characters[start + 13] for start in range(0, 56, 14)])
        self.assertEqual(syncthing.check_character("AAAAAAAAAAAAA"), "A")
        self.assertEqual(syncthing.check_character("BAAAAAAAAAAAA"), "7")
        self.assertEqual(syncthing.check_character("ABAAAAAAAAAAA"), "6")

    def test_wrong_lengths_and_characters_are_refused(self):
        for value in (PIXEL[:-1], PIXEL + "A", "", PIXEL.replace("Q", "1", 1), PIXEL.replace("Q", "0", 1), 56, None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(syncthing.InvalidDeviceId, "^not a Syncthing device ID$"):
                    syncthing.device_id(value)

    def test_each_changed_character_is_caught(self):
        characters = PIXEL.replace("-", "")
        for position in range(56):
            replacement = "A" if characters[position] != "A" else "B"
            corrupted = characters[:position] + replacement + characters[position + 1:]
            with self.subTest(position=position):
                self.assertFalse(syncthing.is_device_id(corrupted))
        with self.assertRaisesRegex(syncthing.InvalidDeviceId, "check character"):
            syncthing.device_id(PIXEL[:-1] + "A")

    def test_two_swapped_neighbours_are_caught(self):
        characters = PIXEL.replace("-", "")
        swapped = characters[1] + characters[0] + characters[2:]
        self.assertFalse(syncthing.is_device_id(swapped))


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSyncthing(self, KEY, OWN)
        self.client = syncthing.Client(self.fake.url, KEY, timeout=5.0)

    def add(self, paused=True):
        return self.client.add_folder("pixel-1", PATH, [PIXEL], paused=paused)

    def test_every_request_carries_the_key_in_x_api_key(self):
        self.client.own_device_id()
        self.client.folders()
        self.assertEqual({received.headers.get("x-api-key") for received in self.fake.requests}, {KEY})
        self.assertFalse(any("authorization" in received.headers for received in self.fake.requests))

    def test_own_id_and_version(self):
        self.assertEqual(self.client.own_device_id(), OWN)
        self.assertEqual(self.client.version(), syncthing.Version("v2.1.7", True))

    def test_a_folder_is_added_send_only_paused_and_read_back(self):
        folder = self.add()
        self.assertEqual((folder.id, folder.path, folder.type, folder.paused), ("pixel-1", PATH, "sendonly", True))
        self.assertEqual(folder.devices, {PIXEL, OWN})
        posted = json.loads(next(received.body for received in self.fake.requests if received.method == "POST"))
        self.assertEqual(posted, {"id": "pixel-1", "path": PATH, "type": "sendonly", "paused": True, "devices": [{"deviceID": PIXEL}]})
        self.assertEqual([folder.id for folder in self.client.folders()], ["pixel-1"])

    def test_a_configured_folder_is_never_added_again(self):
        self.add()
        with self.assertRaisesRegex(syncthing.SyncthingFailed, "^the folder is already configured$"):
            self.add(paused=False)
        self.assertEqual(sum(received.method == "POST" for received in self.fake.requests), 1)

    def test_a_folder_stored_as_another_type_is_a_failure(self):
        self.fake.forced_type = "sendreceive"
        with self.assertRaisesRegex(syncthing.NotSendOnly, "^Syncthing did not keep the folder Send Only$"):
            self.add()
        self.fake.forced_type = None
        self.fake.folders["persisted"] = {"id": "persisted", "path": PATH, "type": "sendreceive", "paused": True, "devices": []}
        self.assertEqual(self.client.update_folder("persisted").type, "sendonly")

    def test_an_unknown_type_is_stored_as_send_receive_by_the_fake_as_by_syncthing(self):
        self.add()
        self.fake.folders["pixel-1"] = self.fake.stored_folder(self.fake.folders["pixel-1"] | {"type": "SendOnly"})
        self.assertEqual(self.client.folder("pixel-1").type, "sendreceive")

    def test_a_write_syncthing_did_not_keep_is_a_failure(self):
        self.add()
        self.fake.forgetful = True
        for write in (lambda: self.client.update_folder("pixel-1", paused=False), lambda: self.client.update_folder("pixel-1", devices=[]),
                      lambda: self.client.add_device(PIXEL, "Pixel"), lambda: self.client.set_options({"relaysEnabled": True}),
                      lambda: self.client.set_gui({"unixSocketPermissions": "0644"}), lambda: self.client.set_ignores("pixel-1", ["*.xmp"]),
                      lambda: self.client.remove_folder("pixel-1"), lambda: self.client.remove_device(OWN)):
            with self.subTest(write=write), self.assertRaises(syncthing.ReadBackFailed):
                write()
        with self.assertRaisesRegex(syncthing.ReadBackFailed, "as written"):
            self.client.add_folder("pixel-2", PATH, [PIXEL])

    def test_a_folder_is_paused_resumed_shared_and_removed(self):
        self.add()
        self.assertFalse(self.client.update_folder("pixel-1", paused=False).paused)
        self.assertEqual(self.client.update_folder("pixel-1", devices=[]).devices, {OWN})
        self.assertEqual([json.loads(received.body)["type"] for received in self.fake.requests if received.method == "PATCH"], ["sendonly", "sendonly"])
        self.client.remove_folder("pixel-1")
        self.assertEqual(self.client.folders(), [])

    def test_devices_are_added_paused_resumed_and_removed(self):
        device = self.client.add_device(PIXEL.lower(), "Pixel")
        self.assertEqual((device.device_id, device.name, device.paused), (PIXEL, "Pixel", True))
        self.assertFalse(self.client.update_device(PIXEL, paused=False).paused)
        self.assertEqual({device.device_id for device in self.client.devices()}, {OWN, PIXEL})
        self.client.remove_device(PIXEL)
        self.assertEqual([device.device_id for device in self.client.devices()], [OWN])

    def test_options_and_gui_are_set_and_read_back(self):
        self.assertTrue(self.client.set_options({"localAnnounceEnabled": True})["localAnnounceEnabled"])
        self.assertFalse(self.client.options()["relaysEnabled"])
        self.assertEqual(self.client.set_gui({"unixSocketPermissions": "0600"})["unixSocketPermissions"], "0600")

    def test_the_api_key_is_stripped_from_every_configuration_reply(self):
        self.assertNotIn("apiKey", self.client.gui())
        self.assertNotIn("password", self.client.gui())
        self.assertNotIn(KEY, json.dumps(self.client.configuration()))
        self.assertNotIn(KEY, json.dumps(self.client.call("GET", "/rest/config")))

    def test_the_api_key_and_password_are_never_written(self):
        for changes in ({"apiKey": "new-canary-key-1234"}, {"password": "x"}):
            with self.subTest(changes=changes), self.assertRaisesRegex(syncthing.SyncthingFailed, "does not change"):
                self.client.set_gui(changes)
        self.assertEqual(self.fake.requests, [])

    def test_ignore_patterns_are_set_and_read_back(self):
        self.add()
        self.assertEqual(self.client.ignores("pixel-1"), ())
        self.assertEqual(self.client.set_ignores("pixel-1", ["*.xmp"]), ("*.xmp",))
        self.assertEqual(self.client.ignores("pixel-1"), ("*.xmp",))
        self.assertEqual(self.fake.requests[-2].query, {"folder": ["pixel-1"]})
        with self.assertRaisesRegex(syncthing.SyncthingFailed, "ignore pattern"):
            self.client.set_ignores("pixel-1", ["*.xmp\n!*"])

    def test_scan_and_a_folder_in_error(self):
        self.add()
        self.client.scan("pixel-1")
        self.assertEqual(self.fake.scans, ["pixel-1"])
        self.fake.errors.add("pixel-1")
        with self.assertRaises(syncthing.SyncthingFailed) as caught:
            self.client.scan("pixel-1")
        self.assertEqual((str(caught.exception), caught.exception.status), ("Syncthing answered 500", 500))

    def test_completion_connections_stats_and_pending_devices(self):
        self.add()
        self.fake.completions[("pixel-1", PIXEL)] = {"completion": 87.5, "globalBytes": 400, "globalItems": 4, "needBytes": 50,
                                                      "needItems": 1, "needDeletes": 2, "remoteState": "valid", "sequence": 9}
        self.assertEqual(self.client.completion("pixel-1", PIXEL), syncthing.Completion(87.5, 400, 4, 50, 1, 2, "valid"))
        self.fake.connections = {PIXEL: {"connected": True, "type": "tcp-client"}, "not-an-id": {"connected": True}}
        self.assertEqual(self.client.connections(), {PIXEL: syncthing.Connection(True, "tcp-client")})
        self.fake.stats = {PIXEL: {"lastSeen": "2026-11-14T10:00:00Z", "lastConnectionDurationS": 12.5}, OWN: "odd"}
        self.assertEqual(self.client.device_stats(), {PIXEL: syncthing.DeviceStats("2026-11-14T10:00:00Z", 12.5)})
        self.fake.pending = {PIXEL: {"name": "Pixel", "address": "192.0.2.7:22000", "time": "2026-11-14T10:00:00Z"}, "bogus": {}}
        self.assertEqual(self.client.pending_devices(), [syncthing.PendingDevice(PIXEL, "Pixel", "192.0.2.7:22000", "2026-11-14T10:00:00Z")])

    def test_bad_ids_names_and_paths_are_refused_before_any_request(self):
        for call in (lambda: self.client.folder("../config"), lambda: self.client.scan("a b"), lambda: self.client.add_folder("p", "relative", [PIXEL]), lambda: self.client.add_folder("a/b", PATH, [PIXEL]),
                     lambda: self.client.add_folder("p", PATH, [PIXEL[:-1] + "A"]), lambda: self.client.add_device(PIXEL, "x" * 65),
                     lambda: self.client.update_device("nope", paused=True)):
            with self.subTest(call=call), self.assertRaises(syncthing.SyncthingFailed):
                call()
        self.assertEqual(self.fake.requests, [])

    def test_only_the_verified_endpoints_can_be_called(self):
        for method, path in (("PUT", "/rest/config"), ("POST", "/rest/db/override"), ("PUT", "/rest/config/folders/pixel-1"),
                             ("POST", "/rest/system/shutdown"), ("GET", "/rest/config/devices/x")):
            with self.subTest(method=method, path=path), self.assertRaisesRegex(syncthing.SyncthingFailed, "not allowed"):
                self.client.call(method, path)
        self.assertEqual(self.fake.requests, [])
        self.assertFalse(hasattr(self.client, "override"))

    def test_a_wrong_key_is_refused_with_a_fixed_text(self):
        with self.assertRaises(syncthing.KeyRefused) as caught:
            syncthing.Client(self.fake.url, "wrong-canary-key-99").own_device_id()
        self.assertEqual((str(caught.exception), caught.exception.status), ("Syncthing refused the API key", 403))

    def test_unusable_addresses_and_keys(self):
        for url, key in (("ftp://127.0.0.1", KEY), ("http://u:p@127.0.0.1", KEY), ("http://127.0.0.1/rest", KEY), ("http://", KEY),
                         ("http://[::1", KEY), (self.fake.url, ""), (self.fake.url, "has space")):
            with self.subTest(url=url, key=key), self.assertRaises(syncthing.SyncthingFailed):
                syncthing.Client(url, key)

    def test_odd_replies_are_failures(self):
        self.fake.raw = b"not json"
        with self.assertRaisesRegex(syncthing.SyncthingFailed, "not JSON"):
            self.client.version()
        for raw, call in ((b"[]", self.client.version), (b"{}", self.client.version), (b"{}", self.client.folders), (b"{}", self.client.devices),
                          (b"[{}]", self.client.folders), (b'{"devices": [{"deviceID": "x"}]}', self.client.devices),
                          (b'{"devices": [{"deviceID": "' + PIXEL.encode() + b'"}]}', self.client.devices),
                          (b'{"ignore": [1]}', lambda: self.client.ignores("pixel-1"))):
            self.fake.raw = raw
            self.fake.folders["pixel-1"] = {}
            with self.subTest(raw=raw), self.assertRaises(syncthing.SyncthingFailed):
                call()
        self.fake.raw = b""
        self.assertIsNone(self.client.call("POST", "/rest/db/scan", {"folder": "pixel-1"}))
        with mock.patch.object(syncthing, "MAX_REPLY", 4):
            self.fake.raw = b'{"myID": 1}'
            with self.assertRaisesRegex(syncthing.SyncthingFailed, "too large"):
                self.client.own_device_id()

    def test_an_unreachable_or_refused_server_is_a_fixed_failure(self):
        self.fake.server.shutdown()
        self.fake.server.server_close()
        with self.assertRaisesRegex(syncthing.SyncthingFailed, r"^Syncthing could not be reached \(\w+\)$"):
            self.client.own_device_id()
        with mock.patch.object(tls, "urlopen", side_effect=tls.Refused("redirects are not followed")), \
                self.assertRaisesRegex(syncthing.SyncthingFailed, "refused before it was sent"):
            self.client.own_device_id()
        with self.assertRaisesRegex(syncthing.SyncthingFailed, "refused before"):
            syncthing.Client("http://syncthing.example.com:8384", KEY).own_device_id()


class NoLeakTests(unittest.TestCase):
    def test_the_key_device_ids_and_paths_appear_in_no_exception_log_or_repr(self):
        fake = FakeSyncthing(self, KEY, OWN)
        client = syncthing.Client(fake.url, KEY, timeout=5.0)
        caught: list[BaseException] = []
        with no_leaks(self, KEY, PIXEL, PATH) as watched:
            watched.add(OWN)
            logging.getLogger().warning("client %r", client)
            print(repr(client), repr(client.add_folder("pixel-1", PATH, [PIXEL])), repr(client.devices()), client.gui(), client.options())
            fake.forced_type = "receiveonly"
            fake.errors.add("pixel-1")
            fake.raw = None
            for call in (lambda: client.add_folder("pixel-2", PATH, [PIXEL]), lambda: client.scan("pixel-1"),
                         lambda: syncthing.Client(fake.url, KEY + "x").folders(), lambda: client.set_gui({"apiKey": KEY}),
                         lambda: client.add_device(PIXEL[:-1] + "A", "Pixel"), lambda: syncthing.Client(fake.url, KEY + " ")):
                try:
                    call()
                except syncthing.SyncthingFailed as error:
                    caught.append(error)
            for error in caught:
                print("".join(traceback.format_exception(error)), repr(error))
            fake.status = 500
            try:
                client.connections()
            except syncthing.SyncthingFailed as error:
                print("".join(traceback.format_exception(error)))
        self.assertEqual(len(caught), 6)
        self.assertTrue(all(error.__suppress_context__ or error.__context__ is None for error in caught))
