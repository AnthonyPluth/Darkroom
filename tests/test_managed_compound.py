import dataclasses
import hashlib
import unittest
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from darkroom.domain import managed_compound, managed_manifest, managed_metadata, xmp
from darkroom.domain.ingest import finish_export, pairing
from darkroom.domain.managed_compound import Planned, Refused
from darkroom.storage import db
from darkroom.storage.models import Export, GoogleAccount, ManagedCompound, ManagedCompoundMember, ManagedImportIntent, ManagedImportReceipt, Person, TakeoutEntry
from tests.shared import own_database_url

ROME = (45.4340, 12.3388)
NOW = 1_800_000_000.0
QUICKTIME = {"QuickTime:CreateDate": "2019:07:04 12:30:15"}
STILL_TAGS = {"ExifIFD:DateTimeOriginal": "2019:07:04 14:30:15", "ExifIFD:OffsetTimeOriginal": "+02:00"}
SIDECAR = '{"photoTakenTime": {"timestamp": "1300000000"}, "geoData": {"latitude": 45.434, "longitude": 12.3388, "altitude": 0.0}}'


def digest(text):
    return hashlib.sha1(text.encode(), usedforsecurity=False).hexdigest()


def canon_of(*names, without=()):
    folders = {}
    for name in names:
        folders.setdefault(Path(name).parent, []).append(Path(name).name)
    report = pairing.pair_names(sorted(folders.items()), {Path(name): digest(Path(name).name) for name in names})
    units, trashed = [], []
    for number, item in enumerate(report.items, 1):
        if str(item.path) not in without:
            unit = finish_export.Unit(item, TakeoutEntry(id=number, name=str(item.path), sha1=item.sha1, bytes=100 + number), None)
            (trashed if item.special == pairing.TRASHED else units).append(unit)
    return finish_export.Canon(report, tuple(units), tuple(trashed))


def named(canon, grouping):
    names = {unit.root.id: unit.root.name for unit in (*canon.units, *canon.trashed)}
    return (sorted((Path(pair.still.root.name).name, Path(pair.video.root.name).name) for pair in grouping.pairs),
            {Path(names[root]).name: reason for root, reason in grouping.held.items()})


