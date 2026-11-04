import hashlib
import io
import resource as rusage
import tempfile
import time
import tracemalloc
import unittest
from pathlib import Path
from unittest import mock

from darkroom.domain import devices, uploads
from darkroom.storage import db, paths
from darkroom.storage.models import MediaItem, PhoneAsset, PhoneResource, User
from tests.device_world import ASSET, OTHER_TOKEN, TOKEN, DeviceCase, resource, sha1
from tests.fixtures.media import make_jpeg
from tests.privacy import no_leaks

MB = 1024 * 1024
BIG = 50 * MB


def big_chunks():
    for number in range(BIG // MB):
        yield hashlib.sha256(str(number).encode()).digest() * (MB // 32)


def big_sha1():
    digest = hashlib.sha1(usedforsecurity=False)
    for chunk in big_chunks():
        digest.update(chunk)
    return digest.hexdigest()


def jpeg_bytes(colour=(200, 40, 40)):
    with tempfile.TemporaryDirectory() as folder:
        return make_jpeg(Path(folder) / "made.jpg", taken="2024:05:01 10:00:00", offset="+02:00", colour=colour).read_bytes()


def media_item(opened, person_id, digest, path="jo/originals/IMG_0001.JPG", confirmed=False):
    item = MediaItem(person_id=person_id, source="phone", path=path, sha1=digest, bytes=1, indexed_at=1.0, confirmed_in_google=confirmed)
    opened.add(item)
    opened.flush()
    return item.id


class UploadTests(DeviceCase):
    def resource_row(self, kind="photo", asset=ASSET):
        with db.session(self.engine) as opened:
            found = opened.query(PhoneAsset).filter_by(device_id=self.device_id, photokit_id=asset).one()
            return opened.query(PhoneResource).filter_by(asset_id=found.id, kind=kind).one()

    def test_a_50_mb_upload_streams_with_a_bounded_memory_footprint(self):
        digest = big_sha1()
        status, reply = self.check([{"id": ASSET, "resources": [{"kind": "live_video", "sha1": digest, "bytes": BIG, "filename": "IMG_0001.MOV"}]}])
        self.assertEqual((status, reply), (200, {"needed": [{"id": ASSET, "kind": "live_video"}]}))
        before = rusage.getrusage(rusage.RUSAGE_SELF).ru_maxrss
        tracemalloc.start()
        try:
            status, reply = self.put(big_chunks(), "live_video", "IMG_0001.MOV", digest=digest, length=BIG)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        grown_mb = (rusage.getrusage(rusage.RUSAGE_SELF).ru_maxrss - before) / 1024
        self.assertEqual(status, 200, reply)
        self.assertEqual(reply, {"id": ASSET, "kind": "live_video", "sha1": digest, "already": False, "indexed": False})
        self.assertLess(peak, 8 * MB, "the upload was buffered in memory")
        self.assertLess(grown_mb, 32, "the upload was buffered in memory")
        saved = self.originals / "IMG_0001.MOV"
        self.assertEqual(saved.stat().st_size, BIG)
        self.assertEqual(uploads.media.sha1_of(saved)[0], digest)
        self.assertEqual(self.files_under_originals(), [saved])
        self.assertIsNotNone(self.resource_row("live_video").uploaded_at)

    def test_a_hash_mismatch_is_422_and_leaves_nothing_under_originals(self):
        data = b"the bytes the phone meant to send" * 100
        self.check([{"id": ASSET, "resources": [resource("photo", data, "IMG_0001.JPG")]}])
        status, reply = self.put(data[:-1] + b"!", digest=sha1(data))
        self.assertEqual(status, 422)
        self.assertIn("does not match", reply["error"])
        self.assertEqual(self.files_under_originals(), [])
        self.assertIsNone(self.resource_row().uploaded_at)

    def test_a_new_resource_is_needed_uploaded_once_indexed_and_idempotent_on_re_put(self):
        data = jpeg_bytes()
        status, reply = self.check([{"id": ASSET, "resources": [resource("photo", data, "IMG_0001.JPG")]}])
        self.assertEqual(reply, {"needed": [{"id": ASSET, "kind": "photo"}]})
        status, reply = self.put(data)
        self.assertEqual((status, reply), (200, {"id": ASSET, "kind": "photo", "sha1": sha1(data), "already": False, "indexed": True}))
        saved = self.originals / "IMG_0001.JPG"
        self.assertEqual(saved.read_bytes(), data)
        row = self.resource_row()
        with db.session(self.engine) as opened:
            item = opened.get(MediaItem, row.media_item_id)
            self.assertEqual((item.source, item.sha1, item.path, item.person_id), ("phone", sha1(data), "jo/originals/IMG_0001.JPG", self.person_id))
            self.assertFalse(item.confirmed_in_google)
        mtime = saved.stat().st_mtime_ns
        status, reply = self.put(data)
        self.assertEqual((status, reply["already"], reply["indexed"]), (200, True, True))
        self.assertEqual(self.files_under_originals(), [saved])
        self.assertEqual(saved.stat().st_mtime_ns, mtime)
        self.assertEqual(self.check([{"id": ASSET, "resources": [resource("photo", data, "IMG_0001.JPG")]}]), (200, {"needed": []}))

    def test_a_resource_already_present_by_hash_is_not_needed_and_is_linked(self):
        data = b"delivered by Mobius Sync earlier" * 10
        with db.session(self.engine) as opened:
            item_id = media_item(opened, self.person_id, sha1(data), confirmed=True)
        status, reply = self.check([{"id": ASSET, "resources": [resource("photo", data, "IMG_0001.JPG"), resource("adjusted", b"edit", "IMG_0001_adjusted.JPG")]}])
        self.assertEqual((status, reply), (200, {"needed": [{"id": ASSET, "kind": "adjusted"}]}))
        row = self.resource_row()
        self.assertEqual(row.media_item_id, item_id)
        self.assertIsNotNone(row.uploaded_at)
        self.assertEqual(self.files_under_originals(), [])
        with db.session(self.engine) as opened:
            self.assertTrue(opened.get(MediaItem, item_id).confirmed_in_google)

    def test_another_persons_copy_does_not_count_but_an_older_iphone_of_the_same_person_does(self):
        data = b"a photo both people have" * 10
        self.enrol("sam", "sam@example.test", OTHER_TOKEN)
        self.assertEqual(self.check([{"id": "sam-1", "resources": [resource("photo", data, "IMG_9.JPG")]}], OTHER_TOKEN)[1]["needed"], [{"id": "sam-1", "kind": "photo"}])
        self.assertEqual(self.put(data, filename="IMG_9.JPG", asset="sam-1", token=OTHER_TOKEN)[0], 200)
        self.assertEqual(self.check([{"id": ASSET, "resources": [resource("photo", data, "IMG_9.JPG")]}])[1]["needed"], [{"id": ASSET, "kind": "photo"}])
        self.enrol("jo", "jo@example.test", "device-token-canary-newer-a1b2c3d4e5f6a7b8")
        self.assertEqual(self.put(data, filename="IMG_9.JPG")[0], 200)
        with db.session(self.engine) as opened:
            devices.revoke(opened, self.device_id, 5.0)
        status, reply = self.check([{"id": "new-id", "resources": [resource("photo", data, "IMG_9.JPG")]}], "device-token-canary-newer-a1b2c3d4e5f6a7b8")
        self.assertEqual((status, reply), (200, {"needed": []}))

    def test_different_bytes_at_the_same_name_get_a_suffixed_name_and_identical_bytes_are_already_there(self):
        (self.originals / "IMG_0001.JPG").write_bytes(b"someone else's photo")
        data = b"this phone's photo" * 10
        self.check([{"id": ASSET, "resources": [resource("photo", data, "IMG_0001.JPG")]}])
        status, reply = self.put(data)
        self.assertEqual((status, reply["already"]), (200, False))
        self.assertEqual((self.originals / f"IMG_0001~{sha1(data)[:8]}.JPG").read_bytes(), data)
        self.assertEqual((self.originals / "IMG_0001.JPG").read_bytes(), b"someone else's photo")
        twin = b"already delivered" * 10
        (self.originals / "IMG_0002.JPG").write_bytes(twin)
        self.check([{"id": "second", "resources": [resource("photo", twin, "IMG_0002.JPG")]}])
        status, reply = self.put(twin, filename="IMG_0002.JPG", asset="second")
        self.assertEqual((status, reply["already"]), (200, True))
        self.assertEqual(len(self.files_under_originals()), 3)

    def test_a_name_held_twice_by_other_bytes_is_409_and_nothing_is_added(self):
        data = b"third photo" * 10
        (self.originals / "IMG_0003").write_bytes(b"one")
        (self.originals / f"IMG_0003~{sha1(data)[:8]}").write_bytes(b"two")
        self.check([{"id": ASSET, "resources": [resource("photo", data, "IMG_0003")]}])
        status, _ = self.put(data, filename="IMG_0003")
        self.assertEqual(status, 409)
        self.assertEqual(len(self.files_under_originals()), 2)
        self.assertEqual(list(self.photos.uploads_tmp("jo").iterdir()), [])

    def test_a_resource_changed_by_a_check_during_the_upload_stays_needed(self):
        data, newer = b"first export" * 10, b"second export" * 10
        self.check([{"id": ASSET, "resources": [resource("photo", data, "IMG_0001.JPG")]}])
        saving = uploads.save

        def recheck_meanwhile(*args):
            saved = saving(*args)
            self.check([{"id": ASSET, "resources": [resource("photo", newer, "IMG_0001.JPG")]}])
            return saved

        with mock.patch.object(uploads, "save", side_effect=recheck_meanwhile):
            status, reply = self.put(data)
        self.assertEqual(status, 409, reply)
        row = self.resource_row()
        self.assertEqual((row.sha1, row.uploaded_at, row.media_item_id), (sha1(newer), None, None))
        self.assertEqual(self.check([{"id": ASSET, "resources": [resource("photo", newer, "IMG_0001.JPG")]}])[1], {"needed": [{"id": ASSET, "kind": "photo"}]})

    def test_an_upload_must_be_checked_first_and_match_what_was_checked(self):
        data = b"checked bytes" * 10
        self.assertEqual(self.put(data)[0], 404)
        self.check([{"id": ASSET, "resources": [resource("photo", data, "IMG_0001.JPG")]}])
        self.assertEqual(self.put(b"other" * 10)[0], 409)
        self.assertEqual(self.put(data, filename="IMG_0002.JPG")[0], 409)
        self.assertEqual(self.files_under_originals(), [])

    def test_bad_headers_and_path_parts_are_400(self):
        data = b"some bytes" * 10
        self.check([{"id": ASSET, "resources": [resource("photo", data, "IMG_0001.JPG")]}])
        for overrides in ({"kind": "thumbnail"}, {"digest": "not-a-sha"}, {"filename": "../escape.JPG"}, {"filename": ".hidden.JPG"},
                          {"filename": "IMG_0001.JPG.xmp"}, {"filename": "a\\b.JPG"}, {"filename": "x" * 201}):
            with self.subTest(overrides=overrides):
                status, _ = self.put(data, **overrides)
                self.assertEqual(status, 400)
        self.assertEqual(self.files_under_originals(), [])

    def test_an_upload_that_ends_early_or_goes_idle_or_has_no_length_writes_nothing(self):
        self.check([{"id": ASSET, "resources": [resource("photo", b"x" * 1000, "IMG_0001.JPG")]}])
        self.assertEqual(self.raw_put(1000, b"x" * 10), 400)
        self.assertEqual(self.raw_put(1000, b"5\r\nxxxxx\r\n0\r\n\r\n", chunked=True), 411)
        self.assertEqual(self.files_under_originals(), [])
        self.assertIsNone(self.resource_row().uploaded_at)

    def test_a_device_token_and_file_names_never_reach_logs_or_replies(self):
        data = b"private photo bytes" * 10
        name = "IMG_canary_secret_name_0042.JPG"
        with no_leaks(self, TOKEN, name, replies=self.replies):
            self.check([{"id": ASSET, "resources": [resource("photo", data, name)]}])
            self.put(data, filename=name)
            self.put(data, filename=name)
            self.put(data + b"!", filename=name, digest=sha1(data), asset="unchecked")
        with no_leaks(self, TOKEN, database=self.engine):
            pass


class IdleUploadTests(DeviceCase):
    extra_env = {"UPLOAD_READ_TIMEOUT": "1"}

    def test_an_idle_upload_times_out_and_its_temp_is_removed(self):
        self.check([{"id": ASSET, "resources": [resource("photo", b"x" * 1000, "IMG_0001.JPG")]}])
        status = self.raw_put(1000, b"x" * 10, wait=2.5)
        self.assertEqual(status, 408)
        self.assertEqual(self.files_under_originals(), [])


class RefusedDeviceTests(DeviceCase):
    def test_a_revoked_device_is_401_and_nothing_is_written(self):
        data = b"bytes from a revoked phone" * 10
        self.check([{"id": ASSET, "resources": [resource("photo", data, "IMG_0001.JPG")]}])
        with db.session(self.engine) as opened:
            devices.revoke(opened, self.device_id, 5.0)
        self.assertEqual(self.put(data)[0], 401)
        self.assertEqual(self.check([{"id": "later", "resources": [resource("photo", data, "IMG_0002.JPG")]}])[0], 401)
        self.assertEqual(self.files_under_originals(), [])
        with db.session(self.engine) as opened:
            self.assertEqual(opened.query(PhoneAsset).filter_by(photokit_id="later").count(), 0)
            self.assertIsNone(opened.query(PhoneResource).one().uploaded_at)

    def test_a_lapsed_person_is_401_and_nothing_is_written(self):
        data = b"bytes from a lapsed person" * 10
        self.check([{"id": ASSET, "resources": [resource("photo", data, "IMG_0001.JPG")]}])
        with db.session(self.engine) as opened:
            opened.query(User).update({"last_seen": time.time() - 31 * 86_400})
        self.assertEqual(self.put(data)[0], 401)
        self.assertEqual(self.files_under_originals(), [])

    def test_no_token_or_a_wrong_token_is_401(self):
        self.assertEqual(self.put(b"x", token=None)[0], 401)
        self.assertEqual(self.put(b"x", token="device-token-canary-unknown-000000000")[0], 401)


class CheckValidationTests(DeviceCase):
    def test_malformed_check_bodies_are_400(self):
        good = resource("photo", b"x", "IMG_0001.JPG")
        for assets in ([], "nope", [{"id": ASSET, "resources": []}], [{"id": "", "resources": [good]}],
                       [{"id": ASSET, "resources": [good, good]}], [{"id": ASSET, "resources": [good]}, {"id": ASSET, "resources": [good]}],
                       [{"id": ASSET, "resources": [{**good, "sha1": "zz"}]}], [{"id": ASSET, "resources": [{**good, "bytes": -1}]}],
                       [{"id": ASSET, "resources": [{**good, "bytes": "1"}]}], [{"id": ASSET, "resources": [{**good, "kind": "raw"}]}],
                       [{"id": ASSET, "resources": [{**good, "filename": "a/b.JPG"}]}], [{"id": "x" * 201, "resources": [good]}]):
            with self.subTest(assets=assets):
                status, reply = self.check(assets)
                self.assertEqual(status, 400, reply)
        self.assertEqual(self.call("POST", "/api/device/assets/check", None)[0], 400)

    def test_a_changed_resource_is_needed_again_clears_eligibility_and_a_dropped_kind_goes(self):
        data, edit = b"photo" * 10, b"edit" * 10
        with db.session(self.engine) as opened:
            item_id = media_item(opened, self.person_id, sha1(data))
            media_item(opened, self.person_id, sha1(edit), path="jo/originals/IMG_0001_adjusted.JPG")
        self.assertEqual(self.check([{"id": ASSET, "resources": [resource("photo", data, "IMG_0001.JPG"), resource("adjusted", edit, "IMG_0001_adjusted.JPG")]}])[1],
                         {"needed": []})
        with db.session(self.engine) as opened:
            opened.query(PhoneAsset).update({"eligible_at": 10.0, "eligible_confirmed_at": 11.0})
        self.assertEqual(self.check([{"id": ASSET, "resources": [resource("photo", data, "IMG_0001.JPG"), resource("adjusted", b"new edit", "IMG_0001_adjusted.JPG")]}])[1],
                         {"needed": [{"id": ASSET, "kind": "adjusted"}]})
        with db.session(self.engine) as opened:
            asset = opened.query(PhoneAsset).one()
            self.assertEqual((asset.eligible_at, asset.eligible_confirmed_at), (None, None))
            opened.query(PhoneAsset).update({"eligible_at": 12.0})
        self.assertEqual(self.check([{"id": ASSET, "resources": [resource("photo", data, "IMG_0001.JPG")]}])[1], {"needed": []})
        with db.session(self.engine) as opened:
            self.assertEqual([row.kind for row in opened.query(PhoneResource)], ["photo"])
            self.assertIsNone(opened.query(PhoneAsset).one().eligible_at)
            self.assertEqual(opened.query(PhoneResource).one().media_item_id, item_id)
        self.assertEqual(self.check([{"id": ASSET, "resources": [resource("photo", data, "IMG_0001.JPG")]}])[1], {"needed": []})
        with db.session(self.engine) as opened:
            opened.query(PhoneAsset).update({"eligible_at": 13.0})
        self.check([{"id": ASSET, "resources": [resource("photo", data, "IMG_0001.JPG")]}])
        with db.session(self.engine) as opened:
            self.assertEqual(opened.query(PhoneAsset).one().eligible_at, 13.0)


class UploadPathsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.photos = paths.Photos(self.root)
        self.photos.ensure_person_tree("jo")

    def test_uploads_are_received_only_into_an_upload_folder(self):
        with self.assertRaises(paths.NotAnUpload):
            paths.receive_upload(self.photos.originals("jo"), io.BytesIO(b"abc"), 3)
        temp, digest = paths.receive_upload(self.photos.uploads_tmp("jo"), io.BytesIO(b"abc"), 3)
        self.assertEqual((temp.read_bytes(), digest), (b"abc", sha1(b"abc")))
        with self.assertRaises(paths.NotAnUpload):
            paths.discard_upload(self.photos.originals("jo") / "IMG.JPG")
        with self.assertRaises(paths.NotAnUpload):
            paths.adopt_upload(self.photos.originals("jo") / "IMG.JPG", self.photos.originals("jo") / "x.JPG")
        paths.discard_upload(temp)
        self.assertFalse(temp.exists())

    def test_an_adopted_upload_never_replaces_a_file_and_keeps_its_temp_when_refused(self):
        held = self.photos.originals("jo") / "IMG.JPG"
        held.write_bytes(b"kept")
        temp, _ = paths.receive_upload(self.photos.uploads_tmp("jo"), io.BytesIO(b"new"), 3)
        with self.assertRaises(paths.AlreadyExists):
            paths.adopt_upload(temp, held)
        self.assertEqual((held.read_bytes(), temp.exists()), (b"kept", True))
        with mock.patch("os.link", side_effect=OSError("cross-device")):
            paths.adopt_upload(temp, self.photos.originals("jo") / "copied.JPG")
        self.assertEqual(((self.photos.originals("jo") / "copied.JPG").read_bytes(), temp.exists()), (b"new", False))

    def test_a_short_stream_removes_its_temp(self):
        with self.assertRaises(paths.IncompleteUpload):
            paths.receive_upload(self.photos.uploads_tmp("jo"), io.BytesIO(b"ab"), 3)
        self.assertEqual(list(self.photos.uploads_tmp("jo").iterdir()), [])

    def test_filenames_and_candidates(self):
        self.assertEqual(list(uploads.candidates("IMG_1.HEIC", "a" * 40)), ["IMG_1.HEIC", "IMG_1~aaaaaaaa.HEIC"])
        self.assertEqual(list(uploads.candidates("README", "b" * 40)), ["README", "README~bbbbbbbb"])
        self.assertEqual(uploads.sha1_of(" " + "A" * 40), "a" * 40)


if __name__ == "__main__":
    unittest.main()
