import contextlib
import json
import shutil
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import select

from darkroom.domain import pixel_check, pixel_folder, transport
from darkroom.providers import syncthing
from darkroom.storage.models import Event, Pixel, PixelTransport
from tests import test_pixel_check, test_pixel_folder
from tests.fakes.syncthing import FakeSyncthing
from tests.privacy import no_leaks

NOW = test_pixel_folder.NOW
KEY = "st-transport-canary-key-51c0"
OWN = syncthing.device_id_of_certificate(b"made-up certificate of darkroom in the transport tests")
JO_DEVICE = syncthing.device_id_of_certificate(b"made-up certificate of jo's pixel")
SAM_DEVICE = syncthing.device_id_of_certificate(b"made-up certificate of sam's pixel")
STRANGER = syncthing.device_id_of_certificate(b"made-up certificate of a device nobody asked for")


class ReconcilerTests(unittest.TestCase):
    setUp_pixels = test_pixel_folder.PixelFolderTests.setUp
    account = test_pixel_folder.PixelFolderTests.account
    item = test_pixel_folder.PixelFolderTests.item
    copies = test_pixel_folder.PixelFolderTests.copies
    copy_path = test_pixel_folder.PixelFolderTests.copy_path
    tend = test_pixel_folder.PixelFolderTests.tend

    def setUp(self):
        self.setUp_pixels()
        self.fake = FakeSyncthing(self, KEY, OWN)
        self.fake.options["localAnnounceEnabled"] = False
        self.client = syncthing.Client(self.fake.url, KEY, timeout=5.0)
        self.held: set[int] = set()
        self.pair(self.jo_pixel, JO_DEVICE)
        self.pair(self.sam_pixel, SAM_DEVICE)

    def pair(self, pixel_id: int, device: str | None) -> None:
        self.opened.get(Pixel, pixel_id).syncthing_device_id = device
        self.opened.commit()

    def reconcile(self, share_problem: str | None = None, at: float = NOW) -> transport.Outcome:
        return transport.reconcile(self.opened, self.client, self.photos, lambda pixel_id: pixel_id in self.held, share_problem, at)

    def folder_id(self, pixel_id: int) -> str:
        return transport.folder_of(pixel_id)

    def persisted(self, pixel_id: int, device: str, **changes: object) -> str:
        identity = self.folder_id(pixel_id)
        self.fake.folders[identity] = {"id": identity, "path": str(self.photos.pixel_root(pixel_id)), "type": "sendonly", "paused": True,
                                       "devices": [{"deviceID": device}, {"deviceID": OWN}], "versioning": {"type": ""}} | changes
        self.fake.devices[device] = {"deviceID": device, "name": "persisted", "paused": True}
        self.fake.ignores[identity] = ["*.xmp"]
        return identity

    def writes(self) -> list[tuple[str, str, object]]:
        return [(received.method, received.path, json.loads(received.body) if received.body else None)
                for received in self.fake.requests if received.method != "GET"]

    def first_resume(self) -> int:
        found = [index for index, (method, _, body) in enumerate(self.writes()) if method == "PATCH" and isinstance(body, dict) and body.get("paused") is False]
        return found[0] if found else len(self.writes())

    def removal(self, identity: str) -> int:
        return self.writes().index(("DELETE", f"/rest/config/folders/{identity}", None))

    def rows(self) -> dict[int, tuple[bool, str | None]]:
        return {row.pixel_id: (row.paired, row.error) for row in self.opened.execute(select(PixelTransport)).scalars()}

    def events(self, kind: str) -> list[tuple[str, str | None]]:
        return [(row.subject, row.detail) for row in self.opened.execute(select(Event).where(Event.type == kind).order_by(Event.id)).scalars()]

    def tearDown(self):
        writes = self.writes()
        self.assertFalse(any("override" in path or method == "PUT" for method, path, _ in writes))
        removed: set[str] = set()
        for method, path, body in writes:
            if method == "DELETE" and path.startswith("/rest/config/folders/"):
                removed.add(path.rsplit("/", 1)[1])
            if method == "POST" and path == "/rest/config/folders" and isinstance(body, dict):
                self.assertNotIn(body["id"], removed, "a folder was removed and added again")

    def test_the_wanted_configuration_is_one_send_only_folder_per_pixel_at_its_photos_path_shared_with_its_device_alone(self):
        outcome = self.reconcile()
        self.assertIsNone(outcome.failure)
        self.assertEqual(outcome.resumed, {self.jo_pixel, self.sam_pixel})
        for pixel_id, device in ((self.jo_pixel, JO_DEVICE), (self.sam_pixel, SAM_DEVICE)):
            folder = self.client.folder(self.folder_id(pixel_id))
            with self.subTest(pixel=pixel_id):
                self.assertEqual((folder.type, folder.path, folder.devices, folder.paused, folder.versioning),
                                 ("sendonly", str(self.photos.pixel_root(pixel_id)), {device, OWN}, False, ""))
                self.assertTrue(Path(folder.path).is_relative_to(self.root / "_pixels"))
                self.assertEqual(self.client.ignores(folder.id), ("*.xmp",))
        self.assertEqual({folder.id for folder in self.client.folders()}, {self.folder_id(self.jo_pixel), self.folder_id(self.sam_pixel)})
        self.assertEqual({device.device_id: device.paused for device in self.client.devices()}, {OWN: False, JO_DEVICE: False, SAM_DEVICE: False})
        self.assertTrue(self.fake.options["localAnnounceEnabled"])
        self.assertEqual(self.rows(), {self.jo_pixel: (True, None), self.sam_pixel: (True, None)})
        self.assertEqual(len(self.events(transport.FOLDER_ADDED)), 2)

    def test_a_new_folder_is_added_paused_and_unshared_and_gets_its_ignore_pattern_before_it_is_shared(self):
        self.pair(self.sam_pixel, None)
        self.reconcile()
        identity = self.folder_id(self.jo_pixel)
        writes = self.writes()
        added = writes.index(("POST", "/rest/config/folders", mock.ANY))
        self.assertEqual(writes[added][2]["devices"], [])
        self.assertTrue(writes[added][2]["paused"])
        ignored = writes.index(("POST", "/rest/db/ignores", {"ignore": ["*.xmp"]}))
        shared = next(index for index, (method, path, body) in enumerate(writes) if method == "PATCH" and path.endswith(identity) and body.get("devices"))
        self.assertLess(added, ignored)
        self.assertLess(ignored, shared)
        self.assertTrue(writes[shared][2]["paused"])
        self.assertLess(shared, self.first_resume())

    def test_any_other_folder_and_device_found_is_removed_before_anything_is_resumed(self):
        self.persisted(self.jo_pixel, JO_DEVICE)
        self.fake.folders["photos"] = {"id": "photos", "path": str(self.root / "jo" / "originals"), "type": "sendonly", "paused": True, "devices": [{"deviceID": STRANGER}]}
        self.fake.folders["darkroom-pixel-999"] = {"id": "darkroom-pixel-999", "path": str(self.root / "_pixels" / "999"), "type": "sendonly", "paused": True, "devices": []}
        self.fake.devices[STRANGER] = {"deviceID": STRANGER, "name": "stranger", "paused": True}
        outcome = self.reconcile()
        self.assertIsNone(outcome.failure)
        self.assertEqual({folder.id for folder in self.client.folders()}, {self.folder_id(self.jo_pixel), self.folder_id(self.sam_pixel)})
        self.assertNotIn(STRANGER, {device.device_id for device in self.client.devices()})
        resumed = self.first_resume()
        self.assertLess(self.removal("photos"), resumed)
        self.assertLess(self.removal("darkroom-pixel-999"), resumed)
        self.assertLess(self.writes().index(("DELETE", f"/rest/config/devices/{STRANGER}", None)), resumed)
        self.assertEqual(self.events(transport.CORRECTED), [("syncthing", transport.NOT_ASKED_FOR)] * 2 + [("syncthing", transport.DEVICE_NOT_ASKED_FOR)])

    def test_a_persisted_folder_that_is_not_send_only_is_removed_before_anything_is_resumed_and_never_added_again(self):
        identity = self.persisted(self.jo_pixel, JO_DEVICE, type="sendreceive")
        outcome = self.reconcile()
        self.assertIsNone(outcome.failure)
        self.assertLess(self.removal(identity), self.first_resume())
        self.assertNotIn(identity, {folder.id for folder in self.client.folders()})
        self.assertEqual(outcome.resumed, {self.sam_pixel})
        self.assertEqual(self.events(transport.CORRECTED), [(f"pixel {self.jo_pixel}", transport.NOT_SEND_ONLY)])
        self.assertEqual(self.events(transport.FOLDER_REMOVED), [(f"pixel {self.jo_pixel}", None)])
        self.assertEqual(self.rows()[self.jo_pixel], (False, transport.REMOVED))
        for at in (NOW + 10, NOW + 20):
            self.assertEqual(self.reconcile(at=at).resumed, {self.sam_pixel})
        self.assertNotIn(identity, {folder.id for folder in self.client.folders()})

    def test_a_device_id_problem_is_recorded_ahead_of_a_removed_folder_and_the_removal_shows_once_the_id_is_fixed(self):
        invalid = JO_DEVICE[:-1] + ("A" if JO_DEVICE[-1] != "A" else "B")
        for problem, device, code in (("invalid", invalid, transport.INVALID_DEVICE), ("shared", SAM_DEVICE, transport.SHARED_DEVICE)):
            with self.subTest(problem=problem):
                self.opened.query(Event).delete()
                self.opened.commit()
                self.fake.folders.clear()
                self.fake.requests.clear()
                identity = self.persisted(self.jo_pixel, JO_DEVICE, type="sendreceive")
                self.pair(self.jo_pixel, device)
                for at in (NOW, NOW + 10):
                    outcome = self.reconcile(at=at)
                    self.assertIsNone(outcome.failure)
                    self.assertEqual(self.rows()[self.jo_pixel], (False, code))
                self.assertNotIn(identity, self.fake.folders)
                self.assertEqual(self.events(transport.FOLDER_REMOVED), [(f"pixel {self.jo_pixel}", None)])
                self.pair(self.jo_pixel, JO_DEVICE)
                self.pair(self.sam_pixel, SAM_DEVICE)
                for at in (NOW + 20, NOW + 30):
                    outcome = self.reconcile(at=at)
                    self.assertEqual(self.rows()[self.jo_pixel], (False, transport.REMOVED))
                    self.assertNotIn(self.jo_pixel, outcome.resumed)
                self.assertNotIn(identity, self.fake.folders)

    def test_a_persisted_folder_outside_its_pixels_folder_is_removed_before_anything_is_resumed(self):
        for path in (str(self.root / "jo" / "originals"), str(self.root / "_pixels"), str(self.photos.pixel_root(self.sam_pixel)), "/elsewhere"):
            with self.subTest(path=path):
                self.fake.requests.clear()
                self.opened.query(Event).delete()
                self.opened.commit()
                identity = self.persisted(self.jo_pixel, JO_DEVICE, path=path)
                outcome = self.reconcile()
                self.assertLess(self.removal(identity), self.first_resume())
                self.assertNotIn(self.jo_pixel, outcome.resumed)
                self.assertEqual(self.events(transport.CORRECTED), [(f"pixel {self.jo_pixel}", transport.WRONG_PATH)])
                self.fake.folders.pop(self.folder_id(self.sam_pixel), None)

    def test_a_folder_without_the_xmp_pattern_is_corrected_while_paused(self):
        for patterns in (None, [], ["*.jpg"], ["!*.xmp", "*.xmp"]):
            with self.subTest(patterns=patterns):
                identity = self.persisted(self.jo_pixel, JO_DEVICE, paused=False)
                self.fake.ignores[identity] = patterns
                self.fake.requests.clear()
                self.reconcile()
                writes = self.writes()
                paused = writes.index(("PATCH", f"/rest/config/folders/{identity}", mock.ANY))
                self.assertTrue(writes[paused][2]["paused"])
                self.assertLess(paused, writes.index(("POST", "/rest/db/ignores", {"ignore": ["*.xmp"]})))
                self.assertEqual(self.fake.ignores[identity], ["*.xmp"])
                self.assertIn((f"pixel {self.jo_pixel}", transport.IGNORES_SET), self.events(transport.CORRECTED))

    def test_a_folder_shared_more_widely_or_keeping_versions_is_corrected_while_paused(self):
        for changes in ({"devices": [{"deviceID": JO_DEVICE}, {"deviceID": SAM_DEVICE}, {"deviceID": OWN}]}, {"devices": []},
                        {"versioning": {"type": "simple", "params": {"keep": "5"}}}):
            with self.subTest(changes=changes):
                identity = self.persisted(self.jo_pixel, JO_DEVICE, paused=False, **changes)
                self.fake.requests.clear()
                self.reconcile()
                folder = self.client.folder(identity)
                self.assertEqual((folder.devices, folder.versioning), ({JO_DEVICE, OWN}, ""))
                writes = self.writes()
                self.assertEqual(writes[0], ("PATCH", f"/rest/config/folders/{identity}", {"type": "sendonly", "versioning": {"type": ""}, "paused": True}))

    def test_a_failed_reconcile_resumes_nothing_and_keeps_the_transport_down(self):
        for breaking in ("forgetful", "forced_type", "status"):
            with self.subTest(breaking=breaking):
                self.fake.folders.clear()
                self.fake.devices = {OWN: {"deviceID": OWN, "name": "darkroom", "paused": False}}
                self.fake.forgetful, self.fake.forced_type, self.fake.status = breaking == "forgetful", "sendreceive" if breaking == "forced_type" else None, 0
                self.fake.requests.clear()
                if breaking == "status":
                    self.persisted(self.jo_pixel, JO_DEVICE)
                    self.fake.status = 500
                outcome = self.reconcile()
                self.fake.status = 0
                self.assertIsNotNone(outcome.failure)
                self.assertEqual(outcome.resumed, set())
                self.assertFalse(any(isinstance(body, dict) and body.get("paused") is False for _, _, body in self.writes()))
                self.assertFalse(self.fake.options["localAnnounceEnabled"])
                self.assertTrue(all(entry["paused"] for entry in self.fake.folders.values()))
                self.assertEqual(self.rows(), {self.jo_pixel: (False, transport.DOWN), self.sam_pixel: (False, transport.DOWN)})
        self.fake.forgetful, self.fake.forced_type = False, None
        self.assertEqual([detail for _, detail in self.events(transport.FAILED)], [transport.NOT_KEPT, transport.KEPT_NOT_SEND_ONLY, transport.UNREACHABLE])
        self.fake.folders.clear()
        self.assertEqual(self.reconcile(at=NOW + 50).resumed, {self.jo_pixel, self.sam_pixel})
        self.assertEqual(self.events(transport.RECOVERED), [("syncthing", None)])

    def test_a_held_pixels_folder_is_neither_added_nor_resumed_while_the_other_is(self):
        self.held = {self.jo_pixel}
        outcome = self.reconcile()
        self.assertEqual(outcome.resumed, {self.sam_pixel})
        self.assertNotIn(self.folder_id(self.jo_pixel), {folder.id for folder in self.client.folders()})
        self.assertEqual(self.rows()[self.jo_pixel], (False, transport.HELD))
        self.held = set()
        self.reconcile(at=NOW + 10)
        self.held = {self.jo_pixel}
        self.fake.requests.clear()
        outcome = self.reconcile(at=NOW + 20)
        self.assertEqual(outcome.resumed, {self.sam_pixel})
        self.assertTrue(self.client.folder(self.folder_id(self.jo_pixel)).paused)
        self.assertTrue(next(device for device in self.client.devices() if device.device_id == JO_DEVICE).paused)
        self.assertEqual(self.rows()[self.jo_pixel], (True, transport.HELD))

    def test_a_failed_share_check_adds_nothing_and_pauses_the_whole_transport(self):
        self.reconcile()
        self.assertTrue(all(not entry["paused"] for entry in self.fake.folders.values()))
        self.pair(self.jo_pixel, None)
        self.opened.add(Pixel(name="Third", google_account_id=self.jo_account, syncthing_device_id=STRANGER, created=1.0))
        self.opened.commit()
        self.pair(self.jo_pixel, JO_DEVICE)
        outcome = self.reconcile("the sentinel is missing from the share", at=NOW + 10)
        self.assertTrue(outcome.paused)
        self.assertEqual(outcome.resumed, set())
        self.assertEqual(len(self.fake.folders), 2)
        self.assertTrue(all(entry["paused"] for entry in self.fake.folders.values()))
        self.assertTrue(all(entry["paused"] for identity, entry in self.fake.devices.items() if identity != OWN))
        self.assertEqual(self.events(transport.PAUSED), [("syncthing", "the sentinel is missing from the share")])
        self.assertEqual(set(self.rows().values()), {(True, transport.SHARE), (False, transport.SHARE)})

    def test_pausing_for_the_share_check_pauses_every_folder_and_device(self):
        self.reconcile()
        moving = transport.Transport()
        moving.pause(self.opened, self.client, "the share root is not the filesystem it was at startup", NOW + 10)
        self.assertTrue(all(entry["paused"] for entry in self.fake.folders.values()))
        self.assertTrue(all(entry["paused"] for identity, entry in self.fake.devices.items() if identity != OWN))
        self.assertEqual(set(self.rows().values()), {(False, transport.SHARE)})
        self.assertIsNone(moving.approved)

    def test_stopping_pauses_whatever_it_can_reach_when_a_listing_or_a_pause_fails(self):
        failed = syncthing.SyncthingFailed("Syncthing answered 500", 500)
        real_update = syncthing.Client.update_device

        def refuse_jo(client, identity, paused):
            if identity == JO_DEVICE:
                raise failed
            return real_update(client, identity, paused)

        for broken, patched in (("folders", {"folders": mock.DEFAULT}), ("devices", {"devices": mock.DEFAULT}), ("own", {"own_device_id": mock.DEFAULT}),
                                ("one pause", {})):
            with self.subTest(broken=broken):
                self.reconcile()
                self.assertTrue(all(not entry["paused"] for entry in self.fake.folders.values()))
                with contextlib.ExitStack() as stack:
                    replaced = stack.enter_context(mock.patch.multiple(syncthing.Client, **patched)) if patched else {}
                    stack.enter_context(mock.patch.object(syncthing.Client, "update_device", autospec=True,
                                                          side_effect=refuse_jo if broken == "one pause" else real_update))
                    for each in replaced.values():
                        each.side_effect = failed
                    transport.stop(self.client)
                folders = {entry["paused"] for entry in self.fake.folders.values()}
                devices = {identity: entry["paused"] for identity, entry in self.fake.devices.items() if identity != OWN}
                self.assertEqual(folders, {False} if broken == "folders" else {True})
                if broken == "devices":
                    self.assertEqual(set(devices.values()), {False})
                elif broken == "one pause":
                    self.assertEqual(devices, {JO_DEVICE: False, SAM_DEVICE: True})
                else:
                    self.assertEqual(set(devices.values()), {True})
                self.held = set()

    def test_a_pixel_without_a_usable_device_id_gets_no_folder_and_its_persisted_one_stays_paused(self):
        identity = self.persisted(self.jo_pixel, JO_DEVICE, paused=False)
        self.pair(self.jo_pixel, JO_DEVICE[:-1] + ("A" if JO_DEVICE[-1] != "A" else "B"))
        outcome = self.reconcile()
        self.assertIn(identity, self.fake.folders)
        self.assertTrue(self.fake.folders[identity]["paused"])
        self.assertEqual(outcome.resumed, {self.sam_pixel})
        self.assertEqual(self.rows()[self.jo_pixel], (False, transport.INVALID_DEVICE))

    def link_jos_folder(self, link: str) -> Path:
        originals = self.root / "jo" / "originals"
        (originals / "2024").mkdir(parents=True, exist_ok=True)
        (originals / "2024" / "IMG_0001.HEIC").write_bytes(b"jo's original")
        if link == "_pixels":
            (self.root / "_pixels").symlink_to(self.root / "jo")
        else:
            (self.root / "_pixels").mkdir(exist_ok=True)
            (self.root / "_pixels" / str(self.jo_pixel)).symlink_to(originals)
        return self.root / "_pixels" / str(self.jo_pixel)

    def test_a_pixel_whose_folder_is_a_link_gets_no_folder_and_no_device_and_the_other_still_resumes(self):
        self.link_jos_folder(f"_pixels/{self.jo_pixel}")
        before = test_pixel_check.share_tree(self.root)
        for at in (NOW, NOW + 10):
            outcome = self.reconcile(at=at)
            self.assertIsNone(outcome.failure)
            self.assertEqual(outcome.resumed, {self.sam_pixel})
            self.assertEqual({folder.id for folder in self.client.folders()}, {self.folder_id(self.sam_pixel)})
            self.assertEqual({device.device_id for device in self.client.devices()}, {OWN, SAM_DEVICE})
            self.assertEqual(self.rows(), {self.jo_pixel: (False, transport.PATH_REFUSED), self.sam_pixel: (True, None)})
        self.assertFalse(any(method == "POST" and path == "/rest/config/folders" and body["id"] == self.folder_id(self.jo_pixel)
                             for method, path, body in self.writes()))
        self.assertEqual(self.events(transport.FOLDER_ADDED), [(f"pixel {self.sam_pixel}", mock.ANY)])
        self.assertEqual(self.events(transport.FOLDER_REMOVED), [])
        self.assertEqual(test_pixel_check.share_tree(self.root), before)

    def test_a_persisted_folder_of_a_pixel_whose_folder_became_a_link_stays_paused_and_is_never_resumed(self):
        for link in (f"_pixels/{self.jo_pixel}", "_pixels"):
            with self.subTest(link=link):
                linked = self.link_jos_folder(link)
                identity = self.folder_id(self.jo_pixel)
                self.fake.folders[identity] = {"id": identity, "path": str(linked), "type": "sendonly", "paused": False,
                                               "devices": [{"deviceID": JO_DEVICE}, {"deviceID": OWN}], "versioning": {"type": ""}}
                self.fake.devices[JO_DEVICE] = {"deviceID": JO_DEVICE, "name": "persisted", "paused": False}
                self.fake.ignores[identity] = ["*.xmp"]
                self.fake.requests.clear()
                refused = {self.jo_pixel} if link != "_pixels" else {self.jo_pixel, self.sam_pixel}
                for at in (NOW, NOW + 10, NOW + 20):
                    outcome = self.reconcile(at=at)
                    self.assertIsNone(outcome.failure)
                    self.assertEqual(outcome.resumed, {self.jo_pixel, self.sam_pixel} - refused)
                    self.assertNotIn(self.jo_pixel, outcome.approved)
                    self.assertIn(identity, self.fake.folders)
                    self.assertTrue(self.fake.folders[identity]["paused"])
                    self.assertTrue(self.fake.devices[JO_DEVICE]["paused"])
                    self.assertEqual({pixel_id: row for pixel_id, row in self.rows().items() if pixel_id in refused},
                                     dict.fromkeys(refused, (False, transport.PATH_REFUSED)))
                writes = self.writes()
                self.assertFalse(any(isinstance(body, dict) and body.get("paused") is False and (path.endswith(identity) or path.endswith(JO_DEVICE))
                                     for _, path, body in writes))
                self.assertNotIn(identity, [body.get("id") for method, path, body in writes if method == "POST" and path == "/rest/config/folders"])
                self.assertEqual(self.events(transport.FOLDER_REMOVED), [])
                self.assertEqual((self.root / "jo/originals/2024/IMG_0001.HEIC").read_bytes(), b"jo's original")
                if link == "_pixels":
                    (self.root / "_pixels").unlink()
                else:
                    (self.root / "_pixels" / str(self.jo_pixel)).unlink()
                    (self.root / "_pixels").rmdir()

    def test_two_pixels_with_one_device_id_get_no_folder_at_all(self):
        self.pair(self.sam_pixel, JO_DEVICE)
        outcome = self.reconcile()
        self.assertEqual(outcome.resumed, set())
        self.assertEqual(self.fake.folders, {})
        self.assertEqual(self.rows(), {self.jo_pixel: (False, transport.SHARED_DEVICE), self.sam_pixel: (False, transport.SHARED_DEVICE)})

    def test_a_pixel_that_is_deleted_or_unpaired_loses_its_folder_and_device_and_no_file_is_deleted(self):
        item = self.item(self.jo)
        pixel_folder.queue(self.opened, item, NOW)
        self.tend()
        self.reconcile()
        before = test_pixel_check.share_tree(self.root)
        self.pair(self.jo_pixel, None)
        self.reconcile(at=NOW + 10)
        self.assertEqual({folder.id for folder in self.client.folders()}, {self.folder_id(self.sam_pixel)})
        self.assertNotIn(JO_DEVICE, {device.device_id for device in self.client.devices()})
        self.opened.delete(self.opened.get(Pixel, self.sam_pixel))
        self.opened.commit()
        self.reconcile(at=NOW + 20)
        self.assertEqual(self.client.folders(), [])
        self.assertEqual({device.device_id for device in self.client.devices()}, {OWN})
        self.assertEqual(test_pixel_check.share_tree(self.root), before)
        self.assertTrue(self.copy_path(self.jo_pixel, item).is_file())

    def test_syncthings_reports_fill_the_transport_table_and_last_sync_only_when_fully_synced(self):
        self.reconcile()
        self.fake.connections = {JO_DEVICE: {"connected": True, "type": "tcp-client"}, SAM_DEVICE: {"connected": False}}
        self.fake.stats = {JO_DEVICE: {"lastSeen": "2026-11-10T12:00:00.123456789Z"}, SAM_DEVICE: {"lastSeen": "1970-01-01T00:00:00Z"}}
        self.fake.completions = {(self.folder_id(self.jo_pixel), JO_DEVICE): {"completion": 100, "needItems": 0, "needDeletes": 0},
                                 (self.folder_id(self.sam_pixel), SAM_DEVICE): {"completion": 100, "needItems": 0}}
        self.reconcile(at=NOW + 10)
        jo, sam = self.opened.get(PixelTransport, self.jo_pixel), self.opened.get(PixelTransport, self.sam_pixel)
        self.assertEqual((jo.completion, jo.last_seen, jo.updated), (100.0, 1794312000.123456, NOW + 10))
        self.assertEqual((sam.completion, sam.last_seen), (100.0, None))
        self.assertEqual((self.opened.get(Pixel, self.jo_pixel).last_sync, self.opened.get(Pixel, self.sam_pixel).last_sync), (NOW + 10, None))
        self.fake.completions[(self.folder_id(self.jo_pixel), JO_DEVICE)] = {"completion": 40.5, "needItems": 3}
        self.reconcile(at=NOW + 20)
        self.assertEqual((self.opened.get(PixelTransport, self.jo_pixel).completion, self.opened.get(Pixel, self.jo_pixel).last_sync), (40.5, NOW + 10))

    def test_reports_that_cannot_be_read_leave_the_reconcile_standing(self):
        self.reconcile()
        with mock.patch.object(syncthing.Client, "connections", side_effect=syncthing.SyncthingFailed("Syncthing answered 500", 500)):
            outcome = self.reconcile(at=NOW + 10)
        self.assertIsNone(outcome.failure)
        self.assertEqual(outcome.resumed, {self.jo_pixel, self.sam_pixel})

    def test_no_device_id_path_or_key_reaches_a_log_an_event_or_the_transport_table(self):
        canary_root = self.root / "canary-share-root-8b1e"
        canary_root.mkdir()
        self.photos = type(self.photos)(canary_root)
        self.fake.folders["photos"] = {"id": "photos", "path": str(canary_root / "x"), "type": "sendreceive", "paused": False, "devices": [{"deviceID": STRANGER}]}
        self.fake.devices[STRANGER] = {"deviceID": STRANGER, "name": "stranger", "paused": False}
        with no_leaks(self, KEY, "canary-share-root-8b1e", STRANGER, database=self.opened.get_bind()):
            self.reconcile()
            self.fake.forgetful = True
            self.reconcile(at=NOW + 10)
        self.fake.forgetful = False

    def test_the_transport_reconciles_only_when_what_it_wants_changed_unless_forced(self):
        moving = transport.Transport()

        def tick(at, force=False, share_problem=None):
            return moving.tick(self.opened, self.client, self.photos, lambda pixel_id: pixel_id in self.held, share_problem, at, force)

        self.assertIsNotNone(tick(NOW))
        self.fake.requests.clear()
        self.assertIsNone(tick(NOW + 60))
        self.assertEqual({received.method for received in self.fake.requests}, {"GET"})
        self.assertIsNotNone(tick(NOW + 120, force=True))
        for change in (lambda: self.pair(self.sam_pixel, None), lambda: self.held.add(self.jo_pixel)):
            change()
            self.assertIsNotNone(tick(NOW + 180))
            self.assertIsNone(tick(NOW + 240))
        self.fake.forgetful = True
        self.held.clear()
        self.assertIsNotNone(tick(NOW + 300).failure)
        self.fake.forgetful = False
        self.assertIsNone(tick(NOW + 360).failure)
        self.assertIsNone(tick(NOW + 420))

    def test_the_transport_reconciles_again_when_syncthing_comes_back_paused_or_changed_behind_its_back(self):
        moving = transport.Transport()

        def tick(at):
            return moving.tick(self.opened, self.client, self.photos, lambda pixel_id: False, None, at)

        self.assertEqual(tick(NOW).resumed, {self.jo_pixel, self.sam_pixel})
        self.assertIsNone(tick(NOW + 60))
        for entry in [*self.fake.folders.values(), *(device for identity, device in self.fake.devices.items() if identity != OWN)]:
            entry["paused"] = True
        self.assertEqual(tick(NOW + 120).resumed, {self.jo_pixel, self.sam_pixel})
        self.assertTrue(all(not entry["paused"] for entry in self.fake.folders.values()))
        self.assertIsNone(tick(NOW + 180))
        self.fake.folders[self.folder_id(self.jo_pixel)]["type"] = "sendreceive"
        self.assertEqual(tick(NOW + 240).resumed, {self.sam_pixel})
        self.assertNotIn(self.folder_id(self.jo_pixel), self.fake.folders)
        self.assertEqual(tick(NOW + 300).resumed, {self.sam_pixel})
        self.assertNotIn(JO_DEVICE, self.fake.devices)
        self.assertIsNone(tick(NOW + 360))
        self.fake.devices[STRANGER] = {"deviceID": STRANGER, "name": "stranger", "paused": False}
        self.assertIsNotNone(tick(NOW + 420))
        self.assertNotIn(STRANGER, self.fake.devices)
        with mock.patch.object(syncthing.Client, "folders", side_effect=syncthing.SyncthingFailed("Syncthing answered 500", 500)):
            self.assertIsNotNone(tick(NOW + 480).failure)
        self.assertIsNone(moving.seen)
        self.assertIsNotNone(tick(NOW + 540))
        self.assertIsNone(tick(NOW + 600))


