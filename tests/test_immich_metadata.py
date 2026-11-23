import dataclasses
import hashlib
import io
import json
import re
import unittest
from datetime import UTC, datetime
from unittest import mock

from PIL import ExifTags, Image
from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from darkroom.domain import managed_metadata, managed_upload, xmp
from darkroom.domain.managed_metadata import Resource, complete_live_photo, complete_receipt, intend, snapshot
from darkroom.providers import immich
from darkroom.storage import db
from darkroom.storage.models import Export, GoogleAccount, ManagedImportIntent, ManagedImportReceipt, Person
from tests.fakes.immich import FakeAsset, FakeImmich, FakePerson, extract
from tests.privacy import no_leaks
from tests.shared import own_database_url

OWNER = "11111111-1111-4111-8111-111111111111"
EXISTING = "33333333-3333-4333-8333-333333333333"
PERSON_KEY = "metadata-person-key-canary"
ADMIN_KEY = "metadata-admin-key-canary"
FILENAME = "generated-metadata-name-canary.jpg"
VIDEO_NAME = "generated-live-video-canary.mov"
RAW_NAME = "generated-raw-name-canary.dng"
GRANTS = ("asset.download", "asset.read", "asset.upload", "server.about", "user.read")
NOW = 1_800_000_000.0
ROME, ACCRA = (45.4340, 12.3388), (5.6037, -0.1870)
SUMMER = 1562243415.0
DESCRIBED = 'A made-up <square> & "quay"'
ALLOWED = re.compile(r"GET /api/(users/me|server/about|api-keys/me|assets/[0-9a-f-]{36}(/original)?)|POST /api/(assets|assets/bulk-upload-check|search/metadata)")


def sha1(data):
    return hashlib.sha1(data, usedforsecurity=False).hexdigest()


def utc(text):
    return datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp()


def photo(seed, taken=None, offset=None, place=None, description=None):
    exif, tags = Image.Exif(), {}
    if description is not None:
        exif[ExifTags.Base.ImageDescription] = tags["IFD0:ImageDescription"] = description
    if taken is not None:
        exif.get_ifd(ExifTags.IFD.Exif)[ExifTags.Base.DateTimeOriginal] = tags["ExifIFD:DateTimeOriginal"] = taken
    if offset is not None:
        exif.get_ifd(ExifTags.IFD.Exif)[ExifTags.Base.OffsetTimeOriginal] = tags["ExifIFD:OffsetTimeOriginal"] = offset
    if place is not None:
        def dms(value):
            return float(int(abs(value))), float(int(abs(value) * 60) % 60), round(abs(value) * 3600 % 60, 4)
        exif.get_ifd(ExifTags.IFD.GPSInfo).update({1: "N" if place[0] >= 0 else "S", 2: dms(place[0]), 3: "E" if place[1] >= 0 else "W", 4: dms(place[1])})
        tags["Composite:GPSLatitude"], tags["Composite:GPSLongitude"] = place
    out = io.BytesIO()
    Image.new("RGB", (8, 8), tuple(hashlib.sha1(seed.encode()).digest()[:3])).save(out, "JPEG", exif=exif.tobytes())
    return out.getvalue(), tags


