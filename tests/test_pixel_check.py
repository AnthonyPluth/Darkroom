import hashlib
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import select
from sqlalchemy import update as sql_update

from darkroom.domain import pixel_check, pixel_folder
from darkroom.storage.models import Event, PixelCopy
from tests import test_pixel_folder

NOW = test_pixel_folder.NOW


def share_tree(root: Path) -> dict[str, str]:
    found = {}
    for path in sorted(root.rglob("*")):
        name = str(path.relative_to(root))
        if path.is_symlink():
            found[name] = "link:" + os.readlink(path)
        elif path.is_file():
            found[name] = hashlib.sha1(path.read_bytes(), usedforsecurity=False).hexdigest()
        else:
            found[name] = "folder"
    return found


class PlacedCopyCheckTests(unittest.TestCase):
    setUp = test_pixel_folder.PixelFolderTests.setUp
    account = test_pixel_folder.PixelFolderTests.account
    item = test_pixel_folder.PixelFolderTests.item
    match = test_pixel_folder.PixelFolderTests.match
    copies = test_pixel_folder.PixelFolderTests.copies
    copy_path = test_pixel_folder.PixelFolderTests.copy_path
    tend = test_pixel_folder.PixelFolderTests.tend

    def placed(self, count: int = 1, always_send: bool = False, cap: int = pixel_folder.DEFAULT_CAP_BYTES):
        items = [self.item(self.jo) for _ in range(count)]
        for offset, each in enumerate(items):
            pixel_folder.queue(self.opened, each, NOW + offset, always_send=always_send)
        self.tend(cap=cap, at=NOW + 10)
        return items

    def check(self, at: float = NOW + 100) -> pixel_check.Checked:
        return pixel_check.check(self.opened, self.photos, at)

    def held(self, ran: bool = True) -> bool:
        return pixel_check.held(self.opened, self.jo_pixel, ran)

    def events(self, kind: str) -> list[str | None]:
        return list(self.opened.execute(select(Event.detail).where(Event.type == kind).order_by(Event.id)).scalars())

    def state(self) -> list[tuple[str, float | None, float]]:
        return [(c.state, c.placed_at, c.queued_at) for c in self.copies(self.jo_pixel)]

    def assert_requeued_untouched(self, damage):
        (item,) = self.placed()
        damage(self.copy_path(self.jo_pixel, item))
        before = share_tree(self.root)
        self.assertEqual(self.check(), pixel_check.Checked(checked=1, intact=0, requeued=1))
        self.assertEqual(share_tree(self.root), before)
        self.assertEqual(self.state(), [("queued", NOW + 10, NOW)])
        self.assertTrue(self.held())
        self.assertEqual(self.events("pixel_copy_missing"), [f"copy {self.copies(self.jo_pixel)[0].id}"])
        self.assertEqual(self.events("pixel_copy_requeued"), [])
        return item

    def test_a_missing_copy_is_written_again_verified_and_releases_the_hold(self):
        item = self.assert_requeued_untouched(Path.unlink)
        self.assertEqual(self.tend(at=NOW + 200).placed, 1)
        self.assertEqual(self.copy_path(self.jo_pixel, item).read_bytes(), (self.root / item.path).read_bytes())
        self.assertEqual(self.state(), [("placed", NOW + 10, NOW)])
        self.assertEqual(len(self.events("pixel_copy_replaced")), 1)
        self.assertFalse(self.held())

    def test_a_changed_copy_is_requeued_and_left_in_place_while_the_pixel_stays_held(self):
        self.assert_requeued_untouched(lambda path: path.write_bytes(b"changed"))
        before = share_tree(self.root)
        self.assertEqual(self.tend(at=NOW + 200).failed, 1)
        self.assertEqual(share_tree(self.root), before)
        self.assertTrue(self.held())
        self.assertEqual(len(self.events("pixel_copy_replacement_pending")), 1)

    def test_a_copy_of_another_size_is_requeued(self):
        self.assert_requeued_untouched(lambda path: path.write_bytes(path.read_bytes() + b"more"))

    def test_a_link_in_place_of_the_copy_is_requeued_and_never_followed(self):
        def link(path: Path) -> None:
            path.unlink()
            path.symlink_to(self.root / "jo" / "originals" / "2024" / "IMG_0001.HEIC")
        self.assert_requeued_untouched(link)
        before = share_tree(self.root)
        self.assertEqual(self.tend(at=NOW + 200).failed, 1)
        self.assertEqual(share_tree(self.root), before)
        self.assertTrue(self.held())

    def test_a_missing_pixel_folder_requeues_every_copy_and_the_tend_refills_it(self):
        items = self.placed(2)
        shutil.rmtree(self.root / "_pixels" / str(self.jo_pixel))
        self.assertEqual(self.check().requeued, 2)
        self.assertTrue(self.held())
        self.assertEqual(self.tend(at=NOW + 200).placed, 2)
        for each in items:
            self.assertEqual(self.copy_path(self.jo_pixel, each).read_bytes(), (self.root / each.path).read_bytes())
        self.assertFalse(self.held())

    def test_a_copy_whose_recorded_name_is_outside_the_pixel_folder_is_requeued_and_placed_under_its_own_name(self):
        (item,) = self.placed()
        self.opened.execute(sql_update(PixelCopy).values(name="../../jo/originals/2024/x.HEIC"))
        self.opened.commit()
        before = share_tree(self.root)
        self.assertEqual(self.check().requeued, 1)
        self.assertEqual(share_tree(self.root), before)
        self.assertTrue(self.held())
        self.assertEqual(self.tend(at=NOW + 200).placed, 1)
        self.assertEqual([c.name for c in self.copies(self.jo_pixel)], [f"2024/{Path(item.path).name}"])
        self.assertEqual(share_tree(self.root), before)
        self.assertFalse(self.held())

    def test_an_intact_copy_is_never_requeued(self):
        self.placed(2)
        before = share_tree(self.root)
        for at in (NOW + 100, NOW + 200):
            self.assertEqual(self.check(at), pixel_check.Checked(checked=2, intact=2, requeued=0))
        self.assertEqual(share_tree(self.root), before)
        self.assertEqual([c.state for c in self.copies(self.jo_pixel)], ["placed", "placed"])
        self.assertEqual(self.events("pixel_copy_missing"), [])
        self.assertFalse(self.held())
        self.assertTrue(self.held(ran=False))

    def test_an_always_send_copy_with_an_older_match_is_written_again_and_not_pruned_until_a_later_match(self):
        (item,) = self.placed(always_send=True)
        self.match(item, self.jo_account, at=NOW - 100)
        self.copy_path(self.jo_pixel, item).unlink()
        self.check()
        tended = self.tend(at=NOW + 200)
        self.assertEqual((tended.placed, tended.skipped, tended.pruned), (1, 0, 0))
        self.assertTrue(self.copy_path(self.jo_pixel, item).is_file())
        self.assertEqual(self.tend(at=NOW + 300).pruned, 0)
        self.assertEqual(self.state(), [("placed", NOW + 10, NOW)])
        self.match(item, self.jo_account, at=NOW + 400)
        self.assertEqual(self.tend(at=NOW + 500).pruned, 1)

    def test_a_requeued_copy_confirmed_before_it_was_requeued_is_still_written_again(self):
        (item,) = self.placed()
        self.copy_path(self.jo_pixel, item).unlink()
        self.check()
        self.match(item, self.jo_account, at=NOW + 150)
        self.assertEqual(self.tend(at=NOW + 200).placed, 1)
        self.assertTrue(self.copy_path(self.jo_pixel, item).is_file())

    def test_the_hold_stays_while_the_source_is_gone_or_changed_and_lifts_once_the_copy_is_placed(self):
        (item,) = self.placed()
        original = self.root / item.path
        data = original.read_bytes()
        self.copy_path(self.jo_pixel, item).unlink()
        self.check()
        original.write_bytes(b"changed")
        self.assertEqual(self.tend(at=NOW + 200).failed, 1)
        self.assertTrue(self.held())
        original.unlink()
        self.assertEqual(self.tend(at=NOW + 300).failed, 1)
        self.assertTrue(self.held())
        self.assertEqual(len(self.events("pixel_copy_replacement_pending")), 2)
        self.assertEqual(self.state(), [("queued", NOW + 10, NOW)])
        original.write_bytes(data)
        self.assertEqual(self.tend(at=NOW + 400).placed, 1)
        self.assertFalse(self.held())

    def test_a_copy_that_does_not_read_back_as_recorded_keeps_the_hold(self):
        (item,) = self.placed()
        self.copy_path(self.jo_pixel, item).unlink()
        self.check()
        with mock.patch.object(pixel_folder.paths, "pixel_copy_intact", return_value=False):
            self.assertEqual(self.tend(at=NOW + 200).failed, 1)
        self.assertTrue(self.held())
        self.assertEqual(self.state(), [("queued", NOW + 10, NOW)])

    def test_a_requeued_copy_keeps_its_place_ahead_of_later_copies_and_the_cap_it_fitted_under(self):
        first, second = self.placed(2, cap=250)
        later = self.item(self.jo)
        pixel_folder.queue(self.opened, later, NOW + 50)
        self.assertEqual(self.tend(cap=250, at=NOW + 60).deferred, 1)
        self.copy_path(self.jo_pixel, first).unlink()
        self.check()
        tended = self.tend(cap=150, at=NOW + 200)
        self.assertEqual((tended.placed, tended.deferred), (1, 1))
        self.assertTrue(self.copy_path(self.jo_pixel, first).is_file())
        self.assertFalse(self.copy_path(self.jo_pixel, later).exists())
        self.assertEqual([(c.item_id, c.state, c.queued_at) for c in self.copies(self.jo_pixel)],
                         [(first.id, "placed", NOW), (second.id, "placed", NOW + 1), (later.id, "queued", NOW + 50)])
        self.assertFalse(self.held())

    def test_a_requeued_copy_queued_after_a_deferred_one_is_still_placed(self):
        first, _ = self.placed(2, cap=200)
        self.opened.execute(sql_update(PixelCopy).where(PixelCopy.item_id == first.id).values(queued_at=NOW + 70))
        self.opened.commit()
        earlier = self.item(self.jo)
        pixel_folder.queue(self.opened, earlier, NOW + 60)
        self.opened.execute(sql_update(PixelCopy).where(PixelCopy.item_id == earlier.id).values(bytes=1000))
        self.opened.commit()
        self.copy_path(self.jo_pixel, first).unlink()
        self.check()
        tended = self.tend(cap=200, at=NOW + 200)
        self.assertEqual((tended.placed, tended.deferred), (1, 1))
        self.assertTrue(self.copy_path(self.jo_pixel, first).is_file())
        self.assertFalse(self.held())

    def test_a_copy_that_changed_state_before_its_lock_is_left_alone(self):
        (item,) = self.placed()
        self.copy_path(self.jo_pixel, item).unlink()
        real = pixel_folder.lock_copy

        def pruned_meanwhile(opened, copy_id):
            opened.execute(sql_update(PixelCopy).where(PixelCopy.id == copy_id).values(state="pruned"))
            opened.commit()
            real(opened, copy_id)
        with mock.patch.object(pixel_folder, "lock_copy", side_effect=pruned_meanwhile):
            self.assertEqual(self.check().checked, 0)
        self.assertEqual([c.state for c in self.copies(self.jo_pixel)], ["pruned"])

    def test_the_check_object_holds_every_pixel_until_it_has_run(self):
        self.placed()
        placed_copies = pixel_check.PlacedCopyCheck()
        self.assertTrue(placed_copies.due(self.opened))
        self.assertTrue(placed_copies.held(self.opened, self.sam_pixel))
        self.assertEqual(placed_copies.run(self.opened, self.photos, NOW + 100).intact, 1)
        self.assertFalse(placed_copies.due(self.opened))
        self.assertFalse(placed_copies.held(self.opened, self.jo_pixel))
        self.opened.execute(sql_update(PixelCopy).values(state="queued"))
        self.opened.commit()
        self.assertTrue(placed_copies.due(self.opened))
        self.assertEqual(pixel_check.replacing_pixels(self.opened), [self.jo_pixel])
        self.assertTrue(placed_copies.held(self.opened, self.jo_pixel))
        self.assertFalse(placed_copies.held(self.opened, self.sam_pixel))


if __name__ == "__main__":
    unittest.main()