class GroupingTests(unittest.TestCase):
    def group(self, *names, without=()):
        canon = canon_of(*names, without=without)
        return named(canon, managed_compound.groups(canon))

    def test_a_still_and_its_video_form_one_compound_keyed_by_the_still_and_a_plain_photo_stays_single(self):
        canon = canon_of("2019/IMG_0001.HEIC", "2019/IMG_0001.MOV", "2019/IMG_0002.JPG", "2019/PXL_0003.MP.jpg", "2019/PXL_0003.MP")
        grouping = managed_compound.groups(canon)
        self.assertEqual(named(canon, grouping), ([("IMG_0001.HEIC", "IMG_0001.MOV"), ("PXL_0003.MP.jpg", "PXL_0003.MP")], {}))
        self.assertEqual(grouping.pairs[0].key, f"takeout-live:{grouping.pairs[0].still.root.id}")

    def test_two_videos_claiming_one_still_form_no_compound_and_are_held_with_the_still(self):
        self.assertEqual(self.group("2019/IMG_0001.HEIC", "2019/IMG_0001.MOV", "2019/IMG_0001.MP4"),
                         ([], dict.fromkeys(("IMG_0001.HEIC", "IMG_0001.MOV", "IMG_0001.MP4"), "live_ambiguous")))

    def test_two_stills_of_one_stem_form_no_compound_and_the_video_and_both_stills_are_held(self):
        self.assertEqual(self.group("2019/IMG_0001.HEIC", "2019/IMG_0001.JPG", "2019/IMG_0001.MOV", "2019/IMG_0002.JPG"),
                         ([], dict.fromkeys(("IMG_0001.HEIC", "IMG_0001.JPG", "IMG_0001.MOV"), "live_ambiguous")))

    def test_a_pair_touching_an_ambiguous_still_is_held_rather_than_formed(self):
        self.assertEqual(self.group("2019/PXL_0001.jpg", "2019/PXL_0001.MP.jpg", "2019/PXL_0001.MP", "2019/PXL_0001.mov"),
                         ([], dict.fromkeys(("PXL_0001.jpg", "PXL_0001.MP.jpg", "PXL_0001.MP", "PXL_0001.mov"), "live_ambiguous")))

    def test_a_half_whose_partner_is_in_trash_or_not_a_root_forms_no_compound_and_is_held_partial(self):
        self.assertEqual(self.group("2019/IMG_0001.HEIC", "Trash/IMG_0001.HEIC", "Trash/IMG_0001.MOV"), ([], {"IMG_0001.HEIC": "live_partial"}))
        self.assertEqual(self.group("2019/IMG_0001.HEIC", "2019/IMG_0001.MOV", without=("2019/IMG_0001.HEIC",)), ([], {"IMG_0001.MOV": "live_partial"}))
        self.assertEqual(self.group("Trash/IMG_0001.HEIC", "Trash/IMG_0001.MOV"), ([], {}))

    def test_a_still_with_embedded_motion_is_recognised_and_a_plain_still_is_not(self):
        for tags, embedded in (({"XMP-GCamera:MotionPhoto": 1}, True), ({"XMP-GCamera:MicroVideo": 1}, True), ({"Samsung:EmbeddedVideoType": "MotionPhoto_Data"}, True),
                               ({"Trailer:MotionPhotoVideo": "(Binary data)"}, True), ({"XMP-GCamera:MotionPhoto": 0}, False), (STILL_TAGS, False)):
            with self.subTest(tags=tags):
                self.assertEqual(managed_compound.embedded_motion(tags), embedded)

    def test_a_borrowed_sidecar_never_corrects_the_video_and_a_video_without_offset_or_place_is_held(self):
        canon = canon_of("2019/IMG_0001.HEIC", "2019/IMG_0001.MOV")
        pair = managed_compound.groups(canon).pairs[0]
        video = managed_compound.planned(pair.video, "live_video", QUICKTIME, SIDECAR, borrowed=True)
        self.assertEqual(video.intended, managed_metadata.intend(QUICKTIME, xmp.GoogleMetadata.of(SIDECAR), "VIDEO", "live_video", corrects=False))
        self.assertEqual((video.intended.taken_utc, video.intended.taken_from, video.intended.offset), (datetime(2019, 7, 4, 12, 30, 15, tzinfo=UTC).timestamp(), "media", 120))
        self.assertEqual((video.resource_key, video.sha1, video.bytes), (f"takeout-entry:{pair.video.root.id}", pair.video.root.sha1, pair.video.root.bytes))
        self.assertEqual(managed_compound.planned(pair.video, "live_video", QUICKTIME, None, borrowed=True).intended.hold, "unknown_offset")
        self.assertEqual(managed_compound.planned(pair.still, "live_still", STILL_TAGS, SIDECAR, borrowed=False).intended.kind, "IMAGE")
        for tags in (None, {"Error": "unreadable"}):
            self.assertIsNone(managed_compound.planned(pair.still, "live_still", tags, SIDECAR, borrowed=False))
        self.assertIsNone(managed_compound.planned(dataclasses.replace(pair.still, root=TakeoutEntry(id=9, sha1=None, bytes=1)), "live_still", STILL_TAGS, None, False))


