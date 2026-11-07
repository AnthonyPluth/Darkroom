import unittest

from sqlalchemy import select

from darkroom.domain import legacy, pixel_folder
from darkroom.storage.models import MediaItem, Pixel, PixelCopy, PixelPerson
from tests.import_world import NOW, tree
from tests.test_legacy import LegacyWorld

GIGABYTE = 1024**3


class QueueCase(unittest.TestCase):
    def prepare(self, **world):
        self.home = LegacyWorld(self, **world)
        self.world = self.home.world
        self.home.imported()
        self.fresh = self.world.scratch_jpeg("fresh.jpg", taken="2015:01:01 10:00:00", offset="+00:00", colour=(1, 2, 3))
        self.home.put("google-only-copy.jpg", self.home.takeout_bytes())
        self.home.put("fresh.jpg", self.fresh)
        self.home.put("not-backed-up.jpg", (self.world.root / "jo/originals/IMG_3001.JPG").read_bytes())
        self.home.configure()

    def copies(self):
        return list(self.world.opened.execute(select(PixelCopy).order_by(PixelCopy.id)).scalars())

    def second_pixel(self, *slugs):
        pixel = Pixel(name="Second Pixel", google_account_id=self.world.account, created=1.0)
        self.world.opened.add(pixel)
        self.world.opened.flush()
        self.world.opened.add_all(PixelPerson(pixel_id=pixel.id, person_id=self.world.ids[slug]) for slug in slugs)
        self.world.opened.commit()
        return pixel


