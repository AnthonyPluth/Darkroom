import hashlib
import itertools
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom.domain import dedupe, pixel_folder
from darkroom.storage.models import DedupeDecision, Event, Export, GoogleAccount, MediaItem, Pixel, PixelCopy, PixelPerson
from darkroom.storage.paths import Photos
from tests.shared import add_person, own_database

NOW = 1_700_000_000.0


def sha1(data: bytes) -> str:
    return hashlib.sha1(data, usedforsecurity=False).hexdigest()


def kept_tree(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): sha1(path.read_bytes()) for path in sorted(root.rglob("*"))
            if path.is_file() and not str(path.relative_to(root)).startswith("_")}


class PixelFolderTests(unittest.TestCase):
    def setUp(self):
        self.opened = Session(own_database(self), expire_on_commit=False)
        self.addCleanup(self.opened.close)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.photos = Photos(self.root)
        self.counter = itertools.count(1)
        self.jo, self.sam = add_person(self.opened, "jo"), add_person(self.opened, "sam")
        self.jo_account, self.jo_pixel = self.account("jo@x.test", self.jo, (self.jo,))
        self.sam_account, self.sam_pixel = self.account("sam@x.test", self.sam, (self.sam,))

    def account(self, email: str, person: int, served: tuple[int, ...]) -> tuple[int, int]:
        account = GoogleAccount(email=email, default_person_id=person, created=1.0)
        self.opened.add(account)
        self.opened.flush()
        pixel = Pixel(name="Pixel", google_account_id=account.id, created=1.0)
        self.opened.add(pixel)
        self.opened.flush()
        self.opened.add_all(PixelPerson(pixel_id=pixel.id, person_id=each) for each in served)
        self.opened.flush()
        return account.id, pixel.id

    def item(self, person: int, size: int = 100) -> MediaItem:
        number = next(self.counter)
        slug = "jo" if person == self.jo else "sam"
        data = f"{slug}-{number}".encode().ljust(size, b".")
        path = self.root / slug / "originals" / "2024" / f"IMG_{number:04d}.HEIC"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        item = MediaItem(person_id=person, source="phone", path=f"{slug}/originals/2024/{path.name}", sha1=sha1(data), bytes=size, indexed_at=1.0)
        self.opened.add(item)
        self.opened.flush()
        return item

    def match(self, item: MediaItem, account_id: int, person_id: int | None = None, tier: int = 1, by: str = "auto", at: float = NOW) -> DedupeDecision:
        export = Export(account_id=account_id, kind="manual")
        self.opened.add(export)
        self.opened.flush()
        row = DedupeDecision(export_id=export.id, account_id=account_id, source_path=f"Takeout/{next(self.counter)}.HEIC",
                             person_id=item.person_id if person_id is None else person_id, matched_item_id=item.id, tier=tier, reason="test",
                             decided_by=by, decided_at=at)
        self.opened.add(row)
        self.opened.flush()
        return row

    def copies(self, pixel_id: int) -> list[PixelCopy]:
        return list(self.opened.execute(select(PixelCopy).where(PixelCopy.pixel_id == pixel_id).order_by(PixelCopy.id)).scalars())

    def copy_path(self, pixel_id: int, item: MediaItem) -> Path:
        slug = item.path.split("/")[0]
        return self.root / "_pixels" / str(pixel_id) / slug / "2024" / Path(item.path).name

    def tend(self, cap: int = pixel_folder.DEFAULT_CAP_BYTES, at: float = NOW) -> pixel_folder.Tended:
        return pixel_folder.tend(self.opened, self.photos, cap, at)

    def test_a_queued_photo_is_copied_from_originals_to_each_pixel_that_serves_the_person(self):
        item = self.item(self.jo)
        before = kept_tree(self.root)
        self.assertEqual(pixel_folder.queue(self.opened, item, NOW), 1)
        self.assertEqual(self.tend().placed, 1)
        self.assertEqual(self.copy_path(self.jo_pixel, item).read_bytes(), (self.root / item.path).read_bytes())
        self.assertEqual([(c.state, c.name) for c in self.copies(self.jo_pixel)], [("placed", f"2024/{Path(item.path).name}")])
        self.assertEqual(self.copies(self.sam_pixel), [])
        self.assertEqual(kept_tree(self.root), before)

    def test_a_confirming_match_in_the_pixels_own_account_prunes_the_copy_only(self):
        item = self.item(self.jo)
        pixel_folder.queue(self.opened, item, NOW)
        self.tend()
        before = kept_tree(self.root)
        self.match(item, self.jo_account, at=NOW + 10)
        tended = self.tend(at=NOW + 20)
        self.assertEqual(tended.pruned, 1)
        self.assertFalse(self.copy_path(self.jo_pixel, item).exists())
        self.assertEqual([(c.state, c.pruned_at) for c in self.copies(self.jo_pixel)], [("pruned", NOW + 20)])
        self.assertEqual(kept_tree(self.root), before)
        self.assertTrue((self.root / item.path).is_file())

    def test_a_match_in_another_persons_takeout_does_not_prune(self):
        item = self.item(self.jo)
        pixel_folder.queue(self.opened, item, NOW)
        self.tend()
        self.match(item, self.sam_account, at=NOW + 10)
        self.match(item, self.sam_account, person_id=self.sam, at=NOW + 10)
        self.match(item, self.jo_account, person_id=self.sam, at=NOW + 10)
        self.assertEqual(self.tend(at=NOW + 20).pruned, 0)
        self.assertTrue(self.copy_path(self.jo_pixel, item).is_file())
        self.assertEqual([c.state for c in self.copies(self.jo_pixel)], ["placed"])

    def test_only_an_unreversed_automatic_tier_1_or_2_match_prunes(self):
        item = self.item(self.jo)
        pixel_folder.queue(self.opened, item, NOW)
        self.tend()
        self.match(item, self.jo_account, tier=4, at=NOW + 10)
        self.match(item, self.jo_account, tier=3, at=NOW + 10)
        self.match(item, self.jo_account, by="owner", at=NOW + 10)
        dedupe.reverse(self.opened, self.match(item, self.jo_account, at=NOW + 10).id, NOW + 11, "owner")
        self.assertEqual(self.tend(at=NOW + 20).pruned, 0)
        self.assertTrue(self.copy_path(self.jo_pixel, item).is_file())
        self.match(item, self.jo_account, tier=2, at=NOW + 30)
        self.assertEqual(self.tend(at=NOW + 40).pruned, 1)

    def test_a_shared_pixel_prunes_one_persons_copy_without_touching_anothers(self):
        self.opened.add(PixelPerson(pixel_id=self.jo_pixel, person_id=self.sam))
        self.opened.flush()
        mine, theirs = self.item(self.jo), self.item(self.sam)
        pixel_folder.queue(self.opened, mine, NOW)
        pixel_folder.queue(self.opened, theirs, NOW)
        self.tend()
        self.match(mine, self.jo_account, at=NOW + 10)
        self.match(theirs, self.sam_account, at=NOW + 10)
        self.assertEqual(self.tend(at=NOW + 20).pruned, 2)
        self.assertFalse(self.copy_path(self.jo_pixel, mine).exists())
        self.assertTrue(self.copy_path(self.jo_pixel, theirs).is_file())
        self.assertFalse(self.copy_path(self.sam_pixel, theirs).exists())

    def test_a_match_reversed_after_pruning_requeues_the_photo_from_originals(self):
        item = self.item(self.jo)
        pixel_folder.queue(self.opened, item, NOW)
        self.tend()
        decision = self.match(item, self.jo_account, at=NOW + 10)
        self.tend(at=NOW + 20)
        self.assertFalse(self.copy_path(self.jo_pixel, item).exists())
        before = kept_tree(self.root)
        dedupe.reverse(self.opened, decision.id, NOW + 30, "owner")
        self.assertEqual([(c.state, c.requeued) for c in self.copies(self.jo_pixel)], [("queued", 1)])
        self.assertEqual(self.opened.execute(select(Event.type).where(Event.type == "pixel_copy_requeued")).scalars().all(), ["pixel_copy_requeued"])
        self.assertEqual(self.tend(at=NOW + 40).placed, 1)
        self.assertEqual(self.copy_path(self.jo_pixel, item).read_bytes(), (self.root / item.path).read_bytes())
        self.assertEqual(kept_tree(self.root), before)

    def test_reversing_one_of_two_matches_keeps_the_photo_pruned(self):
        item = self.item(self.jo)
        pixel_folder.queue(self.opened, item, NOW)
        self.tend()
        first = self.match(item, self.jo_account, at=NOW + 10)
        self.match(item, self.jo_account, at=NOW + 11)
        self.tend(at=NOW + 20)
        dedupe.reverse(self.opened, first.id, NOW + 30, "owner")
        self.assertEqual([c.state for c in self.copies(self.jo_pixel)], ["pruned"])
        self.assertFalse(self.copy_path(self.jo_pixel, item).exists())

    def test_a_reversed_match_leaves_the_photo_pruned_on_a_pixel_that_no_longer_serves_the_person(self):
        item = self.item(self.jo)
        pixel_folder.queue(self.opened, item, NOW)
        self.tend()
        decision = self.match(item, self.jo_account, at=NOW + 10)
        self.tend(at=NOW + 20)
        self.opened.delete(self.opened.get(PixelPerson, (self.jo_pixel, self.jo)))
        self.opened.flush()
        dedupe.reverse(self.opened, decision.id, NOW + 30, "owner")
        self.assertEqual([(c.state, c.requeued) for c in self.copies(self.jo_pixel)], [("pruned", 0)])
        tended = self.tend(at=NOW + 40)
        self.assertEqual((tended.placed, tended.failed), (0, 0))
        self.assertFalse(self.copy_path(self.jo_pixel, item).exists())

    def test_reversing_a_match_in_another_account_requeues_nothing(self):
        item = self.item(self.jo)
        pixel_folder.queue(self.opened, item, NOW)
        self.tend()
        self.match(item, self.jo_account, at=NOW + 10)
        self.tend(at=NOW + 20)
        dedupe.reverse(self.opened, self.match(item, self.sam_account, at=NOW + 21).id, NOW + 30, "owner")
        self.assertEqual([c.state for c in self.copies(self.jo_pixel)], ["pruned"])

    def test_the_size_cap_defers_further_batches_until_earlier_photos_are_pruned(self):
        items = [self.item(self.jo) for _ in range(5)]
        for each in items:
            pixel_folder.queue(self.opened, each, NOW)
        tended = self.tend(cap=250)
        self.assertEqual((tended.placed, tended.deferred), (2, 3))
        self.assertEqual([c.state for c in self.copies(self.jo_pixel)], ["placed", "placed", "queued", "queued", "queued"])
        self.assertFalse(self.copy_path(self.jo_pixel, items[2]).exists())
        tended = self.tend(cap=250, at=NOW + 5)
        self.assertEqual((tended.placed, tended.deferred), (0, 3))
        self.match(items[0], self.jo_account, at=NOW + 10)
        tended = self.tend(cap=250, at=NOW + 20)
        self.assertEqual((tended.pruned, tended.placed, tended.deferred), (1, 1, 2))
        self.assertEqual([c.state for c in self.copies(self.jo_pixel)], ["pruned", "placed", "placed", "queued", "queued"])

    def test_a_photo_bigger_than_the_cap_waits_for_an_empty_folder(self):
        small, big = self.item(self.jo, 100), self.item(self.jo, 400)
        pixel_folder.queue(self.opened, small, NOW)
        pixel_folder.queue(self.opened, big, NOW + 1)
        self.assertEqual(self.tend(cap=250).placed, 1)
        self.match(small, self.jo_account, at=NOW + 10)
        tended = self.tend(cap=250, at=NOW + 20)
        self.assertEqual((tended.pruned, tended.placed), (1, 1))
        self.assertTrue(self.copy_path(self.jo_pixel, big).is_file())

    def test_a_photo_already_confirmed_for_the_pixels_account_is_not_queued(self):
        item = self.item(self.jo)
        self.match(item, self.jo_account)
        self.assertEqual(pixel_folder.queue(self.opened, item, NOW), 0)
        self.assertEqual([(c.state, c.name) for c in self.copies(self.jo_pixel)], [("pruned", None)])
        self.match(item, self.sam_account)
        self.assertEqual(pixel_folder.queue(self.opened, self.item(self.jo), NOW), 1)

    def test_a_queued_photo_confirmed_before_it_was_placed_is_skipped(self):
        item = self.item(self.jo)
        pixel_folder.queue(self.opened, item, NOW)
        self.match(item, self.jo_account, at=NOW + 1)
        self.assertEqual(self.tend(at=NOW + 2).skipped, 1)
        self.assertFalse(self.copy_path(self.jo_pixel, item).exists())
        self.assertEqual([c.state for c in self.copies(self.jo_pixel)], ["pruned"])

    def test_an_immich_photo_is_sent_even_when_a_match_exists_and_pruned_only_on_a_later_match(self):
        item = self.item(self.jo)
        self.match(item, self.jo_account, at=NOW - 100)
        self.assertEqual(pixel_folder.queue(self.opened, item, NOW, always_send=True), 1)
        self.assertEqual(self.tend(at=NOW).placed, 1)
        self.assertEqual(self.tend(at=NOW + 10).pruned, 0)
        self.assertTrue(self.copy_path(self.jo_pixel, item).is_file())
        self.match(item, self.jo_account, at=NOW + 20)
        self.assertEqual(self.tend(at=NOW + 30).pruned, 1)

    def test_a_photo_confirmed_before_it_was_queued_is_sent_once_that_match_is_reversed(self):
        item = self.item(self.jo)
        decision = self.match(item, self.jo_account, at=NOW - 10)
        self.assertEqual(pixel_folder.queue(self.opened, item, NOW), 0)
        self.assertEqual(self.tend(at=NOW + 1).placed, 0)
        dedupe.reverse(self.opened, decision.id, NOW + 2, "owner")
        self.assertEqual(self.tend(at=NOW + 3).placed, 1)
        self.assertEqual(self.copy_path(self.jo_pixel, item).read_bytes(), (self.root / item.path).read_bytes())

    def test_an_immich_arrival_puts_a_withheld_photo_back_in_the_queue(self):
        item = self.item(self.jo)
        self.match(item, self.jo_account, at=NOW - 10)
        pixel_folder.queue(self.opened, item, NOW)
        self.assertEqual(pixel_folder.queue(self.opened, item, NOW + 1, always_send=True), 1)
        self.assertEqual([(c.state, c.always_send) for c in self.copies(self.jo_pixel)], [("queued", True)])
        self.assertEqual(self.tend(at=NOW + 2).placed, 1)
        self.assertEqual(pixel_folder.queue(self.opened, item, NOW + 3, always_send=True), 0)
        self.assertEqual([c.state for c in self.copies(self.jo_pixel)], ["placed"])

    def test_each_placed_copy_is_kept_when_a_later_one_fails_the_pass(self):
        first, second = self.item(self.jo), self.item(self.jo)
        pixel_folder.queue(self.opened, first, NOW)
        pixel_folder.queue(self.opened, second, NOW)
        self.opened.commit()
        real = pixel_folder.paths.place_pixel_copy
        calls = []

        def stops_on_the_second(*args: object) -> str:
            calls.append(args)
            if len(calls) == 2:
                raise RuntimeError("the process stopped")
            return real(*args)

        with mock.patch.object(pixel_folder.paths, "place_pixel_copy", stops_on_the_second), self.assertRaises(RuntimeError):
            self.tend()
        self.opened.rollback()
        self.assertEqual([c.state for c in self.copies(self.jo_pixel)], ["placed", "queued"])
        self.assertEqual(self.tend(at=NOW + 1).placed, 1)

    def test_reversing_the_match_that_pruned_an_immich_photo_restores_it_despite_an_older_match(self):
        item = self.item(self.jo)
        self.match(item, self.jo_account, at=NOW - 100)
        pixel_folder.queue(self.opened, item, NOW, always_send=True)
        self.tend(at=NOW)
        newer = self.match(item, self.jo_account, at=NOW + 10)
        self.assertEqual(self.tend(at=NOW + 20).pruned, 1)
        dedupe.reverse(self.opened, newer.id, NOW + 30, "owner")
        self.assertEqual([(c.state, c.requeued) for c in self.copies(self.jo_pixel)], [("queued", 1)])
        self.assertEqual(self.tend(at=NOW + 40).placed, 1)
        self.assertEqual(self.copy_path(self.jo_pixel, item).read_bytes(), (self.root / item.path).read_bytes())

    def test_a_repeat_in_another_accounts_or_persons_takeout_leaves_the_copy_unseen(self):
        item = self.item(self.jo)
        pixel_folder.queue(self.opened, item, NOW, always_send=True)
        self.tend(at=NOW)
        pixel_folder.seen_again(self.opened, self.sam_account, item.id, self.jo, NOW + 10)
        pixel_folder.seen_again(self.opened, self.jo_account, item.id, self.sam, NOW + 10)
        self.assertEqual([c.seen_again_at for c in self.copies(self.jo_pixel)], [None])
        pixel_folder.seen_again(self.opened, self.jo_account, item.id, self.jo, NOW + 10)
        self.assertEqual([c.seen_again_at for c in self.copies(self.jo_pixel)], [NOW + 10])

    def test_queueing_twice_keeps_one_row_per_pixel(self):
        item = self.item(self.jo)
        pixel_folder.queue(self.opened, item, NOW)
        self.assertEqual(pixel_folder.queue(self.opened, item, NOW, always_send=True), 0)
        self.assertEqual([(c.state, c.always_send) for c in self.copies(self.jo_pixel)], [("queued", True)])

    def test_only_a_photo_in_originals_is_queued(self):
        takeout = MediaItem(person_id=self.jo, source="takeout", path="jo/google-only/2024/01/a.jpg", sha1="a" * 40, bytes=1, indexed_at=1.0)
        elsewhere = MediaItem(person_id=self.jo, source="phone", path="sam/originals/a.jpg", sha1="b" * 40, bytes=1, indexed_at=1.0)
        self.opened.add_all([takeout, elsewhere])
        self.opened.flush()
        for item in (takeout, elsewhere):
            with self.assertRaises(pixel_folder.NotInOriginals):
                pixel_folder.queue(self.opened, item, NOW)
        self.assertEqual(self.copies(self.jo_pixel), [])

    def test_a_changed_copy_is_refused_and_left_in_place(self):
        item = self.item(self.jo)
        pixel_folder.queue(self.opened, item, NOW)
        self.tend()
        self.copy_path(self.jo_pixel, item).write_bytes(b"changed")
        self.match(item, self.jo_account, at=NOW + 10)
        before = kept_tree(self.root)
        self.assertEqual(self.tend(at=NOW + 20).refused, 1)
        self.assertEqual(self.copy_path(self.jo_pixel, item).read_bytes(), b"changed")
        self.assertEqual([c.state for c in self.copies(self.jo_pixel)], ["placed"])
        self.assertEqual(self.opened.execute(select(Event.type).where(Event.type == "pixel_prune_refused")).scalars().all(), ["pixel_prune_refused"])
        self.assertEqual(kept_tree(self.root), before)

    def test_an_original_that_changed_or_a_person_no_longer_served_is_not_placed(self):
        changed, moved = self.item(self.jo), self.item(self.jo)
        pixel_folder.queue(self.opened, changed, NOW)
        pixel_folder.queue(self.opened, moved, NOW)
        (self.root / changed.path).write_bytes(b"not what was indexed")
        self.opened.delete(self.opened.get(PixelPerson, (self.jo_pixel, self.jo)))
        self.opened.add(PixelPerson(pixel_id=self.jo_pixel, person_id=self.sam))
        self.opened.flush()
        self.assertEqual(self.tend().failed, 2)
        self.assertEqual([c.state for c in self.copies(self.jo_pixel)], ["queued", "queued"])
        self.assertFalse((self.root / "_pixels" / str(self.jo_pixel) / "jo").exists())


if __name__ == "__main__":
    unittest.main()