class DatabaseCase(unittest.TestCase):
    def setUp(self):
        self.engine = db.make_engine(own_database_url(self))
        self.addCleanup(self.engine.dispose)
        db.migrate(self.engine)
        with self.engine.begin() as connection:
            connection.execute(insert(Person).values(id=1, name="Jo", slug="jo", originals_path="o", google_only_path="g", created=1.0))
            connection.execute(insert(GoogleAccount).values(id=1, email="jo@example.test", default_person_id=1, created=1.0))
            connection.execute(insert(Export).values(id=1, account_id=1, kind="manual"))
            connection.execute(insert(ManagedImportIntent).values(id=1, person_id=1, export_id=1, owner_id="owner", resource_manifest=managed_manifest.TAKEOUT,
                                                                  created_at=1.0))
        self.opened = Session(self.engine, expire_on_commit=False)
        self.addCleanup(self.opened.close)

    def halves(self, video_tags=QUICKTIME, still="takeout-entry:1", video="takeout-entry:2", video_sidecar=SIDECAR):
        google = xmp.GoogleMetadata.of(SIDECAR)
        return [Planned(still, "live_still", digest("still"), 10, managed_metadata.intend(STILL_TAGS, google, "IMAGE", "live_still")),
                Planned(video, "live_video", digest("video"), 20, managed_metadata.intend(video_tags, xmp.GoogleMetadata.of(video_sidecar), "VIDEO", "live_video", corrects=False))]

    def freeze(self, members=None, key="takeout-live:1", wanted=lambda: True):
        return managed_compound.freeze(self.opened, 1, key, self.halves() if members is None else members, NOW, wanted)

    def rows(self):
        with self.engine.connect() as connection:
            compounds = connection.execute(select(ManagedCompound.compound_key, ManagedCompound.kind, ManagedCompound.hold, ManagedCompound.frozen_at,
                                                  ManagedCompound.committed_at)).all()
            members = connection.execute(select(ManagedCompoundMember.resource_key, ManagedCompoundMember.role, ManagedCompoundMember.expected_bytes)
                                         .order_by(ManagedCompoundMember.id)).all()
            receipts = connection.execute(select(ManagedImportReceipt.resource_key, ManagedImportReceipt.compound_key, ManagedImportReceipt.state,
                                                 ManagedImportReceipt.expected_metadata).order_by(ManagedImportReceipt.id)).all()
        return compounds, members, [(key, compound, state, getattr(managed_metadata.intended_of(raw), "role", None)) for key, compound, state, raw in receipts]


class FreezeTests(DatabaseCase):

    def test_freezing_writes_the_compound_its_two_members_and_two_prepared_receipts_and_a_second_freeze_reuses_them(self):
        first = self.freeze()
        expected = ([("takeout-live:1", "live_photo", None, NOW, None)], [("takeout-entry:1", "live_still", 10), ("takeout-entry:2", "live_video", 20)],
                    [("takeout-entry:1", "takeout-live:1", "prepared", "live_still"), ("takeout-entry:2", "takeout-live:1", "prepared", "live_video")])
        self.assertEqual(self.rows(), expected)
        self.assertEqual(set(first.receipts), {"takeout-entry:1", "takeout-entry:2"})
        changed = self.halves(video="takeout-entry:3")
        self.assertEqual(self.freeze(changed, wanted=lambda: False), first)
        self.assertEqual(self.rows(), expected)

    def test_a_half_held_before_upload_holds_the_whole_compound(self):
        frozen = self.freeze(self.halves(video_sidecar=None))
        self.assertEqual((frozen.hold, self.rows()[0][0][2], len(frozen.receipts)), ("unknown_offset", "unknown_offset", 2))

    def test_freezing_refuses_a_resource_with_a_receipt_or_another_compound_and_a_half_no_longer_wanted(self):
        self.freeze()
        for members, key, wanted in ((self.halves(still="takeout-entry:5", video="takeout-entry:2"), "takeout-live:5", lambda: True),
                                     (self.halves(still="takeout-entry:6", video="takeout-entry:7"), "takeout-live:6", lambda: False)):
            with self.subTest(key=key), self.assertRaises(Refused):
                self.freeze(members, key, wanted)
        with self.engine.begin() as connection:
            connection.execute(insert(ManagedImportReceipt).values(intent_id=1, resource_key="takeout-entry:8", compound_key="takeout-entry:8", source_sha1=digest("single"),
                                                                   expected_bytes=1, expected_metadata="{}", updated_at=1.0))
        with self.assertRaises(Refused):
            self.freeze(self.halves(still="takeout-entry:8", video="takeout-entry:9"), "takeout-live:8")
        self.assertEqual(len(self.rows()[0]), 1)
        self.assertEqual(len(self.rows()[1]), 2)

    def test_freezing_refuses_anything_but_one_still_and_one_video_that_match_their_roles(self):
        still, video = self.halves()
        image_video = dataclasses.replace(video, intended=dataclasses.replace(video.intended, kind="IMAGE"))
        for members, key in (([still, still], "takeout-live:1"), ([still], "takeout-live:1"), ([still, dataclasses.replace(video, resource_key=still.resource_key)], "takeout-live:1"),
                             ([still, image_video], "takeout-live:1"), ([still, dataclasses.replace(video, role="live_still")], "takeout-live:1"),
                             ([dataclasses.replace(still, sha1="short"), video], "takeout-live:1"), ([still, dataclasses.replace(video, bytes=0)], "takeout-live:1"),
                             ([still, video], "")):
            with self.subTest(members=members, key=key), self.assertRaises(Refused):
                self.freeze(members, key)
        self.assertEqual(self.rows(), ([], [], []))


