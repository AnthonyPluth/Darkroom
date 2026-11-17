import contextlib
import errno
import hashlib
import os
import shutil
import threading
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import select
from sqlalchemy import update as sql_update

from darkroom.domain import pixel_check, pixel_folder, transport
from darkroom.providers import syncthing
from darkroom.storage.models import Event, Pixel, PixelCopy
from tests.fakes.syncthing import FakeSyncthing
from tests import test_pixel_folder
from tests.privacy import no_leaks

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
        self.assertEqual(len(self.events("pixel_copy_replacement_pending")), 1)
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

    def test_a_copy_awaiting_replacement_keeps_its_room_under_the_cap(self):
        first, second = self.placed(2, cap=200)
        original = self.root / first.path
        data = original.read_bytes()
        self.copy_path(self.jo_pixel, first).unlink()
        original.unlink()
        self.check()
        self.assertEqual(self.tend(cap=200, at=NOW + 200).failed, 1)
        later = self.item(self.jo)
        pixel_folder.queue(self.opened, later, NOW + 250)
        self.assertEqual(self.tend(cap=200, at=NOW + 300).deferred, 1)
        self.assertFalse(self.copy_path(self.jo_pixel, later).exists())
        original.write_bytes(data)
        tended = self.tend(cap=200, at=NOW + 400)
        self.assertEqual((tended.placed, tended.deferred), (1, 1))
        self.assertEqual(pixel_folder.used_bytes(self.opened, self.jo_pixel), 200)
        self.assertEqual(self.tend(cap=200, at=NOW + 500).deferred, 1)
        self.assertFalse(self.copy_path(self.jo_pixel, later).exists())
        self.assertTrue(self.copy_path(self.jo_pixel, second).is_file())

    def test_a_pipe_in_place_of_the_copy_never_blocks_the_check_or_the_tend(self):
        (item,) = self.placed()
        copy = self.copy_path(self.jo_pixel, item)
        copy.unlink()
        os.mkfifo(copy)
        before = share_tree(self.root)
        done = threading.Event()
        found = {}

        def check_then_tend() -> None:
            found["checked"] = self.check()
            found["tended"] = self.tend(at=NOW + 200)
            done.set()
        worker = threading.Thread(target=check_then_tend, daemon=True)
        worker.start()
        finished = done.wait(20)
        if not finished:
            with contextlib.suppress(OSError):
                os.close(os.open(copy, os.O_WRONLY | os.O_NONBLOCK))
        worker.join(20)
        self.assertTrue(finished)
        self.assertEqual(found["checked"].requeued, 1)
        self.assertEqual(found["tended"].failed, 1)
        self.assertEqual(share_tree(self.root), before)
        self.assertTrue(self.held())
        self.assertEqual(len(self.events("pixel_copy_replacement_pending")), 1)

    def test_a_replacement_removed_or_swapped_while_it_is_read_back_keeps_the_hold(self):
        def other_file(path: Path) -> None:
            path.unlink()
            path.write_bytes(b"swapped in")

        def link(path: Path) -> None:
            path.unlink()
            path.symlink_to(self.root / "jo" / "originals" / "2024" / "IMG_0001.HEIC")
        (item,) = self.placed()
        copy = self.copy_path(self.jo_pixel, item)
        real = pixel_folder.paths.fd_sha1
        for name, swap in (("removed", Path.unlink), ("other file", other_file), ("link", link)):
            with self.subTest(name):
                with contextlib.suppress(FileNotFoundError):
                    copy.unlink()
                self.opened.execute(sql_update(PixelCopy).values(state="queued"))
                self.opened.commit()
                calls: list[int] = []

                def swapped_on_read_back(fd: int, calls: list[int] = calls, swap=swap) -> str:
                    digest = real(fd)
                    calls.append(fd)
                    if len(calls) == 2:
                        swap(copy)
                    return digest
                with mock.patch.object(pixel_folder.paths, "fd_sha1", side_effect=swapped_on_read_back):
                    self.assertEqual(self.tend(at=NOW + 200).failed, 1)
                self.assertEqual(len(calls), 2)
                self.assertEqual(self.state(), [("queued", NOW + 10, NOW)])
                self.assertTrue(self.held())

    def assert_one_unreadable_copy_holds_only_its_pixel(self, unreadable):
        first, _ = self.placed(2)
        sams = self.item(self.sam)
        pixel_folder.queue(self.opened, sams, NOW)
        self.tend(at=NOW + 10)
        before = share_tree(self.root)
        with unreadable(self.copy_path(self.jo_pixel, first).name):
            self.assertEqual(self.check(), pixel_check.Checked(checked=3, intact=2, requeued=1))
        self.assertEqual(share_tree(self.root), before)
        self.assertEqual([c.state for c in self.copies(self.jo_pixel)], ["queued", "placed"])
        self.assertTrue(self.held())
        self.assertFalse(pixel_check.held(self.opened, self.sam_pixel, True))
        later = self.item(self.sam)
        pixel_folder.queue(self.opened, later, NOW + 150)
        tended = self.tend(at=NOW + 200)
        self.assertEqual(tended.placed, 2)
        self.assertTrue(self.copy_path(self.sam_pixel, later).is_file())
        self.assertFalse(self.held())

    def test_a_copy_whose_open_is_refused_is_queued_again_and_the_others_carry_on(self):
        real = pixel_folder.paths.open_no_follow

        @contextlib.contextmanager
        def refused(leaf: str):
            def open_refused(name: str, folder: int, flags: int = os.O_RDONLY, mode: int = 0o644) -> int:
                if name == leaf:
                    raise PermissionError(errno.EACCES, "refused")
                return real(name, folder, flags, mode)
            with mock.patch.object(pixel_folder.paths, "open_no_follow", side_effect=open_refused):
                yield
        self.assert_one_unreadable_copy_holds_only_its_pixel(refused)

    def test_a_copy_that_fails_to_read_is_queued_again_and_the_others_carry_on(self):
        real = pixel_folder.paths.fd_sha1

        @contextlib.contextmanager
        def failing(leaf: str):
            calls = []

            def read_fails_once(fd: int) -> str:
                calls.append(fd)
                if len(calls) == 1:
                    raise OSError(errno.EIO, "input/output error")
                return real(fd)
            with mock.patch.object(pixel_folder.paths, "fd_sha1", side_effect=read_fails_once):
                yield
        self.assert_one_unreadable_copy_holds_only_its_pixel(failing)

    def test_a_copy_that_cannot_be_written_back_records_one_pending_event_until_it_is_requeued_again(self):
        (item,) = self.placed()
        original = self.root / item.path
        data = original.read_bytes()
        self.copy_path(self.jo_pixel, item).unlink()
        original.unlink()
        self.check()
        for at in (NOW + 200, NOW + 300, NOW + 400):
            self.assertEqual(self.tend(at=at).failed, 1)
        self.assertEqual(len(self.events("pixel_copy_replacement_pending")), 1)
        original.write_bytes(data)
        self.tend(at=NOW + 500)
        self.copy_path(self.jo_pixel, item).unlink()
        original.unlink()
        self.check(at=NOW + 600)
        self.assertEqual(self.tend(at=NOW + 700).failed, 1)
        self.assertEqual(len(self.events("pixel_copy_replacement_pending")), 2)

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

    def restored_older(self, count: int = 1, always_send: bool = False) -> list:
        items = self.placed(count, always_send=always_send)
        self.opened.execute(sql_update(PixelCopy).values(state="queued", placed_at=None, name=None))
        self.opened.commit()
        return items

    def pixel_folder(self, pixel_id: int | None = None) -> Path:
        return self.root / "_pixels" / str(self.jo_pixel if pixel_id is None else pixel_id)

    def test_a_file_matching_a_queued_copy_is_adopted_with_the_adoption_time_as_its_cutoff(self):
        (item,) = self.restored_older(always_send=True)
        self.match(item, self.jo_account, at=NOW - 100)
        before = share_tree(self.root)
        placed_copies = pixel_check.PlacedCopyCheck()
        self.assertEqual(placed_copies.run(self.opened, self.photos, NOW + 100), pixel_check.Checked(adopted=1))
        self.assertEqual(share_tree(self.root), before)
        self.assertEqual([(c.state, c.placed_at, c.name) for c in self.copies(self.jo_pixel)], [("placed", NOW + 100, f"2024/{Path(item.path).name}")])
        self.assertEqual(self.events("pixel_copy_adopted"), [f"copy {self.copies(self.jo_pixel)[0].id}"])
        self.assertEqual(self.events(pixel_check.UNACCOUNTED), [])
        self.assertFalse(placed_copies.held(self.opened, self.jo_pixel))
        self.assertFalse(placed_copies.due(self.opened, self.photos, NOW + 150))
        tended = self.tend(at=NOW + 200)
        self.assertEqual((tended.placed, tended.skipped, tended.pruned), (0, 0, 0))
        self.assertEqual(share_tree(self.root), before)
        self.assertEqual(self.check(NOW + 300), pixel_check.Checked(checked=1, intact=1))
        self.match(item, self.jo_account, at=NOW + 400)
        self.assertEqual(self.tend(at=NOW + 500).pruned, 1)
        self.assertFalse(self.copy_path(self.jo_pixel, item).exists())

    def test_an_adopted_copy_counts_under_the_cap(self):
        self.restored_older(2)
        self.assertEqual(self.check().adopted, 2)
        self.assertEqual(pixel_folder.used_bytes(self.opened, self.jo_pixel), 200)
        later = self.item(self.jo)
        pixel_folder.queue(self.opened, later, NOW + 150)
        self.assertEqual(self.tend(cap=250, at=NOW + 200).deferred, 1)

    def strays(self) -> list[Path]:
        folder = self.pixel_folder()
        made = [folder / "notes.txt", folder / "jo" / "2024" / "stray.HEIC", folder / "jo" / ".stignore", folder / "elsewhere" / "deep" / "x.jpg"]
        for each in made:
            each.parent.mkdir(parents=True, exist_ok=True)
            each.write_bytes(b"not a copy Darkroom knows")
        return made

    def test_a_file_matching_nothing_holds_the_pixel_untouched_until_it_is_removed_by_hand(self):
        self.placed()
        made = self.strays()
        before = share_tree(self.root)
        placed_copies = pixel_check.PlacedCopyCheck()
        for at in (NOW + 100, NOW + 200):
            self.assertEqual(placed_copies.run(self.opened, self.photos, at), pixel_check.Checked(checked=1, intact=1, unaccounted={self.jo_pixel: 4}))
            self.assertEqual(share_tree(self.root), before)
            self.assertTrue(placed_copies.held(self.opened, self.jo_pixel))
            self.assertFalse(placed_copies.held(self.opened, self.sam_pixel))
            self.assertEqual(placed_copies.held_pixels(self.opened), [self.jo_pixel])
        self.assertEqual(self.events(pixel_check.UNACCOUNTED), ["files 4"])
        for each in made:
            each.unlink()
        self.assertTrue(placed_copies.due(self.opened, self.photos, NOW + 300))
        self.assertEqual(placed_copies.run(self.opened, self.photos, NOW + 300), pixel_check.Checked(checked=1, intact=1))
        self.assertFalse(placed_copies.held(self.opened, self.jo_pixel))
        self.assertFalse(placed_copies.due(self.opened, self.photos, NOW + 400))
        self.assertEqual(self.events(pixel_check.UNACCOUNTED), ["files 4", "files 0"])

    def test_a_narrowed_run_for_another_pixel_keeps_the_stray_files_hold_and_its_folder_unshared(self):
        own, jo_device, sam_device = (syncthing.device_id_of_certificate(f"made-up certificate of {name} in the narrowed check test".encode())
                                      for name in ("darkroom", "jo's pixel", "sam's pixel"))
        fake = FakeSyncthing(self, "narrowed-check-key", own)
        client = syncthing.Client(fake.url, "narrowed-check-key", timeout=5.0)
        self.opened.get(Pixel, self.jo_pixel).syncthing_device_id = jo_device
        self.opened.get(Pixel, self.sam_pixel).syncthing_device_id = sam_device
        self.opened.commit()
        self.placed()
        theirs = self.item(self.sam)
        pixel_folder.queue(self.opened, theirs, NOW)
        self.tend(at=NOW + 20)
        self.strays()
        placed_copies = pixel_check.PlacedCopyCheck()
        self.assertEqual(placed_copies.run(self.opened, self.photos, NOW + 100).unaccounted, {self.jo_pixel: 4})
        listed = placed_copies.listed[self.jo_pixel]
        self.copy_path(self.sam_pixel, theirs).unlink()
        self.assertEqual(placed_copies.run(self.opened, self.photos, NOW + 101).requeued, 1)
        self.assertEqual(placed_copies.scope(self.opened, self.photos, NOW + 200), frozenset({self.sam_pixel}))
        narrowed = placed_copies.run(self.opened, self.photos, NOW + 200)
        self.assertEqual(narrowed.unaccounted, {})
        self.assertTrue(placed_copies.held(self.opened, self.jo_pixel))
        self.assertEqual(placed_copies.unaccounted, {self.jo_pixel: 4})
        self.assertEqual(placed_copies.listed, {self.jo_pixel: listed})
        outcome = transport.reconcile(self.opened, client, self.photos, lambda pixel_id: placed_copies.held(self.opened, pixel_id), None, NOW + 201)
        self.assertIsNone(outcome.failure)
        self.assertNotIn(self.jo_pixel, outcome.resumed)
        self.assertNotIn(transport.folder_of(self.jo_pixel), fake.folders)

    def test_files_matching_nothing_are_rehashed_at_most_daily_until_the_folder_changes_and_hold_the_pixel_throughout(self):
        self.placed()
        made = self.strays()
        placed_copies = pixel_check.PlacedCopyCheck()
        self.assertTrue(placed_copies.due(self.opened, self.photos, NOW + 100))
        self.assertEqual(placed_copies.run(self.opened, self.photos, NOW + 100).unaccounted, {self.jo_pixel: 4})
        day = pixel_check.RECHECK_SECONDS
        for at in (NOW + 100, NOW + 3700, NOW + 100 + day - 1):
            with self.subTest(at=at):
                self.assertFalse(placed_copies.due(self.opened, self.photos, at))
                self.assertTrue(placed_copies.held(self.opened, self.jo_pixel))
                self.assertEqual(placed_copies.held_pixels(self.opened), [self.jo_pixel])
        self.assertTrue(placed_copies.due(self.opened, self.photos, NOW + 100 + day))
        self.assertTrue(placed_copies.due(self.opened, self.photos, NOW + 99))
        (self.pixel_folder() / "jo" / "2024" / "another.HEIC").write_bytes(b"not a copy Darkroom knows")
        self.assertTrue(placed_copies.due(self.opened, self.photos, NOW + 200))
        self.assertEqual(placed_copies.run(self.opened, self.photos, NOW + 200).unaccounted, {self.jo_pixel: 5})
        self.assertFalse(placed_copies.due(self.opened, self.photos, NOW + 300))
        made[0].unlink()
        self.assertTrue(placed_copies.due(self.opened, self.photos, NOW + 300))
        self.assertTrue(placed_copies.held(self.opened, self.jo_pixel))
        self.assertEqual(placed_copies.run(self.opened, self.photos, NOW + 300).unaccounted, {self.jo_pixel: 4})
        self.assertTrue(placed_copies.held(self.opened, self.jo_pixel))
        self.opened.execute(sql_update(PixelCopy).values(state="queued"))
        self.opened.commit()
        self.assertTrue(placed_copies.due(self.opened, self.photos, NOW + 400))

    def test_the_control_entries_are_never_counted(self):
        self.placed()
        folder = self.pixel_folder()
        (folder / ".stignore").write_text("#include .stignore\n")
        (folder / ".stfolder").mkdir()
        (folder / ".stfolder" / "syncthing-folder-1a2b.txt").write_text("marker")
        self.assertEqual(self.check(), pixel_check.Checked(checked=1, intact=1))
        self.assertFalse(pixel_check.PlacedCopyCheck(ran=True).held(self.opened, self.jo_pixel))

    def test_a_changed_file_at_a_queued_copys_name_is_not_adopted_and_holds_the_pixel(self):
        def changed(path: Path) -> None:
            path.write_bytes(b"changed")

        def hard_linked(path: Path) -> None:
            os.link(path, self.root / "jo" / "elsewhere")

        def linked(path: Path) -> None:
            path.unlink()
            path.symlink_to(self.root / "jo" / "originals" / "2024" / "IMG_0001.HEIC")

        def piped(path: Path) -> None:
            path.unlink()
            os.mkfifo(path)
        for name, damage in (("changed", changed), ("hard link", hard_linked), ("link", linked), ("pipe", piped)):
            with self.subTest(name):
                (item,) = self.restored_older()
                damage(self.copy_path(self.jo_pixel, item))
                before = share_tree(self.root)
                self.assertEqual(self.check(), pixel_check.Checked(unaccounted={self.jo_pixel: 1}))
                self.assertEqual(share_tree(self.root), before)
                self.assertEqual(self.state()[-1][:2], ("queued", None))
                self.assertEqual(self.events("pixel_copy_adopted"), [])
                shutil.rmtree(self.pixel_folder())
                with contextlib.suppress(FileNotFoundError):
                    (self.root / "jo" / "elsewhere").unlink()
                self.opened.execute(sql_update(PixelCopy).values(state="pruned"))
                self.opened.commit()

    def test_a_file_swapped_while_it_is_hashed_is_not_adopted(self):
        (item,) = self.restored_older()
        copy = self.copy_path(self.jo_pixel, item)
        real = pixel_folder.paths.fd_sha1

        def swapped(fd: int) -> str:
            digest = real(fd)
            data = copy.read_bytes()
            copy.unlink()
            copy.write_bytes(data)
            return digest
        with mock.patch.object(pixel_folder.paths, "fd_sha1", side_effect=swapped):
            self.assertEqual(self.check(), pixel_check.Checked(unaccounted={self.jo_pixel: 1}))
        self.assertEqual(self.state(), [("queued", None, NOW)])
        self.assertEqual(self.check().adopted, 1)

    def test_a_copy_that_changed_before_its_adoption_lock_is_left_alone(self):
        self.restored_older()
        real = pixel_folder.lock_copy

        def placed_meanwhile(opened, copy_id):
            opened.execute(sql_update(PixelCopy).where(PixelCopy.id == copy_id).values(sha1="0" * 40))
            opened.commit()
            real(opened, copy_id)
        with mock.patch.object(pixel_folder, "lock_copy", side_effect=placed_meanwhile):
            self.assertEqual(self.check(), pixel_check.Checked(unaccounted={self.jo_pixel: 1}))
        self.assertEqual(self.state(), [("queued", None, NOW)])

    def test_a_copy_whose_item_moved_or_whose_pixel_no_longer_serves_the_person_is_not_adopted(self):
        (item,) = self.restored_older()
        with mock.patch.object(pixel_folder, "serves", return_value=False):
            self.assertEqual(self.check().unaccounted, {self.jo_pixel: 1})
        item.sha1 = "0" * 40
        self.opened.commit()
        self.assertEqual(self.check().unaccounted, {self.jo_pixel: 1})
        self.assertEqual(self.state(), [("queued", None, NOW)])

    def test_a_file_that_cannot_be_read_stays_unaccounted_and_the_others_are_still_adopted(self):
        first, _ = self.restored_older(2)
        leaf = self.copy_path(self.jo_pixel, first).name
        real = pixel_folder.paths.open_no_follow

        def open_refused(name: str, folder: int, flags: int = os.O_RDONLY, mode: int = 0o644) -> int:
            if name == leaf:
                raise PermissionError(errno.EACCES, "refused")
            return real(name, folder, flags, mode)
        with mock.patch.object(pixel_folder.paths, "open_no_follow", side_effect=open_refused):
            self.assertEqual(self.check(), pixel_check.Checked(adopted=1, unaccounted={self.jo_pixel: 1}))
        self.assertEqual([c.state for c in self.copies(self.jo_pixel)], ["queued", "placed"])

    def test_an_unreadable_or_linked_pixel_folder_holds_the_pixel(self):
        self.placed()
        sams = self.pixel_folder(self.sam_pixel)
        sams.symlink_to(self.root / "jo" / "originals")
        with mock.patch.object(pixel_folder.paths.os, "listdir", side_effect=OSError(errno.EIO, "input/output error")):
            self.assertEqual(self.check().unaccounted, {self.jo_pixel: 1, self.sam_pixel: 1})
        self.assertEqual(self.check().unaccounted, {self.sam_pixel: 1})

    def test_no_name_of_an_unaccounted_file_reaches_an_event_or_the_database(self):
        canary = "canary-stray-HEIC-5d2e9a71"
        self.placed()
        (self.pixel_folder() / "jo" / "2024" / f"{canary}.HEIC").write_bytes(b"made up")
        (self.pixel_folder() / canary).mkdir()
        (self.pixel_folder() / canary / "a.jpg").write_bytes(b"made up too")
        with no_leaks(self, canary, database=self.opened.get_bind()):
            checked = self.check()
        self.assertEqual(checked.unaccounted, {self.jo_pixel: 2})
        self.assertEqual(self.events(pixel_check.UNACCOUNTED), ["files 2"])


    def test_a_copy_lost_from_a_pixel_that_holds_nothing_is_noticed_by_the_daily_full_run(self):
        (item,) = self.placed()
        placed_copies = pixel_check.PlacedCopyCheck()
        self.assertEqual(placed_copies.run(self.opened, self.photos, NOW + 100), pixel_check.Checked(checked=1, intact=1))
        self.assertEqual(placed_copies.held_pixels(self.opened), [])
        self.copy_path(self.jo_pixel, item).unlink()
        day = pixel_check.RECHECK_SECONDS
        for at in (NOW + 100, NOW + 3700, NOW + 100 + day - 1):
            with self.subTest(at=at):
                self.assertFalse(placed_copies.due(self.opened, self.photos, at))
                self.assertFalse(placed_copies.held(self.opened, self.jo_pixel))
        self.assertTrue(placed_copies.due(self.opened, self.photos, NOW + 100 + day))
        self.assertEqual(placed_copies.scope(self.opened, self.photos, NOW + 100 + day), None)
        self.assertEqual(placed_copies.run(self.opened, self.photos, NOW + 100 + day), pixel_check.Checked(checked=1, intact=0, requeued=1))
        self.assertEqual(placed_copies.ran_at, NOW + 100 + day)
        self.assertTrue(placed_copies.held(self.opened, self.jo_pixel))
        self.assertEqual(self.events("pixel_copy_missing"), [f"copy {self.copies(self.jo_pixel)[0].id}"])

    def test_the_check_object_holds_every_pixel_until_it_has_run(self):
        self.placed()
        placed_copies = pixel_check.PlacedCopyCheck()
        self.assertTrue(placed_copies.due(self.opened, self.photos, NOW + 200))
        self.assertTrue(placed_copies.held(self.opened, self.sam_pixel))
        self.assertEqual(placed_copies.run(self.opened, self.photos, NOW + 100).intact, 1)
        self.assertFalse(placed_copies.due(self.opened, self.photos, NOW + 200))
        self.assertFalse(placed_copies.held(self.opened, self.jo_pixel))
        self.opened.execute(sql_update(PixelCopy).values(state="queued"))
        self.opened.commit()
        self.assertTrue(placed_copies.due(self.opened, self.photos, NOW + 200))
        self.assertEqual(pixel_check.replacing_pixels(self.opened), [self.jo_pixel])
        self.assertTrue(placed_copies.held(self.opened, self.jo_pixel))
        self.assertFalse(placed_copies.held(self.opened, self.sam_pixel))


if __name__ == "__main__":
    unittest.main()