class FolderIdTests(unittest.TestCase):
    def test_only_darkrooms_own_folder_ids_name_a_pixel(self):
        self.assertEqual(transport.pixel_of(transport.folder_of(12)), 12)
        for identity in ("photos", "darkroom-pixel-", "darkroom-pixel-012", "darkroom-pixel-1a", "darkroom-pixel-١", "pixel-1"):
            with self.subTest(identity=identity):
                self.assertIsNone(transport.pixel_of(identity))

    def test_syncthings_times_are_read_only_with_their_zone(self):
        self.assertEqual(transport.instant("2026-11-10T13:00:00+01:00"), 1794312000.0)
        for value in ("", "yesterday", "2026-11-10T12:00:00", "1970-01-01T00:00:00Z", "2300-01-01T00:00:00Z"):
            with self.subTest(value=value):
                self.assertIsNone(transport.instant(value))


class HeldPixelTests(unittest.TestCase):
    setUp_pixels = test_pixel_folder.PixelFolderTests.setUp
    account = test_pixel_folder.PixelFolderTests.account
    item = test_pixel_folder.PixelFolderTests.item
    copies = test_pixel_folder.PixelFolderTests.copies
    copy_path = test_pixel_folder.PixelFolderTests.copy_path
    tend = test_pixel_folder.PixelFolderTests.tend

    def setUp(self):
        self.setUp_pixels()
        self.fake = FakeSyncthing(self, KEY, OWN)
        self.client = syncthing.Client(self.fake.url, KEY, timeout=5.0)
        self.opened.get(Pixel, self.jo_pixel).syncthing_device_id = JO_DEVICE
        self.opened.commit()
        self.check = pixel_check.PlacedCopyCheck()

    def reconcile(self, at: float) -> transport.Outcome:
        return transport.reconcile(self.opened, self.client, self.photos, lambda pixel_id: self.check.held(self.opened, pixel_id), None, at)

    def shared(self) -> bool:
        found = self.fake.folders.get(transport.folder_of(self.jo_pixel))
        return found is not None and not found["paused"]

    def test_the_folder_is_not_shared_before_the_check_has_run_nor_between_requeueing_and_verified_replacement(self):
        item = self.item(self.jo)
        pixel_folder.queue(self.opened, item, NOW)
        self.tend(at=NOW + 1)
        self.reconcile(NOW + 2)
        self.assertFalse(self.shared())
        self.check.run(self.opened, self.photos, NOW + 3)
        self.reconcile(NOW + 4)
        self.assertTrue(self.shared())
        self.copy_path(self.jo_pixel, item).unlink()
        self.assertEqual(self.check.run(self.opened, self.photos, NOW + 5).requeued, 1)
        self.reconcile(NOW + 6)
        self.assertFalse(self.shared())
        self.assertEqual(self.tend(at=NOW + 7).placed, 1)
        self.assertTrue(self.copy_path(self.jo_pixel, item).is_file())
        self.reconcile(NOW + 8)
        self.assertTrue(self.shared())

    def test_the_folder_stays_unshared_while_a_requeued_copy_cannot_be_placed(self):
        item = self.item(self.jo)
        pixel_folder.queue(self.opened, item, NOW)
        self.tend(at=NOW + 1)
        self.check.run(self.opened, self.photos, NOW + 2)
        self.reconcile(NOW + 3)
        self.assertTrue(self.shared())
        self.copy_path(self.jo_pixel, item).unlink()
        original = self.root / item.path
        shutil.move(original, self.root / "elsewhere")
        self.check.run(self.opened, self.photos, NOW + 4)
        for at in (NOW + 5, NOW + 3600, NOW + 7200):
            with self.subTest(at=at):
                self.assertEqual(self.tend(at=at).failed, 1)
                self.reconcile(at + 1)
                self.assertFalse(self.shared())
        self.assertEqual([copy.state for copy in self.copies(self.jo_pixel)], ["queued"])


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        import dataclasses
        import tempfile

        from darkroom.storage import db
        from darkroom.storage.models import GoogleAccount, MediaItem, PixelPerson
        from tests.shared import add_person, bound_share, own_database, private_socket_folder, settings_from

        self.engine = own_database(self)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.socket = private_socket_folder(self) / "syncthing.sock"
        self.fake = FakeSyncthing(self, KEY, OWN, str(self.socket))
        self.settings = dataclasses.replace(settings_from(DARKROOM_PHOTOS_ROOT=str(self.root), DARKROOM_DATA_DIR=str(self.root)), syncthing=True, syncthing_key=KEY)
        (self.root / "jo/originals").mkdir(parents=True)
        (self.root / "jo/originals/a.jpg").write_bytes(b"photo")
        with db.session(self.engine) as opened:
            person = add_person(opened, "jo")
            account = GoogleAccount(email="jo@x.test", default_person_id=person, created=1.0)
            opened.add(account)
            opened.flush()
            pixel = Pixel(name="Pixel", google_account_id=account.id, syncthing_device_id=JO_DEVICE, created=1.0)
            opened.add(pixel)
            opened.flush()
            opened.add(PixelPerson(pixel_id=pixel.id, person_id=person))
            item = MediaItem(person_id=person, source="phone", path="jo/originals/a.jpg", sha1=test_pixel_folder.sha1(b"photo"), bytes=5, indexed_at=1.0)
            opened.add(item)
            opened.flush()
            pixel_folder.queue(opened, item, 2.0)
            self.pixel = pixel.id
        bound_share(self.engine, self.root)

    def serving(self, settings=None):
        from darkroom import config
        from darkroom.server import jobs

        patched = mock.patch.object(config, "SYNCTHING_SOCKET", self.socket)
        patched.start()
        self.addCleanup(patched.stop)
        return jobs.for_serving(settings or self.settings, self.engine, 100.0)

    def folder(self) -> dict[str, object] | None:
        return self.fake.folders.get(transport.folder_of(self.pixel))

    def test_startup_reconciles_without_resuming_and_each_tend_reconciles_after_the_check_and_before_placing(self):
        from darkroom.server import jobs

        scheduler = self.serving()
        self.assertIsNone(self.folder())
        self.assertEqual({identity: entry["paused"] for identity, entry in self.fake.devices.items()}, {OWN: False, JO_DEVICE: True})
        self.assertEqual(scheduler.jobs["syncthing.reconcile"].interval, jobs.SYNCTHING_RECONCILE_SECONDS)
        order = []
        real_check, real_tick, real_tend = pixel_check.check, transport.Transport.tick, jobs.pixel_folder.tend
        with mock.patch.object(pixel_check, "check", side_effect=lambda *a: order.append("check") or real_check(*a)), \
                mock.patch.object(transport.Transport, "tick", autospec=True, side_effect=lambda *a, **k: order.append("reconcile") or real_tick(*a, **k)), \
                mock.patch.object(jobs.pixel_folder, "tend", side_effect=lambda *a: order.append("tend") or real_tend(*a)):
            scheduler.run_one("pixels.tend", 105.0)
        self.assertEqual(order, ["check", "reconcile", "tend"])
        self.assertFalse(self.folder()["paused"])
        self.assertTrue((self.root / f"_pixels/{self.pixel}/jo/a.jpg").is_file())
        self.fake.requests.clear()
        scheduler.run_one("syncthing.reconcile", 160.0)
        self.assertEqual({received.method for received in self.fake.requests}, {"GET"})
        self.assertTrue(scheduler.jobs["syncthing.reconcile"].own_thread)

    def test_a_startup_reconcile_that_raises_does_not_stop_the_service_from_starting(self):
        with mock.patch.object(transport.Transport, "tick", side_effect=PermissionError("made-up lstat refusal")), \
                mock.patch.object(transport.monitoring, "log") as logged:
            scheduler = self.serving()
        self.assertIn("syncthing.reconcile", scheduler.jobs)
        self.assertIsNone(self.folder())
        self.assertIn(mock.call("job failed", job="syncthing.reconcile", error="PermissionError"), logged.call_args_list)

    def test_a_failed_share_check_pauses_the_transport_and_nothing_resumes_until_a_tend_has_checked_again(self):
        from darkroom.storage import share

        scheduler = self.serving()
        scheduler.run_one("pixels.tend", 105.0)
        self.assertFalse(self.folder()["paused"])
        sentinel = share.read_sentinel(self.root)
        (self.root / share.SHARE_SENTINEL_FILE).unlink()
        scheduler.run_one("pixels.tend", 3700.0)
        self.assertTrue(self.folder()["paused"])
        self.assertTrue(self.fake.devices[JO_DEVICE]["paused"])
        scheduler.run_one("syncthing.reconcile", 3760.0)
        self.assertTrue(self.folder()["paused"])
        share.write_sentinel(self.root, sentinel)
        scheduler.run_one("syncthing.reconcile", 3820.0)
        self.assertTrue(self.folder()["paused"])
        scheduler.run_one("pixels.tend", 7300.0)
        self.assertFalse(self.folder()["paused"])

    def test_a_share_failure_seen_by_the_poll_holds_the_transport_until_a_tend_has_checked_the_recovered_share(self):
        from darkroom.storage import db, share

        scheduler = self.serving()
        scheduler.run_one("pixels.tend", 105.0)
        self.assertFalse(self.folder()["paused"])
        copy = self.root / f"_pixels/{self.pixel}/jo/a.jpg"
        sentinel = share.read_sentinel(self.root)
        (self.root / share.SHARE_SENTINEL_FILE).unlink()
        copy.unlink()
        scheduler.run_one("syncthing.reconcile", 160.0)
        self.assertTrue(self.folder()["paused"])
        self.assertFalse(scheduler.placed_copies.ran)
        share.write_sentinel(self.root, sentinel)
        for at in (220.0, 280.0):
            scheduler.run_one("syncthing.reconcile", at)
            self.assertTrue(self.folder()["paused"])
            with db.session(self.engine) as opened:
                self.assertTrue(scheduler.placed_copies.held(opened, self.pixel))
        self.assertFalse(copy.exists())
        order = []
        real_check, real_tick = pixel_check.check, transport.Transport.tick
        with mock.patch.object(pixel_check, "check", side_effect=lambda *a: order.append("check") or real_check(*a)), \
                mock.patch.object(transport.Transport, "tick", autospec=True, side_effect=lambda *a, **k: order.append("reconcile") or real_tick(*a, **k)):
            scheduler.run_one("pixels.tend", 3700.0)
        self.assertEqual(order, ["check", "reconcile"])
        self.assertTrue(copy.is_file())
        scheduler.run_one("syncthing.reconcile", 3760.0)
        self.assertFalse(self.folder()["paused"])

    def test_a_share_bound_again_counts_as_unchecked_for_the_transport_until_a_tend_has_seen_it(self):
        from darkroom.domain import share_check
        from darkroom.storage import db, share
        from tests.shared import bound_share

        scheduler = self.serving()

        scheduler.run_one("pixels.tend", 105.0)
        (self.root / share.SHARE_SENTINEL_FILE).unlink()
        bound_share(self.engine, self.root, token="b" * 32)
        with db.session(self.engine) as opened:
            self.assertEqual(scheduler.share.transport_problem(opened), share_check.REBOUND)
        scheduler.run_one("syncthing.reconcile", 160.0)
        self.assertTrue(self.folder()["paused"])
        with db.session(self.engine) as opened:
            self.assertEqual(scheduler.share.transport_problem(opened), share_check.REBOUND)
        scheduler.run_one("pixels.tend", 3700.0)
        scheduler.run_one("pixels.tend", 7300.0)
        self.assertFalse(self.folder()["paused"])

    def test_with_syncthing_off_or_its_gate_off_the_reconciler_never_calls_syncthing(self):
        import dataclasses

        from darkroom.server import jobs

        scheduler = self.serving(dataclasses.replace(self.settings, syncthing=False))
        self.assertNotIn("syncthing.reconcile", scheduler.jobs)
        scheduler.run_one("pixels.tend", 105.0)
        self.fake.version = {"version": "v2.2.0", "isRelease": True}
        gated = self.serving()
        self.assertFalse(gated.syncthing.on)
        gated.run_one("pixels.tend", 105.0)
        gated.run_one("syncthing.reconcile", 160.0)
        self.assertEqual({(received.method, received.path) for received in self.fake.requests}, {("GET", "/rest/system/version")})
        self.assertEqual(self.fake.folders, {})
        self.assertIsNone(jobs.transport_client(gated, self.settings))


if __name__ == "__main__":
    unittest.main()