class ManifestRuleTests(DatabaseCase):
    def refusal(self, key):
        return managed_metadata.manifest_refusal(self.opened, 1, key)

    def add(self, key, compound, sha1="a" * 40, size=1, role="single"):
        intended = managed_metadata.intend(STILL_TAGS, xmp.GoogleMetadata.of(None), "IMAGE", role)
        with self.engine.begin() as connection:
            connection.execute(insert(ManagedImportReceipt).values(intent_id=1, resource_key=key, compound_key=compound, source_sha1=sha1, expected_bytes=size,
                                                                   expected_metadata=managed_metadata.snapshot(intended), updated_at=1.0))

    def test_a_schema_two_single_is_one_receipt_keyed_by_its_own_resource(self):
        self.add("takeout-entry:5", "takeout-entry:5")
        self.assertIsNone(self.refusal("takeout-entry:5"))
        self.add("takeout-entry:6", "shot")
        self.add("takeout-entry:7", "shot")
        self.add("takeout-entry:8", "other")
        for key in ("shot", "other", "takeout-entry:9"):
            with self.subTest(key=key):
                self.assertEqual(self.refusal(key).outcome, "pending")

    def test_a_single_named_by_a_compound_member_is_refused(self):
        self.freeze()
        with self.engine.begin() as connection:
            connection.execute(update(ManagedImportReceipt).where(ManagedImportReceipt.resource_key == "takeout-entry:1").values(compound_key="takeout-entry:1"))
        self.assertEqual(self.refusal("takeout-entry:1").outcome, "pending")

    def test_a_compound_commits_only_while_its_receipts_equal_its_members_and_never_while_held(self):
        self.freeze()
        self.assertIsNone(self.refusal("takeout-live:1"))
        self.add("takeout-entry:3", "takeout-live:1", role="live_video")
        self.assertEqual(self.refusal("takeout-live:1").outcome, "pending")
        with self.engine.begin() as connection:
            connection.execute(update(ManagedImportReceipt).where(ManagedImportReceipt.resource_key == "takeout-entry:3").values(compound_key="takeout-entry:3"))
        self.assertIsNone(self.refusal("takeout-live:1"))
        for change in ({"source_sha1": "b" * 40}, {"expected_bytes": 99}):
            with self.subTest(change=change):
                member = ManagedCompoundMember
                with self.engine.begin() as connection:
                    original = connection.execute(select(member.source_sha1, member.expected_bytes).where(member.resource_key == "takeout-entry:1")).one()
                    connection.execute(update(member).where(member.resource_key == "takeout-entry:1").values(**change))
                self.assertEqual(self.refusal("takeout-live:1").outcome, "pending")
                with self.engine.begin() as connection:
                    connection.execute(update(member).where(member.resource_key == "takeout-entry:1").values(source_sha1=original[0], expected_bytes=original[1]))
                self.assertIsNone(self.refusal("takeout-live:1"))
        with self.engine.begin() as connection:
            connection.execute(update(ManagedCompound).values(hold="compound_conflict"))
        self.assertEqual(self.refusal("takeout-live:1").outcome, "held")

    def test_an_intent_whose_manifest_is_not_schema_two_keeps_the_rule_out_of_commit(self):
        with self.engine.begin() as connection:
            connection.execute(update(ManagedImportIntent).values(resource_manifest="{}"))
        self.add("takeout-entry:6", "shot")
        self.add("takeout-entry:7", "shot")
        self.assertIsNone(self.refusal("shot"))
        self.assertFalse(managed_manifest.known("{}"))
        self.assertFalse(managed_manifest.known("not json"))
        self.assertEqual((managed_manifest.resource_of(managed_manifest.TAKEOUT), managed_manifest.resource_of(managed_manifest.MEDIA_ITEM)), ("takeout_entry", "media_item"))
