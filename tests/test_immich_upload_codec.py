import hashlib
import io
import unittest
from email import policy
from email.parser import BytesParser
from unittest import mock
from uuid import UUID

from darkroom import tls
from darkroom.providers import immich
from tests.privacy import no_leaks

CREATED = "2024-01-02T03:04:05.678+05:45"
MODIFIED = "2024-01-03T04:05:06Z"
ASSET_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
VIDEO_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
DATA = b"generated-original\x00\xff\r\nbinary"
SIDECAR = '<x:xmpmeta xmlns:x="adobe:ns:meta/">generated 雪</x:xmpmeta>'.encode()


def source(data=DATA, **changes):
    return immich.UploadSource(**({"reader": io.BytesIO(data), "size": len(data), "sha1": hashlib.sha1(data, usedforsecurity=False).hexdigest()} | changes))


def body(original=None, **changes):
    return immich.MultipartUpload(**({"original": source() if original is None else original, "filename": 'generated "Á" 雪.JPG',
                                      "file_created_at": CREATED, "file_modified_at": MODIFIED} | changes))


class TrackedReader(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


class UploadBodyTests(unittest.TestCase):
    def test_multipart_fields_files_offsets_and_length_survive_an_independent_parser(self):
        original, sidecar = source(), source(SIDECAR)
        upload = body(original, sidecar=sidecar, live_video_id=VIDEO_ID)
        encoded = b"".join(upload)
        self.assertTrue(upload.complete)
        self.assertEqual(len(encoded), int(upload.headers["Content-Length"]))
        message = BytesParser(policy=policy.default).parsebytes(("Content-Type: " + upload.headers["Content-Type"] + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + encoded)
        self.assertFalse(message.defects)
        ordered = list(message.iter_parts())
        names = [part.get_param("name", header="content-disposition") for part in ordered]
        self.assertEqual(names, ["fileCreatedAt", "fileModifiedAt", "livePhotoVideoId", "assetData", "sidecarData", "filename"])
        parts = dict(zip(names, ordered, strict=True))
        self.assertEqual(set(parts), {"fileCreatedAt", "fileModifiedAt", "filename", "livePhotoVideoId", "assetData", "sidecarData"})
        self.assertEqual(parts["assetData"].get_payload(decode=True), DATA)
        self.assertEqual(parts["sidecarData"].get_payload(decode=True), SIDECAR)
        self.assertEqual(parts["assetData"].get_filename(), "original.JPG")
        self.assertEqual(parts["sidecarData"].get_filename(), "sidecar.xmp")
        for field, value in (("fileCreatedAt", CREATED), ("fileModifiedAt", MODIFIED), ("filename", 'generated "Á" 雪.JPG'), ("livePhotoVideoId", VIDEO_ID)):
            self.assertEqual(parts[field].get_payload(decode=True).decode("utf-8"), value)
        self.assertFalse(original.reader.closed)
        self.assertFalse(sidecar.reader.closed)

    def test_optional_parts_are_absent_and_boundary_is_generated_per_body(self):
        first, second = body(), body()
        self.assertNotEqual(first.headers["Content-Type"], second.headers["Content-Type"])
        encoded = b"".join(first)
        self.assertNotIn(b'name="sidecarData"', encoded)
        self.assertNotIn(b'name="livePhotoVideoId"', encoded)
        boundary = first.headers["Content-Type"].split("boundary=", 1)[1].encode()
        self.assertTrue(encoded.startswith(b"--" + boundary + b"\r\n"))
        self.assertTrue(encoded.endswith(b"--" + boundary + b"--\r\n"))

    def test_construction_reads_nothing_and_iteration_is_bounded_without_rewinding(self):
        data = DATA * 100
        reader = TrackedReader(data)
        with mock.patch.object(immich, "DOWNLOAD_CHUNK", 17):
            upload = body(source(data, reader=reader))
            self.assertEqual(reader.read_sizes, [])
            self.assertEqual(reader.tell(), 0)
            self.assertFalse(upload.complete)
            chunks = list(upload)
        self.assertEqual(sum(map(len, chunks)), int(upload.headers["Content-Length"]))
        self.assertTrue(all(0 < size <= 17 for size in reader.read_sizes))
        self.assertEqual(reader.read_sizes[-1], 1)
        self.assertEqual(reader.tell(), len(data))
        self.assertFalse(reader.closed)

    def test_large_declared_length_is_computed_without_allocating_or_reading_media(self):
        reader = TrackedReader(DATA)
        size = 5 * 1024 ** 4 + 17
        upload = body(source(reader=reader, size=size))
        self.assertGreater(int(upload.headers["Content-Length"]), size)
        self.assertLess(int(upload.headers["Content-Length"]) - size, 8192)
        self.assertEqual(reader.read_sizes, [])

    def test_short_long_changed_and_advanced_sources_cannot_finish_a_body(self):
        advanced = io.BytesIO(DATA)
        advanced.read(1)
        for original in (source(size=len(DATA) + 1), source(size=len(DATA) - 1), source(sha1="a" * 40), source(reader=advanced)):
            upload = body(original)
            with self.subTest(original=original), self.assertRaises(immich.ImmichFailed):
                b"".join(upload)
            self.assertFalse(upload.complete)
            self.assertFalse(original.reader.closed)

    def test_sidecar_is_also_checked_and_sources_must_be_independent(self):
        with self.assertRaises(immich.ChecksumMismatch):
            b"".join(body(sidecar=source(SIDECAR, sha1="a" * 40)))
        original = source()
        with self.assertRaisesRegex(immich.ImmichFailed, "independent"):
            body(original, sidecar=original)

    def test_consumed_or_abandoned_bodies_cannot_be_reused(self):
        upload = body()
        b"".join(upload)
        with self.assertRaisesRegex(immich.ImmichFailed, "only be consumed once"):
            b"".join(upload)
        abandoned = body()
        iterator = iter(abandoned)
        next(iterator)
        iterator.close()
        self.assertFalse(abandoned.complete)
        with self.assertRaisesRegex(immich.ImmichFailed, "only be consumed once"):
            b"".join(abandoned)

    def test_invalid_source_shape_is_refused_before_reading(self):
        for changes in ({"size": 0}, {"size": -1}, {"size": True}, {"sha1": "short"}, {"sha1": "G" * 40}, {"reader": None}):
            reader = TrackedReader(DATA)
            with self.subTest(changes=changes), self.assertRaises(immich.ImmichFailed):
                source(**({"reader": reader} | changes))
            self.assertEqual(reader.read_sizes, [])
        for original, sidecar in ((None, None), (source(), b"not a recorded source")):
            with self.subTest(original=type(original).__name__), self.assertRaises(immich.ImmichFailed):
                immich.MultipartUpload(original, "generated.jpg", CREATED, MODIFIED, sidecar=sidecar)

    def test_bad_filenames_and_naive_unknown_or_invalid_offsets_are_refused(self):
        filenames = ("", "no-extension", ".JPG", "../photo.jpg", "a/b.jpg", "a\\b.jpg", "a\r\nInjected: value.jpg",
                     "a\x00.jpg", 'bad.jpg";part=x', "x" * 4096 + ".jpg", "\ud800.jpg")
        for filename in filenames:
            with self.subTest(filename=filename), self.assertRaises(immich.ImmichFailed):
                body(filename=filename)
        for stamp in ("", "2024-01-02", "2024-01-02T03:04:05", "2024-01-02 03:04:05+05:45", "2024-01-02T03:04:05-00:00",
                      "2024-02-30T03:04:05Z", "2024-01-02T03:04:05+25:00", CREATED + "\r\nInjected: value"):
            for field in ("file_created_at", "file_modified_at"):
                with self.subTest(stamp=stamp, field=field), self.assertRaises(immich.ImmichFailed):
                    body(**{field: stamp})
        for asset_id in ("", "not-an-id", ASSET_ID.upper(), "00000000-0000-0000-0000-000000000000"):
            with self.subTest(asset_id=asset_id), self.assertRaises(immich.ImmichFailed):
                body(live_video_id=asset_id)

    def test_closed_faulty_and_nonbinary_readers_fail_with_fixed_private_errors(self):
        closed = io.BytesIO(DATA)
        closed.close()
        faulty = mock.Mock()
        faulty.read.side_effect = OSError("private-source-read-canary")
        nonbinary = mock.Mock()
        nonbinary.read.return_value = "not binary"
        oversized = mock.Mock()
        oversized.read.return_value = DATA + b"extra"
        replies = []
        with no_leaks(self, "private-source-read-canary", replies=replies):
            for reader in (closed, faulty, nonbinary, oversized):
                with self.subTest(reader=type(reader).__name__), self.assertRaises(immich.ImmichFailed) as caught:
                    b"".join(body(source(reader=reader)))
                replies.append(str(caught.exception).encode())

    def test_representations_and_rejections_do_not_expose_private_metadata(self):
        canary = "private-filename-canary"
        replies = []
        with no_leaks(self, canary, replies=replies):
            original = source()
            upload = body(original, filename=canary + ".jpg")
            replies += [repr(original).encode(), repr(upload).encode()]
            for changes in ({"filename": canary + "\r\n.jpg"}, {"file_created_at": canary}, {"live_video_id": canary}):
                with self.assertRaises(immich.ImmichFailed) as caught:
                    body(**changes)
                replies.append(str(caught.exception).encode())


class UploadResultTests(unittest.TestCase):
    def test_only_created_and_duplicate_pairs_return_ids_without_claiming_verification(self):
        for http_status, status in ((201, "created"), (200, "duplicate")):
            result = immich.upload_result(http_status, {"id": ASSET_ID, "status": status})
            self.assertEqual((result.asset_id, result.status), (ASSET_ID, status))
            self.assertNotIn(ASSET_ID, repr(result))

    def test_malformed_ids_statuses_and_conflicting_http_codes_are_refused(self):
        for found in (None, [], {}, {"id": "bad", "status": "created"}, {"id": ASSET_ID.upper(), "status": "created"},
                      {"id": str(UUID(int=0)), "status": "created"}, {"id": ASSET_ID}, {"id": ASSET_ID, "status": "accepted"},
                      {"id": ASSET_ID, "status": ["created"]}):
            with self.subTest(found=found), self.assertRaises(immich.ImmichFailed):
                immich.upload_result(201, found)
        for code, status in ((200, "created"), (201, "duplicate"), (202, "created"), (500, "created"), (201.0, "created"), (True, "created")):
            with self.subTest(code=code, status=status), self.assertRaises(immich.ImmichFailed):
                immich.upload_result(code, {"id": ASSET_ID, "status": status})

    def test_generic_upload_transport_still_refuses_before_network_dispatch(self):
        with mock.patch.object(tls, "urlopen") as opened, self.assertRaisesRegex(immich.ImmichFailed, "not allowed"):
            immich.Client("https://immich.example.test", "admin-canary").call("POST", "/api/assets", key="person-canary")
        opened.assert_not_called()