class ManagedMetadataTests(unittest.TestCase):
    def setUp(self):
        self.engine = db.make_engine(own_database_url(self))
        self.addCleanup(self.engine.dispose)
        db.migrate(self.engine)
        with self.engine.begin() as connection:
            connection.execute(insert(Person).values(id=1, name="Jo", slug="jo", originals_path="o", google_only_path="g", created=1.0))
            connection.execute(insert(GoogleAccount).values(id=1, email="jo@example.test", default_person_id=1, created=1.0))
            connection.execute(insert(Export).values(id=1, account_id=1, kind="manual"))
            connection.execute(insert(ManagedImportIntent).values(id=1, person_id=1, export_id=1, owner_id=OWNER, resource_manifest="{}", created_at=1.0))
        self.fake = FakeImmich([FakePerson("jo", OWNER, "lib-jo", PERSON_KEY)], ADMIN_KEY).start()
        self.addCleanup(self.fake.stop)
        self.fake.state.version, self.fake.state.permissions = "v3.4.1", GRANTS
        self.reader = immich.ManagedReader(immich.Client(self.fake.url, ADMIN_KEY, timeout=5), PERSON_KEY, OWNER, access="import")
        self.opened = Session(self.engine, expire_on_commit=False)
        self.addCleanup(self.opened.close)
        self.paused = []

    def resource(self, key, data, intended, filename=FILENAME, compound=None, expected=None):
        with self.engine.begin() as connection:
            receipt_id = connection.execute(insert(ManagedImportReceipt).values(
                intent_id=1, resource_key=key, compound_key=compound or key, source_sha1=sha1(data), expected_bytes=len(data),
                expected_metadata=expected or snapshot(intended), updated_at=1.0)).inserted_primary_key[0]
        return Resource(receipt_id, lambda: immich.UploadSource(io.BytesIO(data), len(data), sha1(data)), filename)

    def complete(self, resource, **values):
        return complete_receipt(self.opened, self.reader, resource, NOW, pause=self.paused.append, **values)

    def commit(self, compound):
        return managed_metadata.commit_compound(self.opened, self.reader, 1, compound, NOW)

    def row(self, resource):
        receipt = ManagedImportReceipt
        with self.engine.connect() as connection:
            return connection.execute(select(receipt.state, receipt.metadata_hold, receipt.upload_outcome, receipt.asset_id, receipt.metadata_evidence)
                                      .where(receipt.id == resource.receipt_id)).one()

    def asset(self, data):
        return next(asset for asset in self.fake.state.assets if asset.data == data)

    def uploads(self):
        return self.fake.requests().count(("POST", "/api/assets"))

    def assert_only_reads_and_uploads(self):
        self.assertEqual([f"{m} {p}" for m, p in self.fake.requests() if not ALLOWED.fullmatch(f"{m} {p}")], [])

    def test_a_new_photo_carries_only_the_fields_it_lacks_and_commits_only_after_extraction_matches(self):
        data, tags = photo("new", "2019:07:04 14:30:15", "+02:00", ROME)
        intended = intend(tags, xmp.GoogleMetadata(*ROME, SUMMER, DESCRIBED), "IMAGE")
        self.assertEqual((intended.sidecar, intended.taken_utc, intended.offset), (((xmp.DESCRIPTION, DESCRIBED),), SUMMER, 120))
        self.assertEqual((intended.taken_from, intended.place_from, intended.description_from, intended.hold), ("media", "media", "sidecar", None))
        self.assertEqual(managed_metadata.intended_of(snapshot(intended)), intended)
        item = self.resource("part/new", data, intended)
        self.assertEqual(self.complete(item).outcome, "verified")
        state, hold, outcome, asset_id, evidence = self.row(item)
        self.assertEqual((state, hold, outcome, asset_id, json.loads(evidence)["zone"]), ("metadata_verified", None, "created", self.asset(data).id, "UTC+02:00"))
        sent = self.asset(data).sidecar
        self.assertIn(b"A made-up &lt;square&gt; &amp; \"quay\"", sent)
        self.assertNotIn(b"GPSLatitude", sent)
        self.assertNotIn(b"DateTimeOriginal", sent)
        self.assertEqual(self.commit("part/new").outcome, "verified")
        self.assertEqual(self.row(item)[0], "committed")
        self.assertEqual(self.uploads(), 1)
        self.assert_only_reads_and_uploads()

    def test_conflicting_exif_and_xmp_must_show_the_xmp_date_and_an_exif_date_that_wins_stays_pending(self):
        google = xmp.GoogleMetadata(*ROME, utc("2020-01-04T12:00:00"), None)
        data, tags = photo("conflict", "2020:01:01 11:00:00", "+01:00", ROME)
        intended = intend(tags, google, "IMAGE")
        self.assertEqual((intended.taken_from, intended.taken_utc, intended.offset), ("sidecar", utc("2020-01-04T12:00:00"), 60))
        self.assertEqual(self.complete(self.resource("part/conflict", data, intended)).outcome, "verified")
        self.assertIn(b'exif:DateTimeOriginal="2020-01-04T13:00:00+01:00"', self.asset(data).sidecar)
        lost, tags = photo("exif-wins", "2020:01:01 11:00:00", "+01:00", ROME)
        item = self.resource("part/exif-wins", lost, intend(tags, google, "IMAGE"))
        self.fake.state.extraction_delay = 600
        self.assertEqual(self.complete(item).reason, managed_metadata.REASONS["not_extracted"])
        self.assertEqual(self.row(item)[:2], ("bytes_verified", None))
        uploaded = self.asset(lost)
        uploaded.exif["dateTimeOriginal"], uploaded.ready_at = "2020-01-01T10:00:00Z", 0.0
        self.assertEqual(self.complete(item).reason, managed_metadata.REASONS["mismatch"])
        self.assertEqual(self.commit("part/exif-wins").outcome, "pending")
        self.assertEqual(self.row(item)[:3], ("bytes_verified", "mismatch", "created"))
        agreeing, tags = photo("agrees", "2020:01:01 11:00:00", "+01:00", ROME)
        intended = intend(tags, xmp.GoogleMetadata(*ROME, utc("2020-01-01T11:00:00"), None), "IMAGE")
        self.assertEqual((intended.sidecar, intended.taken_from, intended.taken_utc), ((), "media", utc("2020-01-01T10:00:00")))
        self.fake.state.extraction_delay = 0
        self.assertEqual(self.complete(self.resource("part/agrees", agreeing, intended)).outcome, "verified")
        self.assertIsNone(self.asset(agreeing).sidecar)

    def test_utc_plus_zero_is_a_known_offset_that_verifies(self):
        data, tags = photo("zero", "2021:03:04 05:06:07", "+00:00")
        intended = intend(tags, xmp.GoogleMetadata(None, None, utc("2021-03-04T05:06:07"), None), "IMAGE")
        self.assertEqual((intended.offset, intended.taken_from, intended.hold), (0, "media", None))
        self.assertEqual(self.complete(self.resource("part/zero", data, intended)).outcome, "verified")
        data, tags = photo("accra")
        intended = intend(tags, xmp.GoogleMetadata(*ACCRA, utc("2021-03-04T05:06:07"), None), "IMAGE")
        self.assertEqual((intended.offset, intended.taken_from, intended.place_from), (0, "sidecar", "sidecar"))
        self.assertEqual(self.complete(self.resource("part/accra", data, intended)).outcome, "verified")
        self.assertIn(b'exif:DateTimeOriginal="2021-03-04T05:06:07+00:00"', self.asset(data).sidecar)

    def test_zones_immich_reports_are_read_as_offsets_at_the_instant(self):
        winter, summer = utc("2020-01-04T12:00:00"), SUMMER
        cases = [("UTC", winter, 0), ("UTC+0", winter, 0), ("UTC+00:00", winter, 0), ("UTC+2", winter, 120), ("UTC-03:30", winter, -210), ("UTC+5:45", winter, 345),
                 ("+0100", winter, 60), ("Africa/Accra", winter, 0), ("Europe/Rome", winter, 60), ("Europe/Rome", summer, 120),
                 ("UTC+15", winter, None), ("UTC+2:75", winter, None), ("Not/AZone", winter, None), ("../etc", winter, None), ("", winter, None), (None, winter, None)]
        for zone, instant, offset in cases:
            with self.subTest(zone=zone):
                self.assertEqual(managed_metadata.zone_offset(zone, instant), offset)

    def test_an_unknown_offset_or_no_capture_time_is_held_for_review_before_anything_is_sent(self):
        clock, clock_tags = photo("clock", "2021:03:04 05:06:07")
        bare, bare_tags = photo("bare")
        cases = [("wall clock only", clock, clock_tags, xmp.GoogleMetadata(None, None, utc("2021-03-04T09:06:07"), None), "unknown_offset"),
                 ("google date without a place", bare, bare_tags, xmp.GoogleMetadata(None, None, utc("2021-03-04T05:06:07"), None), "unknown_offset"),
                 ("no date at all", bare + b"\x00", bare_tags, xmp.GoogleMetadata(*ROME, None, DESCRIBED), "no_instant")]
        for name, data, tags, google, hold in cases:
            with self.subTest(name):
                intended = intend(tags, google, "IMAGE")
                self.assertEqual((intended.hold, intended.offset, json.loads(snapshot(intended))["offset"]), (hold, None, None))
                item = self.resource(name, data, intended)
                report = self.complete(item)
                self.assertEqual((report.outcome, report.reason), ("held", managed_metadata.REASONS[hold]))
                self.assertEqual(self.row(item)[:2], ("prepared", hold))
        self.assertEqual(self.fake.requests(), [])
        quicktime = {"QuickTime:CreateDate": "2019:07:04 12:30:15"}
        placed = intend(quicktime, xmp.GoogleMetadata(*ROME, SUMMER, None), "VIDEO")
        self.assertEqual((placed.taken_utc, placed.offset, placed.taken_from, placed.place_from, placed.hold), (SUMMER, 120, "media", "sidecar", None))
        self.assertEqual(intend(quicktime, xmp.GoogleMetadata(None, None, SUMMER, None), "VIDEO").hold, "unknown_offset")

    def test_a_wall_clock_that_agrees_with_googles_instant_takes_their_difference_and_one_that_disagrees_stays_held(self):
        data, tags = photo("agreed-clock", "2019:07:04 14:30:15")
        intended = intend(tags, xmp.GoogleMetadata(*ROME, SUMMER, None), "IMAGE")
        self.assertEqual((intended.taken_utc, intended.offset, intended.taken_from, intended.place_from, intended.hold), (SUMMER, 120, "sidecar_offset", "sidecar", None))
        self.assertEqual((managed_metadata.intended_of(snapshot(intended)), set(dict(intended.sidecar))), (intended, {xmp.LATITUDE, xmp.LONGITUDE}))
        for google, corrects in ((xmp.GoogleMetadata(*ROME, SUMMER + 420, None), True), (xmp.GoogleMetadata(*ROME, SUMMER, None), False),
                                 (xmp.GoogleMetadata(None, None, SUMMER, None), True), (xmp.GoogleMetadata(*ACCRA, SUMMER, None), True)):
            with self.subTest(apart=google.taken_utc - SUMMER, corrects=corrects):
                self.assertEqual(intend(tags, google, "IMAGE", corrects=corrects).hold, "unknown_offset")
        item = self.resource("part/agreed-clock", data, intended)
        self.fake.state.extraction_delay = 600
        self.assertEqual(self.complete(item).outcome, "pending")
        uploaded = self.asset(data)
        uploaded.ready_at = 0.0
        self.assertEqual(self.complete(item).reason, managed_metadata.REASONS["mismatch"])
        uploaded.exif |= {"dateTimeOriginal": "2019-07-04T12:30:15.000Z", "timeZone": "Europe/Rome"}
        self.assertEqual((self.complete(item).outcome, self.row(item)[:2]), ("verified", ("metadata_verified", None)))

    def test_a_description_xml_cannot_carry_is_stored_escaped_and_held_unwritable_before_anything_is_sent(self):
        data, tags = photo("unwritable", "2019:07:04 14:30:15", "+02:00", ROME)
        for number, text in enumerate(("made-up \x01 caption", "made-up \ud800 caption", "made-up \ufffe caption")):
            with self.subTest(code=hex(ord(text[8]))):
                intended = intend(tags, xmp.GoogleMetadata(*ROME, SUMMER, text), "IMAGE")
                stored = snapshot(intended)
                self.assertEqual((intended.hold, intended.description, stored.isascii(), managed_metadata.intended_of(stored)), ("unwritable", text, True, intended))
                item = self.resource(f"part/unwritable-{number}", data, intended)
                self.assertEqual((self.complete(item).outcome, self.row(item)[:2]), ("held", ("prepared", "unwritable")))
        self.assertEqual(self.fake.requests(), [])

    def test_a_time_resolved_at_a_position_is_held_when_the_place_immich_sees_is_in_another_zone(self):
        near = (45.4400, 12.3300)
        data, clock = photo("moved", "2019:07:04 14:30:15", None, ROME)
        quicktime = {"QuickTime:CreateDate": "2019:07:04 12:30:15", "Composite:GPSLatitude": ROME[0], "Composite:GPSLongitude": ROME[1]}
        for name, own, kind in (("wall clock at its own position", clock, "IMAGE"), ("quicktime instant", quicktime, "VIDEO")):
            with self.subTest(name):
                found = [intend(own, xmp.GoogleMetadata(*place, SUMMER, None), kind) for place in (ROME, near, ACCRA)]
                self.assertEqual([(intended.hold, intended.offset, intended.place_from) for intended in found],
                                 [(None, 120, "media"), (None, 120, "sidecar"), ("unknown_offset", None, "sidecar")])
        moved = self.resource("part/moved", data, intend(clock, xmp.GoogleMetadata(*ACCRA, SUMMER, None), "IMAGE"))
        self.assertEqual((self.complete(moved).outcome, self.fake.requests()), ("held", []))

    def test_a_missing_description_stays_missing_and_an_intended_one_must_be_extracted(self):
        data, tags = photo("plain", "2019:07:04 14:30:15", "+02:00", ROME)
        intended = intend(tags, xmp.GoogleMetadata(*ROME, SUMMER, None), "IMAGE")
        self.assertEqual((intended.description, intended.sidecar), (None, ()))
        self.assertEqual(self.complete(self.resource("part/plain", data, intended)).outcome, "verified")
        own, tags = photo("own-description", "2019:07:04 14:30:15", "+02:00", ROME, "A made-up caption")
        intended = intend(tags, xmp.GoogleMetadata(*ROME, SUMMER, DESCRIBED), "IMAGE")
        self.assertEqual((intended.description, intended.description_from, intended.sidecar), ("A made-up caption", "media", ()))
        self.assertEqual(self.complete(self.resource("part/own", own, intended)).outcome, "verified")
        blank, tags = photo("blank", "2019:07:04 14:30:15", "+02:00", ROME)
        item = self.resource("part/blank", blank, intend(tags, xmp.GoogleMetadata(*ROME, SUMMER, DESCRIBED), "IMAGE"))
        self.fake.state.extraction_delay = 600
        self.complete(item)
        self.asset(blank).exif["description"], self.asset(blank).ready_at = "", 0.0
        self.assertEqual(self.complete(item).reason, managed_metadata.REASONS["mismatch"])
        self.assertEqual(self.row(item)[:2], ("bytes_verified", "mismatch"))

    def test_a_duplicate_that_lacks_the_intended_metadata_is_recorded_not_enriched_and_never_changed(self):
        data, tags = photo("dup", "2019:07:04 14:30:15", "+02:00", ROME)
        self.fake.add_asset(FakeAsset(EXISTING, OWNER, data, exif=extract(data, None), metadata=()))
        item = self.resource("part/dup", data, intend(tags, xmp.GoogleMetadata(*ROME, SUMMER, DESCRIBED), "IMAGE"))
        self.assertEqual(self.complete(item).reason, managed_metadata.REASONS["not_enriched"])
        self.assertEqual(self.row(item)[:4], ("bytes_verified", "not_enriched", "duplicate", EXISTING))
        self.assertEqual((len(self.fake.state.assets), self.fake.state.assets[0].exif["description"], self.fake.state.assets[0].sidecar), (1, None, None))
        self.assertEqual(self.commit("part/dup").outcome, "pending")
        again = self.resource("part/dup-plain", data, intend(tags, xmp.GoogleMetadata(*ROME, SUMMER, None), "IMAGE"))
        self.assertEqual(self.complete(again).outcome, "verified")
        self.assert_only_reads_and_uploads()

    def live_photo(self, compound="part/live"):
        google = xmp.GoogleMetadata(*ROME, SUMMER, None)
        video = b"generated live photo video bytes"
        still, tags = photo("live-still", "2019:07:04 14:30:15", "+02:00", ROME)
        video_intent = intend({}, google, "VIDEO", "live_video", corrects=False)
        self.assertEqual((video_intent.taken_from, video_intent.place_from), ("sidecar", "sidecar"))
        return (self.resource(f"{compound}/video", video, video_intent, VIDEO_NAME, compound),
                self.resource(f"{compound}/still", still, intend(tags, google, "IMAGE", "live_still"), FILENAME, compound), video, still)

    def test_a_live_photo_sends_its_video_first_and_resumes_after_an_interruption_between_the_halves(self):
        video, still, video_data, still_data = self.live_photo()
        sent = []

        def still_fails(method, path, slug):
            if (method, path) == ("POST", "/api/assets"):
                sent.append(path)
                return 503 if len(sent) > 1 else None
            return None
        self.fake.state.intercept = still_fails
        self.assertEqual(complete_live_photo(self.opened, self.reader, video, still, NOW, self.paused.append).outcome, "pending")
        self.assertEqual((self.row(video)[0], self.row(still)[0], len(self.fake.state.assets)), ("metadata_verified", "outcome_unknown", 1))
        self.fake.state.intercept = None
        self.assertEqual(complete_live_photo(self.opened, self.reader, video, still, NOW, self.paused.append).outcome, "verified")
        moving, stored = self.asset(video_data), self.asset(still_data)
        self.assertEqual((len(self.fake.state.assets), self.uploads()), (2, 1 + managed_upload.MAX_SENDS + 1))
        self.assertEqual((stored.live_video_id, moving.visibility, moving.kind), (moving.id, "hidden", "VIDEO"))
        self.assertEqual((self.row(video)[0], self.row(still)[0]), ("committed", "committed"))

    def test_a_still_is_never_sent_before_its_video_and_an_unlinked_still_never_commits(self):
        video, still, _, still_data = self.live_photo()
        self.assertEqual(self.complete(still).outcome, "pending")
        self.fake.state.intercept = lambda method, path, slug: 400 if (method, path) == ("POST", "/api/assets") else None
        self.assertEqual(complete_live_photo(self.opened, self.reader, video, still, NOW).outcome, "pending")
        self.assertEqual((self.uploads(), self.row(still)[0]), (1, "prepared"))
        self.fake.state.intercept = None
        self.fake.add_asset(FakeAsset(EXISTING, OWNER, still_data, metadata=()))
        report = complete_live_photo(self.opened, self.reader, video, still, NOW, self.paused.append)
        self.assertEqual(report.reason, managed_metadata.REASONS["not_linked"])
        self.assertEqual((self.row(video)[0], self.row(still)[:2]), ("metadata_verified", ("bytes_verified", "not_linked")))
        self.assertEqual(self.commit("part/live").outcome, "pending")
        self.assertEqual(complete_live_photo(self.opened, self.reader, still, video, NOW).outcome, "pending")

    def test_raw_and_jpeg_stay_two_unlinked_assets_that_commit_only_together(self):
        google = xmp.GoogleMetadata(*ROME, SUMMER, None)
        raw_data, (jpeg_data, tags) = b"generated raw sensor bytes", photo("raw-jpeg", "2019:07:04 14:30:15", "+02:00", ROME)
        raw = self.resource("part/shot/raw", raw_data, intend({}, google, "IMAGE"), RAW_NAME, "part/shot")
        jpeg = self.resource("part/shot/jpeg", jpeg_data, intend(tags, google, "IMAGE"), FILENAME, "part/shot")
        self.assertEqual(self.complete(raw).outcome, "verified")
        self.assertEqual((self.commit("part/shot").outcome, self.row(raw)[0]), ("pending", "metadata_verified"))
        self.assertEqual(self.complete(jpeg).outcome, "verified")
        self.assertEqual(self.commit("part/shot").outcome, "verified")
        self.assertEqual({self.row(raw)[0], self.row(jpeg)[0]}, {"committed"})
        self.assertEqual([(asset.kind, asset.live_video_id) for asset in self.fake.state.assets], [("IMAGE", None), ("IMAGE", None)])
        self.assertEqual(self.commit("part/shot").outcome, "verified")
        self.assertEqual(self.commit("part/none").outcome, "pending")

    def test_an_item_whose_asset_changed_after_verification_or_whose_members_do_not_fit_never_commits(self):
        data, tags = photo("later", "2019:07:04 14:30:15", "+02:00", ROME)
        intended = intend(tags, xmp.GoogleMetadata(*ROME, SUMMER, None), "IMAGE")
        item = self.resource("part/later", data, intended)
        self.assertEqual(self.complete(item).outcome, "verified")
        uploaded = self.asset(data)
        for change, outcome in (({"trashed": True}, "unusable"), ({"checksum": "AAAAAAAAAAAAAAAAAAAAAAAAAAA="}, "unusable"), ({}, "pending")):
            uploaded.trashed, uploaded.checksum = change.get("trashed", False), change.get("checksum")
            if not change:
                with self.engine.begin() as connection:
                    connection.execute(update(ManagedImportReceipt).values(expected_metadata=snapshot(dataclasses.replace(intended, role="live_still"))))
            with self.subTest(change=change):
                self.assertEqual((self.commit("part/later").outcome, self.row(item)[0]), (outcome, "metadata_verified"))

    def commit_while(self, name, change):
        data, tags = photo(name, "2019:07:04 14:30:15", "+02:00", ROME)
        intended = intend(tags, xmp.GoogleMetadata(*ROME, SUMMER, None), "IMAGE")
        item = self.resource(f"{name}/one", data, intended, compound=name)
        self.assertEqual(self.complete(item).outcome, "verified")
        real = immich.ManagedReader.get_asset

        def meanwhile(reader, asset_id):
            change(item, data, intended)
            return real(reader, asset_id)
        with mock.patch.object(immich.ManagedReader, "get_asset", autospec=True, side_effect=meanwhile):
            return item, self.commit(name)

    def test_a_commit_that_finds_the_item_changed_under_it_commits_nothing(self):
        def appears(item, data, intended):
            self.resource("appears/late", data + b"late", intended, compound="appears")

        def committed_elsewhere(item, data, intended):
            with self.engine.begin() as connection:
                connection.execute(update(ManagedImportReceipt).where(ManagedImportReceipt.id == item.receipt_id).values(state="committed", committed_at=2.0))
        item, report = self.commit_while("appears", appears)
        self.assertEqual((report.reason, self.row(item)[0]), ("the item changed while it was checked", "metadata_verified"))
        item, report = self.commit_while("elsewhere", committed_elsewhere)
        with self.engine.connect() as connection:
            stamped = connection.execute(select(ManagedImportReceipt.committed_at).where(ManagedImportReceipt.id == item.receipt_id)).scalar_one()
        self.assertEqual((report.reason, stamped), ("the item changed while it was checked", 2.0))

    def test_no_receipt_is_complete_on_a_201_alone_and_extraction_is_polled_a_bounded_number_of_times(self):
        data, tags = photo("slow", "2019:07:04 14:30:15", "+02:00", ROME)
        item = self.resource("part/slow", data, intend(tags, xmp.GoogleMetadata(*ROME, SUMMER, DESCRIBED), "IMAGE"))
        self.fake.state.extraction_delay = 600
        self.assertEqual(self.complete(item).outcome, "pending")
        self.assertEqual(self.row(item)[:3], ("bytes_verified", None, "created"))
        self.assertEqual(self.paused, [managed_metadata.POLL_SECONDS] * (managed_metadata.POLLS - 1))
        self.assertEqual(self.commit("part/slow").outcome, "pending")
        self.asset(data).ready_at = 0.0
        self.assertEqual(self.complete(item).outcome, "verified")
        self.assertEqual((self.uploads(), self.commit("part/slow").outcome, self.row(item)[0]), (1, "verified", "committed"))

    def test_an_asset_that_goes_away_or_cannot_be_read_after_upload_is_never_verified(self):
        data, tags = photo("gone", "2019:07:04 14:30:15", "+02:00", ROME)
        item = self.resource("part/gone", data, intend(tags, xmp.GoogleMetadata(*ROME, SUMMER, DESCRIBED), "IMAGE"))
        self.fake.state.extraction_delay = 600
        self.complete(item)
        self.fake.state.intercept = lambda method, path, slug: 500 if method == "GET" and path.startswith("/api/assets/") else None
        self.assertEqual(self.complete(item).outcome, "pending")
        self.fake.state.intercept = None
        self.asset(data).trashed = True
        self.assertEqual(self.complete(item).outcome, "unusable")
        self.assertEqual((self.row(item)[0], self.asset(data).trashed), ("bytes_verified", True))
        self.assert_only_reads_and_uploads()

    def test_intended_metadata_that_is_unreadable_or_invents_nothing_to_check_is_never_sent(self):
        data, tags = photo("unread", "2019:07:04 14:30:15", "+02:00", ROME)
        held_offset = json.loads(snapshot(intend(tags, xmp.GoogleMetadata(*ROME, SUMMER, None), "IMAGE"))) | {"offset": None}
        for number, expected in enumerate(("{}", "not json", json.dumps(held_offset), json.dumps(held_offset | {"offset": 120, "sidecar": {"XMP-dc:Title": "x"}}))):
            with self.subTest(expected=expected[:20]):
                self.assertEqual(self.complete(self.resource(f"part/unread-{number}", data, None, expected=expected)).outcome, "pending")
        self.assertEqual(self.fake.requests(), [])

    def test_no_key_address_or_filename_reaches_logs_reports_or_the_database(self):
        replies = []
        with no_leaks(self, PERSON_KEY, ADMIN_KEY, FILENAME, VIDEO_NAME, self.fake.url, database=self.engine, replies=replies):
            video, still, _, _ = self.live_photo()
            replies.append(repr(complete_live_photo(self.opened, self.reader, video, still, NOW, self.paused.append)).encode())
            replies.append(repr(video).encode())
        intended = intend({}, xmp.GoogleMetadata(*ROME, SUMMER, DESCRIBED), "IMAGE")
        for hidden in (DESCRIBED, "45.434", "12.3388"):
            self.assertNotIn(hidden, repr(intended))


if __name__ == "__main__":
    unittest.main()
