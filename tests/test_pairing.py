import tempfile
import unittest
import zipfile
from pathlib import Path

from darkroom.domain.ingest import pairing
from tests.takeout_fixtures import GOOGLE_PHOTOS, TakeoutBuilder, standard_export

YEAR = f"{GOOGLE_PHOTOS}/Photos from 2019"


def unzip(manifest, target):
    for part in manifest.parts:
        with zipfile.ZipFile(part) as archive:
            archive.extractall(target)
    return target


class PairingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

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
        item = self.only(pairing.pair(staging), name)
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

    def test_the_counter_moves_before_the_extension_on_the_sidecar(self):
        builder = TakeoutBuilder(self.root / "dup")
        builder.numbered_duplicate("IMG_0001", ".JPG")
        _, staging = self.staged(builder)
        report = pairing.pair(staging)
        self.assertEqual(self.only(report, "IMG_0001(1).JPG").sidecar.name, "IMG_0001.JPG(1).json")
        self.assertEqual(self.only(report, "IMG_0001.JPG").sidecar.name, "IMG_0001.JPG.json")

    def test_a_counter_media_does_not_take_the_uncountered_sidecar(self):
        folder = self.root / "plain"
        folder.mkdir()
        (folder / "IMG_0001(1).JPG").write_bytes(b"x")
        (folder / "IMG_0001.JPG.json").write_text("{}")
        report = pairing.pair(folder)
        self.assertIsNone(self.only(report, "IMG_0001(1).JPG").sidecar)
        self.assertEqual([p.name for p in report.orphan_sidecars], ["IMG_0001.JPG.json"])

    def test_the_counter_after_the_supplemental_part_also_pairs(self):
        folder = self.root / "modern"
        folder.mkdir()
        (folder / "IMG_0001(1).JPG").write_bytes(b"x")
        (folder / "IMG_0001.JPG.supplemental-metadata(1).json").write_text("{}")
        self.assertEqual(self.only(pairing.pair(folder), "IMG_0001(1).JPG").sidecar.name, "IMG_0001.JPG.supplemental-metadata(1).json")

    def test_an_edited_copy_shares_its_originals_sidecar_and_is_flagged(self):
        builder = TakeoutBuilder(self.root / "edit")
        builder.edited("IMG_0009", ".JPG")
        _, staging = self.staged(builder)
        report = pairing.pair(staging)
        original, edit = self.only(report, "IMG_0009.JPG"), self.only(report, "IMG_0009-edited.JPG")
        self.assertEqual(edit.sidecar, original.sidecar)
        self.assertEqual(edit.edited_of, original.path)
        self.assertIsNone(original.edited_of)

    def test_an_edited_copy_without_its_original_still_pairs_by_name_but_has_no_edited_of(self):
        folder = self.root / "lone"
        folder.mkdir()
        (folder / "IMG_1-edited.JPG").write_bytes(b"x")
        (folder / "IMG_1.JPG.json").write_text("{}")
        item = self.only(pairing.pair(folder), "IMG_1-edited.JPG")
        self.assertEqual(item.sidecar.name, "IMG_1.JPG.json")
        self.assertIsNone(item.edited_of)

    def test_a_live_photo_video_pairs_with_its_still_and_keeps_its_own_sidecar(self):
        builder = TakeoutBuilder(self.root / "live")
        builder.live_photo("IMG_0010")
        builder.live_photo("IMG_0011", still_extension=".JPG", video_extension=".MP4", video_sidecar=False)
        _, staging = self.staged(builder)
        report = pairing.pair(staging)
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
        self.assertIsNone(self.only(pairing.pair(staging), "clip.MP4").live_pair_of)

    def test_year_and_album_copies_collapse_into_one_item(self):
        builder = TakeoutBuilder(self.root / "album")
        builder.photo("IMG_0012.JPG", album="Family trip")
        builder.photo("IMG_0013.JPG", album="Family trip", year=2020)
        _, staging = self.staged(builder)
        report = pairing.pair(staging)
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
        report = pairing.pair(folder)
        (item,) = report.items
        self.assertEqual((item.albums, item.in_year_folder, len(item.copies)), (["Family"], True, 2))
        self.assertEqual(item.sidecar.name, "IMG_1.JPG(1).json")

    def test_identical_bytes_in_one_folder_stay_two_items(self):
        folder = self.root / "twins"
        folder.mkdir()
        (folder / "a.JPG").write_bytes(b"same")
        (folder / "b.JPG").write_bytes(b"same")
        self.assertEqual(len(pairing.pair(folder).items), 2)

    def test_a_video_with_two_possible_stills_is_left_unpaired(self):
        folder = self.root / "ambiguous"
        folder.mkdir()
        for name, data in (("IMG_1.HEIC", b"a"), ("IMG_1.JPG", b"b"), ("IMG_1.MOV", b"c")):
            (folder / name).write_bytes(data)
        self.assertIsNone(self.only(pairing.pair(folder), "IMG_1.MOV").live_pair_of)

    def test_an_album_only_item_has_no_year_copy(self):
        folder = self.root / "only"
        (folder / "Holiday").mkdir(parents=True)
        (folder / "Holiday" / "a.JPG").write_bytes(b"x")
        item = self.only(pairing.pair(folder), "a.JPG")
        self.assertEqual((item.albums, item.in_year_folder), (["Holiday"], False))

    def test_different_bytes_with_the_same_name_stay_separate(self):
        folder = self.root / "same"
        for year, data in (("2019", b"one"), ("2020", b"two")):
            (folder / f"Photos from {year}").mkdir(parents=True)
            (folder / f"Photos from {year}" / "IMG_1.JPG").write_bytes(data)
        self.assertEqual(len(pairing.pair(folder).items), 2)

    def test_standard_export_matches_the_manifest(self):
        manifest = standard_export(self.root / "std")
        staging = unzip(manifest, self.root / "stage")
        report = pairing.pair(staging)
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
        report = pairing.pair(unzip(manifest, self.root / "stage2"))
        names = {i.path.name for i in report.items} | {p.name for p in report.orphan_sidecars}
        for stray in manifest.non_media:
            self.assertNotIn(Path(stray).name, names)

    def test_nothing_is_written(self):
        manifest = standard_export(self.root / "std3")
        staging = unzip(manifest, self.root / "stage3")
        before = sorted((str(p), p.stat().st_mtime_ns) for p in staging.rglob("*"))
        pairing.pair(staging)
        self.assertEqual(sorted((str(p), p.stat().st_mtime_ns) for p in staging.rglob("*")), before)

    def test_a_missing_folder_gives_an_empty_report(self):
        report = pairing.pair(self.root / "nowhere")
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
        report = pairing.pair(self.root / "stage-links")
        self.assertEqual([item.path.name for item in report.items], ["IMG_1.JPG"])
        self.assertIsNone(report.items[0].sidecar)
        self.assertEqual(report.orphan_sidecars, [])
        (self.root / "linked-stage").symlink_to(self.root / "stage-links", target_is_directory=True)
        self.assertEqual(pairing.pair(self.root / "linked-stage").items, [])

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


if __name__ == "__main__":
    unittest.main()
