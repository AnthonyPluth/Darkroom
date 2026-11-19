import hashlib
import itertools
import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path

from sqlalchemy import select

from darkroom.domain.ingest import pairing, unpack
from darkroom.storage.models import MediaItem, Person
from tests.import_world import NOW, World
from tests.takeout_fixtures import GOOGLE_PHOTOS, TakeoutBuilder, standard_export

YEAR = f"{GOOGLE_PHOTOS}/Photos from 2019"
CUT = "screen_capture_of_a_chat_with_a_long_title_AAA"
ONE, TWO = f"{CUT}_one.jpg", f"{CUT}_two.jpg"
LONG = "a_long_name_written_by_some_messaging_app_xy"
LOCALISED_EDITS = ("-edited", "-bearbeitet", "-modifié", "-edytowane", "-bewerkt", "-modificato", "-editat", "-ha editado", "-編集済み")


def unzip(manifest, target):
    for part in manifest.parts:
        with zipfile.ZipFile(part) as archive:
            archive.extractall(target)
    return target


def sha1_of(data):
    return hashlib.sha1(data, usedforsecurity=False).hexdigest()


def listed_from_disk(root):
    folders, digests = [], {}
    if os.path.islink(root):
        return folders, digests
    for folder, names, files in os.walk(root):
        names[:] = sorted(name for name in names if not name.startswith("."))
        kept = []
        for name in files:
            found = os.lstat(os.path.join(folder, name))
            if stat.S_ISREG(found.st_mode) and found.st_nlink == 1:
                kept.append(name)
                if pairing.is_media(name):
                    digests[Path(folder) / name] = sha1_of(Path(folder, name).read_bytes())
        folders.append((Path(folder), kept))
    return folders, digests


def listed_from_zips(manifest, staging, numbers=None):
    by_folder, digests = {}, {}
    for number, part in enumerate(manifest.parts):
        if numbers is not None and number not in numbers:
            continue
        with zipfile.ZipFile(part) as archive:
            for entry in archive.namelist():
                path = staging / entry
                by_folder.setdefault(path.parent, []).append(path.name)
                if pairing.is_media(path.name):
                    digests[path] = sha1_of(archive.read(entry))
    return list(by_folder.items()), digests


def zip_reader(manifest, staging):
    texts = {}
    for part in manifest.parts:
        with zipfile.ZipFile(part) as archive:
            for entry in archive.namelist():
                if entry.endswith(".json"):
                    texts[staging / entry] = archive.read(entry).decode("utf-8")
    return texts.get


def mixed_export(root, seed, parts):
    builder = TakeoutBuilder(root, seed=seed, parts=parts)
    last = parts - 1
    builder.photo("IMG_0101.JPG", part=0, sidecar_part=last)
    builder.photo("IMG_0102.HEIC", form="supplemental", part=last, sidecar_part=0)
    builder.photo("IMG_0103.JPG", form="truncated", part=1 % parts)
    builder.photo("a_very_long_original_file_name_from_a_camera_roll_0104.JPG", form="limit", part=last)
    builder.photo("IMG_0105.JPG", form=None, part=0)
    shared = builder.image_bytes(".JPG")
    builder.photo("IMG_0106.JPG", data=shared, part=0)
    builder.add("IMG_0106.JPG", shared, "Family trip", last, "json", 0, None, "")
    builder.add("IMG_0106(1).JPG", shared, "Weekend", 1 % parts, None, None, None, "", sidecar_name="IMG_0106.JPG(1).json")
    builder.numbered_duplicate("IMG_0107", ".JPG", part=1 % parts)
    builder.photo("IMG_0108.JPG", part=0)
    builder.add("IMG_0108-edited.JPG", builder.image_bytes(".JPG"), "Photos from 2019", last, None, None, None, "")
    builder.live_photo("IMG_0109", part=last)
    builder.add("IMG_0109.MOV", builder.video_bytes(), "Holiday", 0, "json", last, None, "")
    builder.photo("IMG_0110.JPG", year=2020, data=shared, part=last)
    builder.orphan_sidecar("IMG_9998.JPG", part=0)
    builder.orphan_sidecar("IMG_9997.JPG", year=2020, part=last)
    builder.non_media(part=1 % parts)
    return builder.build()


class PairingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def pair(self, staging, hashing=pairing.paths.plain_sha1):
        report = pairing.pair(staging, hashing)
        folders, digests = listed_from_disk(staging)
        self.assertEqual(pairing.pair_names(folders, digests, reading=pairing.plain_text), report)
        return report

    def staged(self, builder):
        manifest = builder.build()
        return manifest, unzip(manifest, self.root / f"staging-{builder.root.name}")

    def only(self, report, name):
        (item,) = [i for i in report.items if i.path.name == name]
        return item

    def sidecar_name(self, form, name="IMG_1.JPG"):
        builder = TakeoutBuilder(self.root / f"export-{form}")
        builder.photo(name, form=form)
        manifest, staging = self.staged(builder)
        item = self.only(self.pair(staging), name)
        return item.sidecar.name if item.sidecar else None, manifest

    def test_each_rule_pairs_its_form(self):
        for form, expected in (("json", "IMG_1.JPG.json"), ("supplemental", "IMG_1.JPG.supplemental-metadata.json"),
                               ("truncated", "IMG_1.JPG.supplemental-metada.json"), ("short", "IMG_1.JPG.supplemental-me.json"),
                               ("tiny", "IMG_1.JPG.s.json")):
            with self.subTest(form=form):
                self.assertEqual(self.sidecar_name(form)[0], expected)

    def test_no_sidecar_still_yields_an_item(self):
        found, _ = self.sidecar_name(None)
        self.assertIsNone(found)

    def test_base_name_truncation_keeps_the_truncated_form(self):
        long_name = "a_very_long_original_file_name_from_a_camera_roll_0007.JPG"
        found, _ = self.sidecar_name("limit", long_name)
        self.assertEqual(found, f"{long_name[:46]}.json")

    def shared_cut(self, label, tails=("_one.jpg", "_two.jpg"), titles=None, prefix=CUT):
        builder = TakeoutBuilder(self.root / label)
        builder.shared_cut(prefix, tails, titles)
        return self.staged(builder)[1]

    def sidecars_of(self, report):
        return {item.path.name: item.sidecar.name if item.sidecar else None for item in report.items}

    def test_two_long_names_with_one_cut_name_each_get_the_sidecar_titled_with_their_name(self):
        staging = self.shared_cut("cut", titles=(TWO, ONE))
        before = sorted((str(p), p.stat().st_mtime_ns) for p in staging.rglob("*"))
        report = self.pair(staging)
        self.assertEqual(self.sidecars_of(report), {ONE: f"{CUT}(1).json", TWO: f"{CUT}.json"})
        self.assertEqual((report.orphan_sidecars, report.unpaired_media), ([], []))
        self.assertEqual(sorted((str(p), p.stat().st_mtime_ns) for p in staging.rglob("*")), before)

    def test_two_long_names_whose_titles_cannot_tell_them_apart_get_no_sidecar(self):
        for label, titles in (("missing", (None, None)), ("equal", (ONE, ONE)), ("neither", ("other.jpg", "another.jpg"))):
            with self.subTest(titles=label):
                report = self.pair(self.shared_cut(f"vague-{label}", titles=titles))
                self.assertEqual(self.sidecars_of(report), {ONE: None, TWO: None})
                self.assertEqual(sorted(p.name for p in report.orphan_sidecars), [f"{CUT}(1).json", f"{CUT}.json"])
                self.assertEqual(sorted(i.path.name for i in report.unpaired_media), [ONE, TWO])

    def test_only_the_long_name_a_title_names_gets_a_sidecar(self):
        report = self.pair(self.shared_cut("half", titles=(ONE, None)))
        self.assertEqual(self.sidecars_of(report), {ONE: f"{CUT}.json", TWO: None})
        self.assertEqual([p.name for p in report.orphan_sidecars], [f"{CUT}(1).json"])

    def test_a_title_matches_ignoring_case_and_unicode_composition(self):
        prefix = "cafe\u0301_receipt_photographed_on_a_long_trip_AAAA"
        one, two = f"{prefix}_one.JPG", f"{prefix}_two.jpg"
        composed = "caf\u00e9_receipt_photographed_on_a_long_trip_AAAA"
        report = self.pair(self.shared_cut("unicode", ("_one.JPG", "_two.jpg"), (f"{composed}_TWO.JPG", f"{composed}_one.jpg".upper()), prefix=prefix))
        cut = one[:pairing.SIDECAR_NAME_LIMIT]
        self.assertEqual(self.sidecars_of(report), {one: f"{cut}(1).json", two: f"{cut}.json"})
        self.assertEqual(report.orphan_sidecars, [])

    def test_a_title_that_names_both_long_names_by_case_alone_pairs_neither(self):
        folder = self.root / "case"
        folder.mkdir()
        lower, upper = f"{CUT}_x.jpg", f"{CUT}_X.jpg"
        (folder / lower).write_bytes(b"lower")
        (folder / upper).write_bytes(b"upper")
        (folder / f"{CUT}.json").write_text(f'{{"title": "{lower}"}}')
        self.assertEqual(self.sidecars_of(self.pair(folder)), {lower: None, upper: None})

    def test_a_long_edited_copy_still_shares_its_originals_sidecar(self):
        folder = self.root / "long-edit"
        folder.mkdir()
        edit = f"{CUT}_one-edited.jpg"
        (folder / ONE).write_bytes(b"original")
        (folder / edit).write_bytes(b"edit")
        (folder / f"{CUT}.json").write_text(f'{{"title": "{ONE}"}}')
        report = self.pair(folder)
        self.assertEqual(self.sidecars_of(report), {ONE: f"{CUT}.json", edit: f"{CUT}.json"})
        self.assertEqual(self.only(report, edit).edited_of, folder / ONE)

    def test_a_long_edited_copy_follows_its_original_when_two_long_names_share_a_cut(self):
        folder = self.root / "long-edits"
        folder.mkdir()
        edit = f"{CUT}_two-edited.jpg"
        for name in (ONE, TWO, edit):
            (folder / name).write_bytes(name.encode())
        (folder / f"{CUT}.json").write_text(f'{{"title": "{ONE}"}}')
        (folder / f"{CUT}(1).json").write_text(f'{{"title": "{TWO}"}}')
        self.assertEqual(self.sidecars_of(self.pair(folder)), {ONE: f"{CUT}.json", TWO: f"{CUT}(1).json", edit: f"{CUT}(1).json"})

    def test_long_names_settle_from_sidecars_with_a_byte_order_mark(self):
        folder = self.root / "marked"
        folder.mkdir()
        (folder / ONE).write_bytes(b"one")
        (folder / TWO).write_bytes(b"two")
        (folder / f"{CUT}.json").write_bytes(f'{{"title": "{TWO}"}}'.encode("utf-16"))
        (folder / f"{CUT}(1).json").write_bytes(b"\xef\xbb\xbf" + f'{{"title": "{ONE}"}}'.encode())
        self.assertEqual(self.sidecars_of(self.pair(folder)), {ONE: f"{CUT}(1).json", TWO: f"{CUT}.json"})

    def test_an_unreadable_sidecar_leaves_the_long_names_unpaired(self):
        staging = self.shared_cut("unreadable")

        def refusing(path):
            raise pairing.paths.LinkRefused("not a plain file")

        report = pairing.pair(staging, reading=refusing)
        self.assertEqual(self.sidecars_of(report), {ONE: None, TWO: None})
        self.assertEqual(len(report.orphan_sidecars), 2)

    def test_the_counter_moves_before_the_extension_on_the_sidecar(self):
        builder = TakeoutBuilder(self.root / "dup")
        builder.numbered_duplicate("IMG_0001", ".JPG")
        _, staging = self.staged(builder)
        report = self.pair(staging)
        self.assertEqual(self.only(report, "IMG_0001(1).JPG").sidecar.name, "IMG_0001.JPG(1).json")
        self.assertEqual(self.only(report, "IMG_0001.JPG").sidecar.name, "IMG_0001.JPG.json")

    def test_a_counter_media_does_not_take_the_uncountered_sidecar(self):
        folder = self.root / "plain"
        folder.mkdir()
        (folder / "IMG_0001(1).JPG").write_bytes(b"x")
        (folder / "IMG_0001.JPG.json").write_text("{}")
        report = self.pair(folder)
        self.assertIsNone(self.only(report, "IMG_0001(1).JPG").sidecar)
        self.assertEqual([p.name for p in report.orphan_sidecars], ["IMG_0001.JPG.json"])

    def test_the_counter_after_the_supplemental_part_also_pairs(self):
        folder = self.root / "modern"
        folder.mkdir()
        (folder / "IMG_0001(1).JPG").write_bytes(b"x")
        (folder / "IMG_0001.JPG.supplemental-metadata(1).json").write_text("{}")
        self.assertEqual(self.only(self.pair(folder), "IMG_0001(1).JPG").sidecar.name, "IMG_0001.JPG.supplemental-metadata(1).json")

    def test_an_edited_copy_shares_its_originals_sidecar_and_is_flagged(self):
        builder = TakeoutBuilder(self.root / "edit")
        builder.edited("IMG_0009", ".JPG")
        _, staging = self.staged(builder)
        report = self.pair(staging)
        original, edit = self.only(report, "IMG_0009.JPG"), self.only(report, "IMG_0009-edited.JPG")
        self.assertEqual(edit.sidecar, original.sidecar)
        self.assertEqual(edit.edited_of, original.path)
        self.assertIsNone(original.edited_of)

    def test_an_edited_copy_without_its_original_still_pairs_by_name_but_has_no_edited_of(self):
        folder = self.root / "lone"
        folder.mkdir()
        (folder / "IMG_1-edited.JPG").write_bytes(b"x")
        (folder / "IMG_1.JPG.json").write_text("{}")
        item = self.only(self.pair(folder), "IMG_1-edited.JPG")
        self.assertEqual(item.sidecar.name, "IMG_1.JPG.json")
        self.assertIsNone(item.edited_of)

    def test_a_media_file_without_the_extension_it_was_uploaded_with_pairs_by_its_name(self):
        for number, tail in enumerate((".json", ".supplemental-metadata.json", ".supplemental-metada.json", ".s.json")):
            with self.subTest(tail=tail):
                builder = TakeoutBuilder(self.root / f"bare-{number}")
                builder.without_extension("scan20030616", ".jpg", tail)
                _, staging = self.staged(builder)
                report = self.pair(staging)
                self.assertEqual(self.sidecars_of(report), {"scan20030616.jpg": f"scan20030616{tail}"})
                self.assertEqual((report.orphan_sidecars, report.unpaired_media), ([], []))

    def test_two_media_with_one_stem_do_not_claim_the_sidecar_without_an_extension(self):
        folder = self.root / "clip"
        folder.mkdir()
        (folder / "clip.HEIC").write_bytes(b"still")
        (folder / "clip.MP4").write_bytes(b"video")
        (folder / "clip.json").write_text("{}")
        report = self.pair(folder)
        self.assertEqual(self.sidecars_of(report), {"clip.HEIC": None, "clip.MP4": None})
        self.assertEqual([p.name for p in report.orphan_sidecars], ["clip.json"])

    def test_the_sidecar_without_an_extension_does_not_replace_a_rule_that_already_paired(self):
        folder = self.root / "both"
        folder.mkdir()
        (folder / "scan.jpg").write_bytes(b"x")
        (folder / "scan.jpg.json").write_text("{}")
        (folder / "scan.json").write_text("{}")
        report = self.pair(folder)
        self.assertEqual(self.sidecars_of(report), {"scan.jpg": "scan.jpg.json"})
        self.assertEqual([p.name for p in report.orphan_sidecars], ["scan.json"])

    def test_an_edited_copy_in_another_language_shares_its_originals_sidecar_and_is_flagged(self):
        for number, suffix in enumerate(LOCALISED_EDITS):
            with self.subTest(suffix=suffix):
                builder = TakeoutBuilder(self.root / f"edit-{number}")
                builder.edited("garden", ".jpg", suffix=suffix)
                _, staging = self.staged(builder)
                report = self.pair(staging)
                original, edit = self.only(report, "garden.jpg"), self.only(report, f"garden{suffix}.jpg")
                self.assertEqual(original.sidecar.name, "garden.jpg.json")
                self.assertEqual(edit.sidecar, original.sidecar)
                self.assertEqual(edit.edited_of, original.path)
                self.assertEqual(pairing.original_name(edit.path), "garden.jpg")
                self.assertEqual((report.orphan_sidecars, report.unpaired_media), ([], []))

    def test_an_edited_suffix_matches_ignoring_case_and_unicode_composition(self):
        folder = self.root / "composed"
        folder.mkdir()
        names = ("garden-BEARBEITET.jpg", "garden-modifié.jpg", "garden-Modifié.jpg")
        (folder / "garden.jpg").write_bytes(b"original")
        (folder / "garden.jpg.json").write_text("{}")
        for number, name in enumerate(names):
            (folder / name).write_bytes(f"edit {number}".encode())
        report = self.pair(folder)
        for name in names:
            with self.subTest(name=name):
                item = self.only(report, name)
                self.assertEqual((item.sidecar.name, item.edited_of), ("garden.jpg.json", folder / "garden.jpg"))

    def test_a_suffix_that_is_not_an_edited_one_is_not_an_edited_copy(self):
        folder = self.root / "near"
        folder.mkdir()
        names = ("garden-edit.jpg", "garden-editor.jpg", "garden-edited-by-me.jpg")
        (folder / "garden.jpg").write_bytes(b"original")
        (folder / "garden.jpg.json").write_text("{}")
        for number, name in enumerate(names):
            (folder / name).write_bytes(f"other {number}".encode())
        report = self.pair(folder)
        for name in names:
            with self.subTest(name=name):
                item = self.only(report, name)
                self.assertEqual((item.sidecar, item.edited_of, pairing.original_name(item.path)), (None, None, None))

    def test_a_creation_uses_its_originals_sidecar_but_is_not_an_edited_copy(self):
        for number, suffix in enumerate(("-effects", "-smile", "-mix")):
            with self.subTest(suffix=suffix):
                builder = TakeoutBuilder(self.root / f"creation-{number}")
                builder.creation("garden", ".jpg", suffix)
                _, staging = self.staged(builder)
                report = self.pair(staging)
                original, made = self.only(report, "garden.jpg"), self.only(report, f"garden{suffix}.jpg")
                self.assertEqual(made.sidecar, original.sidecar)
                self.assertEqual(made.sidecar.name, "garden.jpg.json")
                self.assertIsNone(made.edited_of)
                self.assertIsNone(pairing.original_name(made.path))
                self.assertEqual(len(report.items), 2)
                self.assertEqual((report.orphan_sidecars, report.unpaired_media), ([], []))

    def test_a_creation_with_a_sidecar_of_its_own_keeps_it(self):
        folder = self.root / "own"
        folder.mkdir()
        for name in ("garden.jpg", "garden-smile.jpg"):
            (folder / name).write_bytes(name.encode())
            (folder / f"{name}.json").write_text("{}")
        report = self.pair(folder)
        self.assertEqual(self.sidecars_of(report), {"garden.jpg": "garden.jpg.json", "garden-smile.jpg": "garden-smile.jpg.json"})

    def test_an_edited_copy_with_a_counter_uses_the_original_without_the_counter(self):
        builder = TakeoutBuilder(self.root / "counted")
        builder.edited_with_counter("garden", ".jpg")
        _, staging = self.staged(builder)
        report = self.pair(staging)
        original, edit = self.only(report, "garden.jpg"), self.only(report, "garden-edited(1).jpg")
        self.assertEqual((edit.sidecar.name, edit.sidecar), ("garden.jpg.json", original.sidecar))
        self.assertEqual(edit.edited_of, original.path)
        self.assertEqual((report.orphan_sidecars, report.unpaired_media), ([], []))

    def test_an_edited_copy_with_a_counter_in_another_language_uses_the_original_without_the_counter(self):
        folder = self.root / "counted-german"
        folder.mkdir()
        (folder / "garden.jpg").write_bytes(b"original")
        (folder / "garden.jpg.json").write_text("{}")
        (folder / "garden-bearbeitet(2).jpg").write_bytes(b"edit")
        item = self.only(self.pair(folder), "garden-bearbeitet(2).jpg")
        self.assertEqual((item.sidecar.name, item.edited_of), ("garden.jpg.json", folder / "garden.jpg"))

    def test_an_edited_copy_with_a_counter_still_takes_the_numbered_originals_sidecar(self):
        builder = TakeoutBuilder(self.root / "numbered")
        builder.numbered_duplicate("garden", ".jpg")
        builder.add("garden-edited(1).jpg", builder.image_bytes(".jpg"), "Photos from 2019", 0, None, None, None, "")
        _, staging = self.staged(builder)
        report = self.pair(staging)
        edit = self.only(report, "garden-edited(1).jpg")
        self.assertEqual(edit.sidecar.name, "garden.jpg(1).json")
        self.assertEqual(edit.edited_of, self.only(report, "garden(1).jpg").path)
        self.assertEqual(self.only(report, "garden.jpg").sidecar.name, "garden.jpg.json")

    def test_an_edited_copy_with_a_counter_does_not_pass_over_a_numbered_original_without_a_sidecar(self):
        folder = self.root / "numbered-bare"
        folder.mkdir()
        for name in ("garden.jpg", "garden(1).jpg", "garden-edited(1).jpg"):
            (folder / name).write_bytes(name.encode())
        (folder / "garden.jpg.json").write_text("{}")
        report = self.pair(folder)
        self.assertEqual(self.sidecars_of(report), {"garden.jpg": "garden.jpg.json", "garden(1).jpg": None, "garden-edited(1).jpg": None})
        self.assertEqual(self.only(report, "garden-edited(1).jpg").edited_of, folder / "garden(1).jpg")

    def test_a_long_name_with_a_counter_pairs_with_a_sidecar_cut_before_the_counter(self):
        for number, (counter, cut) in enumerate(((1, 46), (1, 43), (12, 46), (12, 42))):
            with self.subTest(counter=counter, cut=cut):
                builder = TakeoutBuilder(self.root / f"cut-{number}")
                item = builder.cut_with_counter(LONG, ".jpg", counter, cut)
                _, staging = self.staged(builder)
                report = self.pair(staging)
                self.assertEqual(self.sidecars_of(report), {item.media_name: item.sidecar_name})
                self.assertEqual((report.orphan_sidecars, report.unpaired_media), ([], []))

    def test_a_long_name_with_a_counter_does_not_pair_with_a_sidecar_cut_at_another_length(self):
        for number, cut in enumerate((40, 42, 45, 47)):
            with self.subTest(cut=cut):
                builder = TakeoutBuilder(self.root / f"wrong-{number}")
                item = builder.cut_with_counter(LONG, ".jpg", 1, cut)
                _, staging = self.staged(builder)
                report = self.pair(staging)
                self.assertEqual(self.sidecars_of(report), {item.media_name: None})
                self.assertEqual(len(report.orphan_sidecars), 1)

    def test_a_long_name_with_a_counter_does_not_pair_with_another_counters_sidecar(self):
        folder = self.root / "other-counter"
        folder.mkdir()
        (folder / f"{LONG}(1).jpg").write_bytes(b"x")
        (folder / f"{LONG}.j(2).json").write_text("{}")
        report = self.pair(folder)
        self.assertEqual(self.sidecars_of(report), {f"{LONG}(1).jpg": None})
        self.assertEqual(len(report.orphan_sidecars), 1)

    def test_two_long_names_with_one_cut_and_counter_do_not_claim_the_sidecar(self):
        builder = TakeoutBuilder(self.root / "cut-shared")
        builder.cut_with_counter(LONG, ".jpg", 1)
        builder.add(f"{LONG}(1).jpeg", builder.image_bytes(".jpg"), "Photos from 2019", 0, None, None, None, "")
        _, staging = self.staged(builder)
        report = self.pair(staging)
        self.assertEqual(self.sidecars_of(report), {f"{LONG}(1).jpg": None, f"{LONG}(1).jpeg": None})
        self.assertEqual(len(report.orphan_sidecars), 1)

    def test_a_short_name_with_a_counter_does_not_pair_with_a_sidecar_cut_at_the_limit(self):
        folder = self.root / "short-counter"
        folder.mkdir()
        (folder / "IMG_1(1).jpg").write_bytes(b"x")
        (folder / "IMG_1.j(1).json").write_text("{}")
        report = self.pair(folder)
        self.assertEqual(self.sidecars_of(report), {"IMG_1(1).jpg": None})
        self.assertEqual(len(report.orphan_sidecars), 1)

    def test_a_live_photo_video_pairs_with_its_still_and_keeps_its_own_sidecar(self):
        builder = TakeoutBuilder(self.root / "live")
        builder.live_photo("IMG_0010")
        builder.live_photo("IMG_0011", still_extension=".JPG", video_extension=".MP4", video_sidecar=False)
        _, staging = self.staged(builder)
        report = self.pair(staging)
        video = self.only(report, "IMG_0010.MOV")
        self.assertEqual(video.live_pair_of, self.only(report, "IMG_0010.HEIC").path)
        self.assertEqual(video.sidecar.name, "IMG_0010.MOV.json")
        quiet = self.only(report, "IMG_0011.MP4")
        self.assertEqual(quiet.live_pair_of, self.only(report, "IMG_0011.JPG").path)
        self.assertIsNone(quiet.sidecar)
        self.assertIsNone(self.only(report, "IMG_0010.HEIC").live_pair_of)

    def test_a_video_without_a_matching_still_is_not_a_live_photo(self):
        builder = TakeoutBuilder(self.root / "solo")
        builder.add("clip.MP4", builder.video_bytes(), "Photos from 2019", 0, "json", None, None, "")
        _, staging = self.staged(builder)
        self.assertIsNone(self.only(self.pair(staging), "clip.MP4").live_pair_of)

    def test_year_and_album_copies_collapse_into_one_item(self):
        builder = TakeoutBuilder(self.root / "album")
        builder.photo("IMG_0012.JPG", album="Family trip")
        builder.photo("IMG_0013.JPG", album="Family trip", year=2020)
        _, staging = self.staged(builder)
        report = self.pair(staging)
        item = self.only(report, "IMG_0012.JPG")
        self.assertEqual(len(report.items), 2)
        self.assertEqual(item.albums, ["Family trip"])
        self.assertEqual(len(item.copies), 2)
        self.assertEqual(item.path.parent.name, "Photos from 2019")
        self.assertTrue(item.in_year_folder)
        self.assertEqual(item.sidecar.name, "IMG_0012.JPG.json")

    def test_identical_bytes_collapse_across_folders_even_when_takeout_renames_one(self):
        folder = self.root / "renamed"
        (folder / "Photos from 2019").mkdir(parents=True)
        (folder / "Family").mkdir()
        (folder / "Photos from 2019" / "IMG_1.JPG").write_bytes(b"same")
        (folder / "Family" / "IMG_1(1).JPG").write_bytes(b"same")
        (folder / "Family" / "IMG_1.JPG(1).json").write_text("{}")
        report = self.pair(folder)
        (item,) = report.items
        self.assertEqual((item.albums, item.in_year_folder, len(item.copies)), (["Family"], True, 2))
        self.assertEqual(item.sidecar.name, "IMG_1.JPG(1).json")

    def test_identical_bytes_in_one_folder_stay_two_items(self):
        folder = self.root / "twins"
        folder.mkdir()
        (folder / "a.JPG").write_bytes(b"same")
        (folder / "b.JPG").write_bytes(b"same")
        self.assertEqual(len(self.pair(folder).items), 2)

    def test_a_video_with_two_possible_stills_is_left_unpaired(self):
        folder = self.root / "ambiguous"
        folder.mkdir()
        for name, data in (("IMG_1.HEIC", b"a"), ("IMG_1.JPG", b"b"), ("IMG_1.MOV", b"c")):
            (folder / name).write_bytes(data)
        self.assertIsNone(self.only(self.pair(folder), "IMG_1.MOV").live_pair_of)

    def test_an_album_only_item_has_no_year_copy(self):
        folder = self.root / "only"
        (folder / "Holiday").mkdir(parents=True)
        (folder / "Holiday" / "a.JPG").write_bytes(b"x")
        item = self.only(self.pair(folder), "a.JPG")
        self.assertEqual((item.albums, item.in_year_folder), (["Holiday"], False))

    def test_different_bytes_with_the_same_name_stay_separate(self):
        folder = self.root / "same"
        for year, data in (("2019", b"one"), ("2020", b"two")):
            (folder / f"Photos from {year}").mkdir(parents=True)
            (folder / f"Photos from {year}" / "IMG_1.JPG").write_bytes(data)
        self.assertEqual(len(self.pair(folder).items), 2)

    def test_standard_export_matches_the_manifest(self):
        manifest = standard_export(self.root / "std")
        staging = unzip(manifest, self.root / "stage")
        report = self.pair(staging)
        for media_path, expected in manifest.pairings.items():
            item = self.only(report, Path(media_path).name)
            if Path(media_path).name.endswith("-edited.JPG"):
                continue
            if expected is None:
                self.assertIsNone(item.sidecar, media_path)
            else:
                self.assertEqual(item.sidecar.name, Path(expected).name, media_path)
        self.assertEqual(sorted(str(p.relative_to(staging)) for p in report.orphan_sidecars), sorted(manifest.orphan_sidecars))
        self.assertEqual(len(report.items), len({Path(m).name for m in manifest.media}))
        unpaired = {i.path.name for i in report.unpaired_media}
        self.assertEqual(unpaired, {"IMG_0006.JPG", "IMG_0011.MP4"})

    def test_non_media_files_are_neither_items_nor_orphans(self):
        manifest = standard_export(self.root / "std2")
        report = self.pair(unzip(manifest, self.root / "stage2"))
        names = {i.path.name for i in report.items} | {p.name for p in report.orphan_sidecars}
        for stray in manifest.non_media:
            self.assertNotIn(Path(stray).name, names)

    def test_nothing_is_written(self):
        manifest = standard_export(self.root / "std3")
        staging = unzip(manifest, self.root / "stage3")
        before = sorted((str(p), p.stat().st_mtime_ns) for p in staging.rglob("*"))
        self.pair(staging)
        self.assertEqual(sorted((str(p), p.stat().st_mtime_ns) for p in staging.rglob("*")), before)

    def test_a_missing_folder_gives_an_empty_report(self):
        report = self.pair(self.root / "nowhere")
        self.assertEqual((report.items, report.orphan_sidecars, report.unpaired_media), ([], [], []))

    def test_links_in_the_staging_folder_are_left_out(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.jpg").write_bytes(b"not part of the export")
        (outside / "secret.jpg.json").write_text("{}")
        staging = self.root / "stage-links" / YEAR
        staging.mkdir(parents=True)
        (staging / "IMG_1.JPG").write_bytes(b"a staged photo")
        (staging / "IMG_2.JPG").symlink_to(outside / "secret.jpg")
        (staging / "IMG_1.JPG.json").symlink_to(outside / "secret.jpg.json")
        (staging / "hop").symlink_to(outside, target_is_directory=True)
        report = self.pair(self.root / "stage-links")
        self.assertEqual([item.path.name for item in report.items], ["IMG_1.JPG"])
        self.assertIsNone(report.items[0].sidecar)
        self.assertEqual(report.orphan_sidecars, [])
        (self.root / "linked-stage").symlink_to(self.root / "stage-links", target_is_directory=True)
        self.assertEqual(self.pair(self.root / "linked-stage").items, [])

    def test_a_hard_linked_file_in_the_staging_folder_is_left_out(self):
        outside = self.root / "outside-hard"
        outside.mkdir()
        (outside / "secret.jpg").write_bytes(b"not part of the export")
        staging = self.root / "stage-hard" / YEAR
        staging.mkdir(parents=True)
        (staging / "IMG_1.JPG").write_bytes(b"a staged photo")
        os.link(outside / "secret.jpg", staging / "IMG_2.JPG")
        report = self.pair(self.root / "stage-hard")
        self.assertEqual([item.path.name for item in report.items], ["IMG_1.JPG"])

    def test_a_file_that_becomes_a_link_before_it_is_hashed_is_left_out(self):
        staging = self.root / "stage-swap" / YEAR
        staging.mkdir(parents=True)
        (staging / "IMG_1.JPG").write_bytes(b"one")
        (staging / "IMG_2.JPG").write_bytes(b"two")

        def hashing(path):
            if path.name == "IMG_2.JPG":
                path.unlink()
                path.symlink_to(staging / "IMG_1.JPG")
            return pairing.paths.plain_sha1(path)

        report = pairing.pair(self.root / "stage-swap", hashing)
        self.assertEqual([item.path.name for item in report.items], ["IMG_1.JPG"])
        self.assertEqual(report.not_imported, {".jpg": 1})


class KindsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def pair(self, builder):
        staging = unzip(builder.build(), self.root / f"stage-{builder.root.name}")
        report = pairing.pair(staging)
        folders, digests = listed_from_disk(staging)
        self.assertEqual(pairing.pair_names(folders, digests), report)
        return report

    def test_raw_avif_mts_and_a_pixel_motion_clip_pair_with_their_sidecars(self):
        builder = TakeoutBuilder(self.root / "kinds", parts=2)
        builder.photo("shot.CR2")
        builder.photo("pic.avif", part=1)
        builder.video("cam.MTS")
        builder.video("PXL_20200101_101010101.MP", part=1)
        report = self.pair(builder)
        self.assertEqual(sorted((item.path.name, item.sidecar.name) for item in report.items),
                         [("PXL_20200101_101010101.MP", "PXL_20200101_101010101.MP.json"), ("cam.MTS", "cam.MTS.json"),
                          ("pic.avif", "pic.avif.json"), ("shot.CR2", "shot.CR2.json")])
        self.assertEqual((report.orphan_sidecars, report.unpaired_media, report.not_imported), ([], [], {}))

    def test_every_listed_kind_is_media(self):
        for name in ("a.CR2", "a.cr3", "a.NEF", "a.nrw", "a.ARW", "a.srf", "a.sr2", "a.RAF", "a.orf", "a.RW2", "a.pef", "a.srw", "a.raw",
                     "a.avif", "a.jxl", "a.MTS", "a.m2ts", "a.mpg", "a.mpeg", "a.wmv", "a.3g2", "a.MP", "a.MV"):
            with self.subTest(name=name):
                self.assertTrue(pairing.is_media(name))

    def test_files_that_are_not_imported_are_counted_by_kind_without_names(self):
        builder = TakeoutBuilder(self.root / "left-out")
        builder.photo("IMG_1.JPG")
        builder.loose_file("notes.txt")
        builder.loose_file("more-notes.TXT")
        builder.loose_file("IMG_NOEXT")
        builder.loose_file("IMG_NOEXT.json", b"{}")
        builder.loose_file("odd.private-kind-canary")
        builder.loose_file("trailing.")
        builder.non_media()
        report = self.pair(builder)
        self.assertEqual([item.path.name for item in report.items], ["IMG_1.JPG"])
        self.assertEqual(report.not_imported, {".txt": 2, "none": 2, "other": 1})
        self.assertEqual([path.name for path in report.orphan_sidecars], ["IMG_NOEXT.json"])

    def test_a_media_name_without_a_digest_is_counted_by_its_kind(self):
        folder = self.root / "f"
        report = pairing.pair_names([(folder, ["a.JPG", "b.CR2", "c.heic"])], {folder / "a.JPG": "1"})
        self.assertEqual(report.not_imported, {".cr2": 1, ".heic": 1})

    def test_the_labels_are_a_fixed_vocabulary(self):
        for name, label in (("x.PDF", ".pdf"), ("x.html", ".html"), ("x", "none"), (".hidden", "none"), ("x.", "none"), ("x.jo-example", "other"),
                            ("x.jpg", ".jpg"), ("x.tar.gz", "other")):
            with self.subTest(name=name):
                self.assertEqual(pairing.left_out_kind(name), label)


class PairingNamesTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def exports(self):
        for parts in (1, 2, 3):
            yield f"standard-{parts}", standard_export(self.root / f"standard-{parts}", seed=parts, parts=parts)
        for parts, seed in ((2, 7), (3, 8), (4, 9), (5, 10)):
            yield f"mixed-{parts}", mixed_export(self.root / f"mixed-{parts}", seed, parts)

    def test_names_from_the_zips_give_exactly_the_walkers_report(self):
        for label, manifest in self.exports():
            with self.subTest(export=label):
                staging = unzip(manifest, self.root / f"stage-{label}")
                folders, digests = listed_from_zips(manifest, staging)
                expected = pairing.pair(staging)
                self.assertGreater(len(expected.items), 10)
                self.assertTrue(expected.orphan_sidecars)
                self.assertEqual(pairing.pair_names(folders, digests), expected)

    def test_names_from_the_zips_give_the_walkers_report_for_the_forms_the_first_rules_missed(self):
        builder = TakeoutBuilder(self.root / "missed", seed=21, parts=3)
        builder.without_extension("scan20030616", ".jpg", part=0).sidecar_part = 2
        builder.edited("garden", ".jpg", suffix="-bearbeitet", part=1)
        builder.creation("pond", ".jpg", "-smile", part=2)
        builder.edited_with_counter("lake", ".jpg", part=0)
        builder.cut_with_counter(LONG, ".jpg", 1, part=1).sidecar_part = 0
        manifest = builder.build()
        staging = unzip(manifest, self.root / "stage-missed")
        report = pairing.pair(staging)
        folders, digests = listed_from_zips(manifest, staging)
        self.assertEqual(pairing.pair_names(folders, digests, reading=zip_reader(manifest, staging)), report)
        self.assertEqual((report.orphan_sidecars, [i.path.name for i in report.unpaired_media]), ([], []))
        self.assertEqual(sorted(i.path.name for i in report.items if i.edited_of is not None), ["garden-bearbeitet.jpg", "lake-edited(1).jpg"])

    def test_the_order_folders_come_in_does_not_change_the_report(self):
        manifest = mixed_export(self.root / "order", 11, 3)
        staging = unzip(manifest, self.root / "stage-order")
        folders, digests = listed_from_zips(manifest, staging)
        expected = pairing.pair(staging)
        for order in itertools.islice(itertools.permutations(folders), 24):
            shuffled = [(folder, list(reversed(names))) for folder, names in order]
            self.assertEqual(pairing.pair_names(shuffled, digests), expected)

    def test_names_and_a_zip_reader_give_the_walkers_report_for_long_names_sharing_a_cut(self):
        for parts in (1, 2, 3):
            with self.subTest(parts=parts):
                builder = TakeoutBuilder(self.root / f"cut-{parts}", seed=parts, parts=parts)
                builder.shared_cut(CUT, ("_one.jpg", "_two.jpg"), (TWO, ONE), part=0, sidecar_part=parts - 1)
                builder.shared_cut("another_screen_capture_from_a_long_chat_title_B", ("_a.png", "_b.png", "_c.png"), (None, None, None))
                manifest = builder.build()
                staging = unzip(manifest, self.root / f"stage-cut-{parts}")
                folders, digests = listed_from_zips(manifest, staging)
                expected = pairing.pair(staging)
                (one,) = [item for item in expected.items if item.path.name == ONE]
                self.assertEqual(one.sidecar.name, f"{CUT}(1).json")
                self.assertEqual(len(expected.orphan_sidecars), 3)
                self.assertEqual(pairing.pair_names(folders, digests, reading=zip_reader(manifest, staging)), expected)

    def test_without_a_reader_long_names_sharing_a_cut_get_no_sidecar(self):
        folder = self.root / YEAR
        report = pairing.pair_names([(folder, [ONE, TWO, f"{CUT}.json", f"{CUT}(1).json"])], {folder / ONE: "1", folder / TWO: "2"})
        self.assertEqual([item.sidecar for item in report.items], [None, None])
        self.assertEqual(report.orphan_sidecars, [folder / f"{CUT}(1).json", folder / f"{CUT}.json"])

    def test_long_names_sharing_a_cut_settle_from_earlier_sidecars(self):
        folder = self.root / YEAR
        texts = {folder / f"{CUT}.json": f'{{"title": "{TWO}"}}', folder / f"{CUT}(1).json": f'{{"title": "{ONE}"}}'}
        report = pairing.pair_names([(folder, [ONE, TWO])], {folder / ONE: "1", folder / TWO: "2"},
                                    {folder: [f"{CUT}.json", f"{CUT}(1).json"]}, reading=texts.get)
        self.assertEqual({i.path.name: i.sidecar.name for i in report.items}, {ONE: f"{CUT}(1).json", TWO: f"{CUT}.json"})

    def test_each_sidecar_of_many_long_names_sharing_a_cut_is_read_once(self):
        for count in (3, 200):
            with self.subTest(count=count):
                folder = self.root / f"many-{count}" / YEAR
                names = [f"{CUT}_{number:04d}.jpg" for number in range(count)]
                sidecars = [f"{CUT}.json"] + [f"{CUT}({number}).json" for number in range(1, count)]
                texts = {folder / sidecar: f'{{"title": "{name}"}}' for sidecar, name in zip(sidecars, reversed(names), strict=True)}
                reads = []

                def counting(path, texts=texts, reads=reads):
                    reads.append(path)
                    return texts.get(path)

                report = pairing.pair_names([(folder, names + sidecars)], {folder / name: str(n) for n, name in enumerate(names)}, reading=counting)
                self.assertEqual(sorted(reads), sorted(texts))
                expected = {name: sidecar for sidecar, name in zip(sidecars, reversed(names), strict=True)}
                self.assertEqual({item.path.name: item.sidecar.name for item in report.items}, expected)
                self.assertEqual(report.orphan_sidecars, [])

    def test_a_media_name_without_a_digest_is_left_out(self):
        folder = self.root / "f"
        report = pairing.pair_names([(folder, ["a.JPG", "b.JPG", "b.JPG.json"])], {folder / "a.JPG": "1"})
        self.assertEqual([item.path.name for item in report.items], ["a.JPG"])
        self.assertEqual(report.orphan_sidecars, [folder / "b.JPG.json"])

    def test_a_sidecar_from_an_earlier_part_pairs_and_is_not_an_orphan(self):
        folder = self.root / YEAR
        report = pairing.pair_names([(folder, ["IMG_1.JPG", "IMG_1-edited.JPG", "IMG_2.JPG", "IMG_2.JPG.json"])],
                                    {folder / "IMG_1.JPG": "1", folder / "IMG_1-edited.JPG": "2", folder / "IMG_2.JPG": "3"},
                                    {folder: ["IMG_1.JPG.supplemental-metadata.json", "IMG_2.JPG.supplemental-metadata.json", "IMG_3.JPG.json"]})
        sidecars = {item.path.name: item.sidecar for item in report.items}
        self.assertEqual(sidecars["IMG_1.JPG"], folder / "IMG_1.JPG.supplemental-metadata.json")
        self.assertEqual(sidecars["IMG_1-edited.JPG"], folder / "IMG_1.JPG.supplemental-metadata.json")
        self.assertEqual(sidecars["IMG_2.JPG"], folder / "IMG_2.JPG.json")
        self.assertEqual(report.orphan_sidecars, [])
        self.assertEqual(report.unpaired_media, [])

    def test_an_earlier_sidecar_in_another_folder_does_not_pair(self):
        folder, other = self.root / YEAR, self.root / f"{GOOGLE_PHOTOS}/Family"
        report = pairing.pair_names([(folder, ["IMG_1.JPG"])], {folder / "IMG_1.JPG": "1"}, {other: ["IMG_1.JPG.json"]})
        self.assertIsNone(report.items[0].sidecar)

    def test_a_later_part_paired_with_earlier_sidecars_finds_the_sidecar_the_whole_export_would(self):
        manifest = mixed_export(self.root / "split", 12, 3)
        staging = unzip(manifest, self.root / "stage-split")
        whole = {item.path: item.sidecar for item in pairing.pair(staging).items if len(item.copies) == 1}
        earlier_folders, _ = listed_from_zips(manifest, staging, {0, 1})
        earlier = dict(earlier_folders)
        folders, digests = listed_from_zips(manifest, staging, {2})
        report = pairing.pair_names(folders, digests, earlier)
        compared = [item for item in report.items if item.path in whole]
        self.assertTrue(any(item.sidecar is not None and item.sidecar.name in earlier.get(item.path.parent, []) for item in compared))
        for item in compared:
            self.assertEqual(item.sidecar, whole[item.path], item.path.name)


class ImportedLongNamesTests(unittest.TestCase):
    def test_an_import_gives_each_long_name_the_place_from_its_own_sidecar(self):
        world = World(self, ("jo",), ("jo",))
        inbox = world.photos.manual_inbox("jo") / "drop"
        builder = TakeoutBuilder(inbox, seed=5)
        builder.shared_cut(CUT, ("_one.jpg", "_two.jpg"))
        builder.build()
        exports = unpack.register_zips(world.opened, world.opened.get(Person, world.ids["jo"]), inbox, NOW)
        world.opened.commit()
        world.run_import(exports[0])
        places = {Path(item.path).name: (item.lat, item.lon) for item in world.opened.execute(select(MediaItem).where(MediaItem.source == "takeout")).scalars()}
        self.assertEqual(places, {ONE: (10.0, 20.0), TWO: (11.0, 20.0)})


if __name__ == "__main__":
    unittest.main()


class SpecialFolderTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def report_of(self, *placements):
        builder = TakeoutBuilder(self.root / "export")
        for name, folders in placements:
            data = builder.image_bytes(".jpg")
            for folder in folders:
                builder.add(name, data, folder, 0, "json", None, None, "")
        manifest = builder.build()
        staging = unzip(manifest, self.root / "staging")
        report = pairing.pair(staging)
        folders, digests = listed_from_disk(staging)
        self.assertEqual(pairing.pair_names(folders, digests, reading=pairing.plain_text), report)
        return report

    def item(self, report, name):
        (found,) = [i for i in report.items if i.path.name == name]
        return found

    def test_a_photo_only_in_bin_or_trash_is_special_trash_and_in_no_album(self):
        report = self.report_of(("only_in_bin.jpg", ["Bin"]), ("only_in_trash.jpg", ["Trash"]), ("in_both.jpg", ["Bin", "Trash"]))
        for name in ("only_in_bin.jpg", "only_in_trash.jpg", "in_both.jpg"):
            with self.subTest(name=name):
                found = self.item(report, name)
                self.assertEqual((found.special, found.albums, found.in_year_folder), ("trash", [], False))

    def test_a_photo_in_a_year_folder_and_trash_follows_the_year_copy(self):
        report = self.report_of(("kept.jpg", ["Photos from 2019", "Trash"]), ("kept_too.jpg", ["Photos from 2019", "Bin"]))
        for name in ("kept.jpg", "kept_too.jpg"):
            with self.subTest(name=name):
                found = self.item(report, name)
                self.assertEqual((found.special, found.albums, found.in_year_folder, found.path.parent.name), (None, [], True, "Photos from 2019"))

    def test_a_photo_in_trash_and_in_an_album_without_a_year_copy_is_not_special(self):
        found = self.item(self.report_of(("shared.jpg", ["Trash", "Family trip"])), "shared.jpg")
        self.assertEqual((found.special, found.albums, found.in_year_folder), (None, ["Family trip"], False))

    def test_a_photo_only_in_archive_is_special_archive_and_in_no_album(self):
        found = self.item(self.report_of(("archived.jpg", ["Archive"])), "archived.jpg")
        self.assertEqual((found.special, found.albums, found.in_year_folder), ("archive", [], False))

    def test_a_photo_in_archive_and_trash_only_is_archive(self):
        found = self.item(self.report_of(("both.jpg", ["Trash", "Archive"])), "both.jpg")
        self.assertEqual(found.special, "archive")

    def test_a_photo_in_a_year_folder_and_archive_is_not_special(self):
        found = self.item(self.report_of(("year.jpg", ["Photos from 2019", "Archive"])), "year.jpg")
        self.assertEqual((found.special, found.albums, found.in_year_folder), (None, [], True))

    def test_only_the_fixed_names_count_and_other_folders_stay_albums(self):
        report = self.report_of(("a.jpg", ["Bins"]), ("b.jpg", ["Trash and more"]), ("c.jpg", ["Recycle"]), ("d.jpg", ["trash"]))
        for name in ("a.jpg", "b.jpg", "c.jpg"):
            with self.subTest(name=name):
                found = self.item(report, name)
                self.assertEqual((found.special, len(found.albums)), (None, 1))
        self.assertEqual(self.item(report, "d.jpg").special, "trash")

    def test_a_trashed_photo_is_not_reported_as_without_a_sidecar(self):
        builder = TakeoutBuilder(self.root / "nosidecar")
        builder.add("gone.jpg", builder.image_bytes(".jpg"), "Trash", 0, None, None, None, "")
        manifest = builder.build()
        report = pairing.pair(unzip(manifest, self.root / "staging-nosidecar"))
        self.assertEqual((self.item(report, "gone.jpg").special, report.unpaired_media), ("trash", []))