class PixelFolderQueueTests(QueueCase):
    def test_each_unmatched_file_is_queued_for_every_pixel_that_serves_the_person_and_placed_within_the_cap(self):
        self.prepare()
        second = self.second_pixel("jo")
        report = self.home.run(legacy.PixelFolderQueue())
        self.assertEqual((report.matched, report.unmatched, report.queued), (1, 2, 2))
        by_pixel = {}
        for copy in self.copies():
            by_pixel.setdefault(copy.pixel_id, []).append((copy.person_id, copy.state))
        self.assertEqual(by_pixel, {self.world.pixel: [(self.home.jo.id, "queued")] * 2, second.id: [(self.home.jo.id, "queued")] * 2})
        before = tree(self.world.root / "jo" / "originals")
        for pixel in self.world.opened.execute(select(Pixel)).scalars():
            done = pixel_folder.fill(self.world.opened, self.world.photos, pixel, GIGABYTE, NOW)
            self.assertEqual((done.placed, done.failed, done.deferred), (2, 0, 0))
        self.world.opened.commit()
        self.assertEqual(sorted(p.name for p in (self.world.root / "_pixels" / str(second.id) / "jo").rglob("*") if p.is_file()), sorted(p.name for p in (self.world.root / "_pixels" / str(self.world.pixel) / "jo").rglob("*") if p.is_file()))
        self.assertEqual(len([p for p in (self.world.root / "_pixels" / str(second.id) / "jo").rglob("*") if p.is_file()]), 2)
        self.assertEqual(tree(self.world.root / "jo" / "originals"), before)

    def test_a_pixel_added_later_gets_the_files_already_imported_and_the_first_pixel_no_duplicates(self):
        self.prepare()
        self.home.run(legacy.PixelFolderQueue())
        second = self.second_pixel("jo")
        elsewhere = self.second_pixel("sam")
        again = self.home.run(legacy.PixelFolderQueue(), at=NOW + 5)
        self.assertEqual((again.new, again.queued), (0, 0))
        rows = sorted((copy.pixel_id, copy.item_id) for copy in self.copies())
        self.assertEqual(len(rows), len(set(rows)))
        self.assertEqual(sorted({pixel for pixel, _ in rows}), sorted([self.world.pixel, second.id]))
        self.assertEqual([pixel for pixel, _ in rows].count(self.world.pixel), 2)
        self.assertEqual([pixel for pixel, _ in rows].count(second.id), 2)
        self.assertNotIn(elsewhere.id, [pixel for pixel, _ in rows])

    def test_files_count_as_waiting_again_once_the_person_loses_their_last_pixel(self):
        self.prepare()
        self.home.run(legacy.PixelFolderQueue())
        self.world.opened.query(PixelPerson).filter(PixelPerson.person_id == self.home.jo.id).delete()
        self.world.opened.commit()
        found = {entry.slug: entry for entry in legacy.totals(self.world.opened)}["jo"]
        self.assertEqual((found.unmatched, found.queued, found.pixels), (2, 0, 0))
        self.assertTrue(legacy.describe(found).endswith("2 waiting for it); no Pixel serves Jo, so nothing can be sent"))

    def test_a_matched_file_creates_no_copy(self):
        self.prepare()
        self.home.run(legacy.PixelFolderQueue())
        matched = self.home.rows()["google-only-copy.jpg"]
        self.assertEqual((matched.status, matched.original_item_id, matched.queued_at), ("matched", None, None))
        self.assertEqual(len(self.copies()), 2)

    def test_a_second_run_queues_nothing_more(self):
        self.prepare()
        self.home.run(legacy.PixelFolderQueue())
        again = self.home.run(legacy.PixelFolderQueue(), at=NOW + 5)
        self.assertEqual((again.new, again.queued, len(self.copies())), (0, 0, 2))

    def test_a_person_with_no_pixel_queues_nothing_and_the_report_says_so(self):
        self.prepare(pixel_people=("sam",))
        report = self.home.run(legacy.PixelFolderQueue())
        self.assertEqual((report.unmatched, report.queued), (2, 0))
        self.assertEqual(self.copies(), [])
        found = {entry.slug: entry for entry in legacy.totals(self.world.opened)}["jo"]
        self.assertEqual((found.pixels, found.queued), (0, 0))
        self.assertTrue(legacy.describe(found).endswith("2 waiting for it); no Pixel serves Jo, so nothing can be sent"))

    def test_another_persons_pixels_get_nothing(self):
        self.prepare(pixel_people=("sam",))
        self.home.run(legacy.PixelFolderQueue())
        self.assertEqual(self.copies(), [])
        self.assertFalse((self.world.root / "_pixels").exists() and any(p.is_file() for p in (self.world.root / "_pixels").rglob("*")))

    def test_the_report_counts_queued_and_waiting_files(self):
        self.prepare()
        self.home.run(legacy.PixelFolderQueue())
        found = {entry.slug: entry for entry in legacy.totals(self.world.opened)}["jo"]
        self.assertEqual((found.unmatched, found.queued, found.pixels), (2, 2, 1))
        self.assertIn("2 sent to the Pixel folder, 0 waiting for it)", legacy.describe(found))

    def test_a_queue_that_raises_while_retrying_a_waiting_file_does_not_stop_the_import(self):
        class Raising:
            def offer(self, opened, person, original, now):
                raise OSError("queue folder is full")

        self.prepare()
        self.home.run(legacy.NoQueue())
        report = self.home.run(Raising(), at=NOW + 5)
        self.assertEqual(report.failed, 2)
        found = {entry.slug: entry for entry in legacy.totals(self.world.opened)}["jo"]
        self.assertEqual((found.unmatched, found.failed), (0, 2))
        fixed = self.home.run(legacy.PixelFolderQueue(), at=NOW + 9)
        self.assertEqual((fixed.failed, fixed.queued, len(self.copies())), (0, 2, 2))

    def test_the_queue_refuses_what_is_not_in_the_persons_originals(self):
        self.prepare()
        item = self.world.opened.execute(select(MediaItem).where(MediaItem.source == "takeout")).scalars().first()
        self.assertFalse(legacy.PixelFolderQueue().offer(self.world.opened, self.home.jo, item, NOW))
        self.assertEqual(self.copies(), [])
