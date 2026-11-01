import tempfile
import unittest
import zipfile
from pathlib import Path

from tests.takeout_fixtures import GOOGLE_PHOTOS, SIDECAR_NAME_LIMIT, TakeoutBuilder, standard_export


def names(path):
    with zipfile.ZipFile(path) as archive:
        return set(archive.namelist())


class TakeoutBuilderTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def sidecar_of(self, form, name="IMG_1.JPG"):
        builder = TakeoutBuilder(self.root)
        builder.photo(name, form=form)
        manifest = builder.build()
        return manifest.pairings[f"{GOOGLE_PHOTOS}/Photos from 2019/{name}"]

    def test_each_sidecar_form_names_the_sidecar_as_google_does(self):
        base = f"{GOOGLE_PHOTOS}/Photos from 2019/IMG_1.JPG"
        self.assertEqual(self.sidecar_of("json"), f"{base}.json")
        self.assertEqual(self.sidecar_of("supplemental"), f"{base}.supplemental-metadata.json")
        self.assertEqual(self.sidecar_of("truncated"), f"{base}.supplemental-metada.json")
        self.assertEqual(self.sidecar_of("short"), f"{base}.supplemental-me.json")
        self.assertEqual(self.sidecar_of("tiny"), f"{base}.s.json")
        self.assertIsNone(self.sidecar_of(None))

    def test_limit_form_cuts_the_media_name_before_the_extension_suffix(self):
        long_name = "a_very_long_original_file_name_from_a_camera_roll_0007.JPG"
        path = self.sidecar_of("limit", long_name)
        self.assertEqual(Path(path).name, f"{long_name[:SIDECAR_NAME_LIMIT]}.json")

    def test_builds_are_deterministic(self):
        first = standard_export(self.root / "a", seed=3)
        second = standard_export(self.root / "b", seed=3)
        self.assertEqual([p.read_bytes() for p in first.parts], [p.read_bytes() for p in second.parts])
        other = standard_export(self.root / "c", seed=4)
        self.assertNotEqual([p.read_bytes() for p in first.parts], [p.read_bytes() for p in other.parts])

    def test_standard_export_spreads_over_parts_and_records_everything(self):
        manifest = standard_export(self.root)
        self.assertEqual(len(manifest.parts), 2)
        everything = names(manifest.parts[0]) | names(manifest.parts[1])
        for media in manifest.media:
            self.assertIn(media, everything)
        for sidecar in manifest.pairings.values():
            if sidecar:
                self.assertIn(sidecar, everything)
        for stray in manifest.orphan_sidecars + manifest.non_media:
            self.assertIn(stray, everything)
        self.assertIn("Family trip", manifest.albums)
        self.assertEqual(len(manifest.albums["Family trip"]), 1)
        self.assertEqual(len(manifest.live_pairs), 2)
        self.assertEqual(list(manifest.edited_of.values()), ["IMG_0009.JPG"])

    def test_sidecar_can_live_in_another_part_than_its_media(self):
        manifest = standard_export(self.root)
        media = f"{GOOGLE_PHOTOS}/Photos from 2019/IMG_0002.JPG"
        sidecar = manifest.pairings[media]
        self.assertIn(media, names(manifest.parts[0]))
        self.assertIn(sidecar, names(manifest.parts[1]))

    def test_zero_byte_and_corrupt_parts(self):
        builder = TakeoutBuilder(self.root / "z", parts=2)
        builder.photo("IMG_1.JPG")
        builder.photo("IMG_2.JPG", part=1)
        builder.zero_byte_part = 1
        manifest = builder.build()
        self.assertEqual(manifest.parts[1].stat().st_size, 0)
        broken = TakeoutBuilder(self.root / "c")
        broken.photo("IMG_1.JPG")
        broken.corrupt_part = 0
        part = broken.build().parts[0]
        with zipfile.ZipFile(part) as archive:
            damaged = False
            try:
                damaged = archive.testzip() is not None
            except (zipfile.BadZipFile, OSError):
                damaged = True
        self.assertTrue(damaged)
