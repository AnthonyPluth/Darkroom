import ast
import contextlib
import io
import os
import shutil
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import select

import run
from darkroom import config, monitoring
from darkroom.domain import pixel_check, pixel_folder, share_bind, share_check
from darkroom.server import jobs
from darkroom.storage import db, share, stored_settings
from darkroom.storage.models import Event, StoredSetting
from tests import test_pixel_folder
from tests.privacy import no_leaks
from tests.shared import bound_share, own_database_url
from tests.test_pixel_check import share_tree

NOW = test_pixel_folder.NOW
HOUR = jobs.PIXEL_TEND_SECONDS
ROOT = Path(__file__).resolve().parent.parent
OUTSIDE_SYNCTHING: dict[str, str] = {}
OWN_SYNCTHING = {"DARKROOM_SYNCTHING": "1", "DARKROOM_ONE_CONTAINER": "1"}
SYNCTHING_OUTSIDE_THE_ONE_CONTAINER = {"DARKROOM_SYNCTHING": "1"}
OUTSIDE_PROMPT = ("Syncthing is configured by hand, outside Darkroom. Pause it for every folder under _pixels/ on this server now: an empty replacement would otherwise reach each Pixel as deletions. "
                  "Type paused once it is paused: ")
OUTSIDE_REFUSED = "bind-share: the outside Syncthing was not confirmed paused; nothing was written"
OUTSIDE_RESUME = ("Bound. Placing and pruning resume at the next hourly tend (or now with `python run.py jobs pixels.tend`); the placed-copy check first writes back every placed copy that is missing. "
                  "Resume the outside Syncthing for _pixels/ only when Darkroom reports the refill complete: the `pixel folders tended` log line shows held=0.")
OWN_PROMPT = ("Syncthing is Darkroom's own (DARKROOM_SYNCTHING=1), and Darkroom keeps it paused for every Pixel while this share has no sentinel. "
              "No other Syncthing may serve _pixels/ on this server, such as Unraid's Syncthing app or one on the host (docs/deploy/one-container.md, \"Ports already in use\"): "
              "an empty replacement would otherwise reach each Pixel as deletions. Type paused once you have checked that none does: ")
OWN_REFUSED = "bind-share: it was not confirmed that no other Syncthing serves _pixels/; nothing was written"
OWN_RESUME = ("Bound. Placing and pruning resume at the next hourly tend (or now with `python run.py jobs pixels.tend`); the placed-copy check first writes back every placed copy that is missing. "
              "Darkroom resumes each Pixel's folder in its own Syncthing by itself, once the service's hourly tend has checked that Pixel's copies and none is still being written back: there is nothing to resume by hand.")


class ShareWorld(unittest.TestCase):
    account = test_pixel_folder.PixelFolderTests.account
    item = test_pixel_folder.PixelFolderTests.item
    match = test_pixel_folder.PixelFolderTests.match
    copies = test_pixel_folder.PixelFolderTests.copies
    copy_path = test_pixel_folder.PixelFolderTests.copy_path
    tend = test_pixel_folder.PixelFolderTests.tend

    def setUp(self):
        test_pixel_folder.PixelFolderTests.setUp(self)
        self.engine = self.opened.get_bind()
        self.settings = config.Settings(**{**config.load().__dict__, "photos_root": self.root, "pixel_folder_cap_gb": 1})
        self.opened.commit()

    def busy_share(self):
        prunable, missing = self.item(self.jo), self.item(self.jo)
        pixel_folder.queue(self.opened, prunable, NOW)
        pixel_folder.queue(self.opened, missing, NOW + 1)
        self.tend(at=NOW + 10)
        self.match(prunable, self.jo_account, at=NOW + 20)
        self.copy_path(self.jo_pixel, missing).unlink()
        queued = self.item(self.jo)
        pixel_folder.queue(self.opened, queued, NOW + 30)
        self.opened.commit()
        return prunable, missing, queued

    def rows(self):
        self.opened.expire_all()
        return [(c.id, c.state, c.name, c.placed_at, c.pruned_at, c.queued_at, c.sha1) for c in self.copies(self.jo_pixel)]

    def states(self):
        return [c[1] for c in self.rows()]

    def events(self, kind):
        self.opened.expire_all()
        return list(self.opened.execute(select(Event.detail).where(Event.type == kind).order_by(Event.id)).scalars())

    def bind(self, token="a" * 32):
        bound_share(self.engine, self.root, token)

    def scheduler(self):
        return jobs.build(self.settings, self.engine, NOW)

    def assert_refused_untouched(self, scheduler, reason, at=NOW + HOUR):
        rows, tree = self.rows(), share_tree(self.root)
        with mock.patch.object(monitoring, "log", wraps=monitoring.log) as log:
            scheduler.run_one("pixels.tend", at)
        log.assert_any_call("pixel folders not tended: the share check failed", reason=reason)
        self.assertNotIn("pixel folders tended", [call.args[0] for call in log.call_args_list])
        self.assertNotIn("placed copies checked", [call.args[0] for call in log.call_args_list])
        self.assertEqual(self.rows(), rows)
        self.assertEqual(share_tree(self.root), tree)
        self.assertEqual(self.events(share_check.FAILED)[-1], reason)


class ShareCheckTests(ShareWorld):
    def test_a_healthy_share_is_not_refused_and_the_cycle_places_prunes_and_refills(self):
        prunable, missing, queued = self.busy_share()
        self.bind()
        with mock.patch.object(monitoring, "log", wraps=monitoring.log) as log:
            self.scheduler().run_one("pixels.tend", NOW + HOUR)
        self.assertEqual(self.states(), ["pruned", "placed", "placed"])
        self.assertFalse(self.copy_path(self.jo_pixel, prunable).exists())
        self.assertTrue(self.copy_path(self.jo_pixel, missing).is_file())
        self.assertTrue(self.copy_path(self.jo_pixel, queued).is_file())
        log.assert_any_call("pixel folders tended", placed=2, pruned=1, skipped=0, deferred=0, failed=0, refused=0, held=0)
        self.assertEqual(self.events(share_check.FAILED), [])
        self.assertEqual(self.events(share_check.PASSED), [])

    def test_a_missing_sentinel_stops_the_cycle_before_it_changes_a_row_or_a_file(self):
        self.busy_share()
        self.bind()
        (self.root / share.SHARE_SENTINEL_FILE).unlink()
        self.assert_refused_untouched(self.scheduler(), share_check.MISSING)

    def test_a_changed_sentinel_stops_the_cycle(self):
        self.busy_share()
        self.bind()
        (self.root / share.SHARE_SENTINEL_FILE).write_bytes(share.sentinel_content("b" * 32))
        self.assert_refused_untouched(self.scheduler(), share_check.CHANGED)

    def test_a_share_that_was_never_bound_stops_the_cycle(self):
        self.busy_share()
        self.assert_refused_untouched(self.scheduler(), share_check.NOT_BOUND)
        share.write_sentinel(self.root, share.sentinel_content("a" * 32))
        self.assert_refused_untouched(self.scheduler(), share_check.NOT_BOUND, at=NOW + 2 * HOUR)

    def test_a_sentinel_that_is_a_link_a_folder_or_too_long_is_unreadable(self):
        self.busy_share()
        self.bind()
        sentinel = self.root / share.SHARE_SENTINEL_FILE
        elsewhere = self.root.parent / f"{self.root.name}-elsewhere"
        elsewhere.write_bytes(sentinel.read_bytes())
        self.addCleanup(elsewhere.unlink)
        sentinel.unlink()
        sentinel.symlink_to(elsewhere)
        self.assert_refused_untouched(self.scheduler(), share_check.UNREADABLE)
        sentinel.unlink()
        sentinel.mkdir()
        self.assert_refused_untouched(self.scheduler(), share_check.UNREADABLE, at=NOW + 2 * HOUR)
        sentinel.rmdir()
        sentinel.write_bytes(share.sentinel_content("a" * 32) * 20)
        self.assert_refused_untouched(self.scheduler(), share_check.UNREADABLE, at=NOW + 3 * HOUR)

    def test_a_different_filesystem_at_the_root_than_at_startup_stops_the_cycle(self):
        self.busy_share()
        self.bind()
        scheduler = self.scheduler()
        real = share.device_of
        with mock.patch.object(share, "device_of", side_effect=lambda path: -1 if path == self.root else real(path)):
            self.assert_refused_untouched(scheduler, share_check.MOVED)
        with mock.patch.object(share, "device_of", return_value=None):
            self.assertEqual(share_check.ShareCheck.at_startup(self.root).problem(self.opened), share_check.MOVED)

    def test_the_share_taken_away_between_two_cycles_of_a_running_service_marks_nothing_pruned_or_placed(self):
        _prunable, missing, _queued = self.busy_share()
        self.bind()
        scheduler = self.scheduler()
        scheduler.jobs = {"pixels.tend": scheduler.jobs["pixels.tend"]}

        def cycle(at):
            self.assertEqual(scheduler.run_due(at), ["pixels.tend"])
            scheduler.jobs["pixels.tend"].worker.join(30)

        cycle(NOW)
        self.assertEqual(self.states(), ["pruned", "placed", "placed"])
        later = self.item(self.jo)
        pixel_folder.queue(self.opened, later, NOW + 40)
        self.match(missing, self.jo_account, at=NOW + 50)
        self.opened.commit()
        rows = self.rows()
        away = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, away)
        for entry in list(self.root.iterdir()):
            shutil.move(entry, away / entry.name)
        cycle(NOW + HOUR)
        self.assertEqual(self.rows(), rows)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(self.events(share_check.FAILED), [share_check.MISSING])
        for entry in list(away.iterdir()):
            shutil.move(entry, self.root / entry.name)
        cycle(NOW + 2 * HOUR)
        self.assertEqual(self.states(), ["pruned", "pruned", "placed", "placed"])
        self.assertTrue(self.copy_path(self.jo_pixel, later).is_file())
        self.assertEqual(self.events(share_check.PASSED), [None])

    def test_a_share_lost_during_the_placed_copy_pass_is_checked_again_before_pruning_or_placing(self):
        prunable, missing, queued = self.busy_share()
        self.bind()
        scheduler = self.scheduler()
        sentinel = self.root / share.SHARE_SENTINEL_FILE
        real = pixel_check.PlacedCopyCheck.run
        trees = []

        def lose_the_share(check, opened, photos, now):
            found = real(check, opened, photos, now)
            sentinel.unlink()
            trees.append((self.rows(), share_tree(self.root)))
            return found

        with mock.patch.object(pixel_check.PlacedCopyCheck, "run", lose_the_share), mock.patch.object(monitoring, "log", wraps=monitoring.log) as log:
            scheduler.run_one("pixels.tend", NOW + HOUR)
        log.assert_any_call("pixel folders not tended: the share check failed", reason=share_check.MISSING)
        self.assertNotIn("pixel folders tended", [call.args[0] for call in log.call_args_list])
        rows, tree = trees[0]
        self.assertEqual(self.rows(), rows)
        self.assertEqual(share_tree(self.root), tree)
        self.assertTrue(self.copy_path(self.jo_pixel, prunable).is_file())
        self.assertFalse(self.copy_path(self.jo_pixel, missing).exists())
        self.assertFalse(self.copy_path(self.jo_pixel, queued).exists())
        self.assertEqual(self.events(share_check.FAILED), [share_check.MISSING])

    def test_a_share_rebound_during_the_placed_copy_pass_waits_for_the_next_cycle(self):
        self.busy_share()
        self.bind()
        scheduler = self.scheduler()
        real = pixel_check.PlacedCopyCheck.run
        trees = []

        def rebind(check, opened, photos, now):
            found = real(check, opened, photos, now)
            (self.root / share.SHARE_SENTINEL_FILE).unlink()
            self.bind("f" * 32)
            trees.append((self.rows(), share_tree(self.root)))
            return found

        with mock.patch.object(pixel_check.PlacedCopyCheck, "run", rebind), mock.patch.object(monitoring, "log", wraps=monitoring.log) as log:
            scheduler.run_one("pixels.tend", NOW + HOUR)
        log.assert_any_call("pixel folders not tended: the share was bound again during the check; tending at the next cycle")
        self.assertNotIn("pixel folders tended", [call.args[0] for call in log.call_args_list])
        rows, tree = trees[0]
        self.assertEqual((self.rows(), share_tree(self.root)), (rows, tree))
        self.assertFalse(scheduler.placed_copies.ran)
        scheduler.run_one("pixels.tend", NOW + 2 * HOUR)
        self.assertEqual(self.states(), ["pruned", "placed", "placed"])

    def test_a_symlinked_root_retargeted_while_running_is_never_tended_at_the_new_place(self):
        prunable, missing, queued = self.busy_share()
        self.bind()
        holder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, holder)
        link, elsewhere = holder / "photos", holder / "elsewhere"
        elsewhere.mkdir()
        link.symlink_to(self.root)
        self.settings = config.Settings(**{**self.settings.__dict__, "photos_root": link})
        scheduler = self.scheduler()
        self.assertEqual(scheduler.share.root, self.root)
        link.unlink()
        link.symlink_to(elsewhere)
        scheduler.run_one("pixels.tend", NOW + HOUR)
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertEqual(self.states(), ["pruned", "placed", "placed"])
        self.assertFalse(self.copy_path(self.jo_pixel, prunable).exists())
        self.assertTrue(self.copy_path(self.jo_pixel, missing).is_file())
        self.assertTrue(self.copy_path(self.jo_pixel, queued).is_file())

    def test_a_failure_is_recorded_once_until_it_changes_or_passes_and_logs_no_path(self):
        self.bind()
        scheduler = self.scheduler()
        sentinel = self.root / share.SHARE_SENTINEL_FILE
        with no_leaks(self, str(self.root), "a" * 32):
            sentinel.unlink()
            scheduler.run_one("pixels.tend", NOW)
            scheduler.run_one("pixels.tend", NOW + HOUR)
            sentinel.write_bytes(b"something else")
            scheduler.run_one("pixels.tend", NOW + 2 * HOUR)
            sentinel.write_bytes(share.sentinel_content("a" * 32))
            scheduler.run_one("pixels.tend", NOW + 3 * HOUR)
            scheduler.run_one("pixels.tend", NOW + 4 * HOUR)
            sentinel.unlink()
            scheduler.run_one("pixels.tend", NOW + 5 * HOUR)
        self.opened.expire_all()
        found = self.opened.execute(select(Event.type, Event.subject, Event.detail).where(Event.type.in_(share_check.SHARE_EVENTS)).order_by(Event.id)).all()
        self.assertEqual([tuple(row) for row in found], [
            (share_check.FAILED, "share", share_check.MISSING), (share_check.FAILED, "share", share_check.CHANGED),
            (share_check.PASSED, "share", None), (share_check.FAILED, "share", share_check.MISSING),
        ])

    def test_only_a_mount_separate_from_the_containers_own_filesystem_counts(self):
        mountinfo = self.root.parent / f"{self.root.name}-mountinfo"
        self.addCleanup(mountinfo.unlink, missing_ok=True)
        point = str(self.root).replace(" ", "\\040")
        mountinfo.write_text(f"22 1 0:21 / / rw - overlay overlay rw\n90 22 8:17 /photos {point} rw,relatime shared:1 - xfs /dev/md1p1 rw\n")
        real = share.device_of
        with mock.patch.object(share, "MOUNTINFO", mountinfo), mock.patch.object(share.sys, "platform", "linux"):
            with mock.patch.object(share, "device_of", side_effect=lambda path: 1 if path == Path("/") else 2 if path == self.root else real(path)):
                self.assertEqual(share.separate_mount(self.root), share.Mount(self.root, "xfs", "/dev/md1p1"))
                self.assertIsNone(share.separate_mount(self.root / "jo"))
                self.assertIsNone(share.separate_mount(Path("/")))
            with mock.patch.object(share, "device_of", return_value=1):
                self.assertIsNone(share.separate_mount(self.root))
            with mock.patch.object(share, "device_of", return_value=None):
                self.assertIsNone(share.separate_mount(self.root))
            mountinfo.unlink()
            with mock.patch.object(share, "device_of", side_effect=lambda path: 1 if path == Path("/") else 2):
                self.assertIsNone(share.separate_mount(self.root))
        with mock.patch.object(share.sys, "platform", "darwin"):
            self.assertIsNone(share.mount_at(self.root))

    def test_nothing_but_the_bind_command_writes_a_sentinel_or_records_its_identity(self):
        writers = {"write_sentinel": {"darkroom/domain/share_bind.py"}, "record_share": {"darkroom/domain/share_bind.py"}, "bind_share": {"run.py"}}
        found = {name: set() for name in writers}
        for path in [*sorted((ROOT / "darkroom").rglob("*.py")), *sorted((ROOT / "darkroom_browser").rglob("*.py")), ROOT / "run.py"]:
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call):
                    name = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id if isinstance(node.func, ast.Name) else None
                    if name in found:
                        found[name].add(str(path.relative_to(ROOT)))
                if isinstance(node, ast.Constant) and node.value == share.SHARE_SENTINEL_FILE:
                    self.assertEqual(path, ROOT / "darkroom/storage/share.py")
        self.assertEqual(found, writers)


class BindShareTests(ShareWorld):
    def setUp(self):
        super().setUp()
        url = self.engine.url.render_as_string(hide_password=False)
        data = tempfile.TemporaryDirectory()
        self.addCleanup(data.cleanup)
        self.environment = {"DARKROOM_PHOTOS_ROOT": str(self.root), "DARKROOM_DATA_DIR": data.name, "DATABASE_URL": url}
        self.mount = share.Mount(self.root, "xfs", "/dev/md1p1")

    def run_bind(self, answers="paused\nbind\n", mounted=True, setup=OUTSIDE_SYNCTHING):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, {**self.environment, **setup}, clear=True))
            stack.enter_context(mock.patch("sys.stdin", io.StringIO(answers)))
            out, err = stack.enter_context(contextlib.redirect_stdout(io.StringIO())), stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            if mounted:
                stack.enter_context(mock.patch.object(share, "separate_mount", return_value=self.mount))
            code = run.main(["bind-share"])
        return code, out.getvalue(), err.getvalue()

    def recorded(self):
        self.opened.expire_all()
        return stored_settings.recorded_share(self.opened)

    def assert_refused(self, found, message, sentinel_before=False):
        code, _out, err = found
        self.assertEqual(code, 1)
        self.assertIn(message, err)
        self.assertEqual(os.path.lexists(self.root / share.SHARE_SENTINEL_FILE), sentinel_before)
        self.assertEqual(self.events(share_check.BOUND), [])

    def test_a_confirmed_bind_writes_a_sentinel_records_it_and_the_cycles_resume_and_refill(self):
        _prunable, missing, _queued = self.busy_share()
        before = self.rows()
        code, out, err = self.run_bind()
        self.assertEqual((code, err), (0, '{"event": "share bound"}\n'))
        self.assertIn(f"Share root: {self.root}", out)
        self.assertIn("Mounted from: /dev/md1p1 (xfs)", out)
        self.assertIn("Pixel copies recorded as placed: 2", out)
        self.assertIn("held=0", out)
        token = self.recorded()
        self.assertRegex(token, r"^[0-9a-f]{32}$")
        self.assertEqual((self.root / share.SHARE_SENTINEL_FILE).read_bytes(), share.sentinel_content(token))
        self.assertEqual(self.events(share_check.BOUND), [None])
        self.assertEqual(self.rows(), before)
        self.scheduler().run_one("pixels.tend", NOW + HOUR)
        self.assertTrue(self.copy_path(self.jo_pixel, missing).is_file())
        self.assertEqual([(c[1], c[3]) for c in self.rows()], [("pruned", NOW + 10), ("placed", NOW + 10), ("placed", NOW + HOUR)])
        self.assertEqual(self.events("pixel_copy_replaced"), [f"copy {before[1][0]}"])

    def test_a_replacement_share_is_refilled_after_the_bind(self):
        _prunable, missing, queued = self.busy_share()
        self.assertEqual(self.run_bind()[0], 0)
        scheduler = self.scheduler()
        scheduler.run_one("pixels.tend", NOW + HOUR)
        old = self.recorded()
        shutil.rmtree(self.root / "_pixels")
        (self.root / share.SHARE_SENTINEL_FILE).unlink()
        self.assert_refused_untouched(scheduler, share_check.MISSING, at=NOW + 2 * HOUR)
        code, _, err = self.run_bind()
        self.assertEqual((code, err), (0, '{"event": "share bound"}\n'))
        self.assertNotEqual(self.recorded(), old)
        with mock.patch.object(monitoring, "log", wraps=monitoring.log) as log:
            scheduler.run_one("pixels.tend", NOW + 3 * HOUR)
        log.assert_any_call("pixel folders tended", placed=2, pruned=0, skipped=0, deferred=0, failed=0, refused=0, held=0)
        self.assertTrue(self.copy_path(self.jo_pixel, missing).is_file())
        self.assertTrue(self.copy_path(self.jo_pixel, queued).is_file())
        self.assertEqual(self.states(), ["pruned", "placed", "placed"])
        self.assertEqual(self.events(share_check.PASSED), [])

    def test_the_bind_is_refused_without_the_placed_copy_check_in_the_tend_cycle(self):
        real = jobs.build

        def without_the_check(*args, **kwargs):
            built = real(*args, **kwargs)
            built.placed_copies = None
            return built

        with mock.patch.object(jobs, "build", side_effect=without_the_check):
            self.assert_refused(self.run_bind(), share_bind.NOT_WIRED)

        def without_the_tend(*args, **kwargs):
            built = real(*args, **kwargs)
            del built.jobs["pixels.tend"]
            return built

        with mock.patch.object(jobs, "build", side_effect=without_the_tend):
            self.assert_refused(self.run_bind(), share_bind.NOT_WIRED)
        self.assertIsNone(self.recorded())

    def test_the_bind_is_refused_unless_the_operator_confirms_the_outside_syncthing_is_paused(self):
        for setup, refused, other in ((OUTSIDE_SYNCTHING, OUTSIDE_REFUSED, OWN_REFUSED), (OWN_SYNCTHING, OWN_REFUSED, OUTSIDE_REFUSED)):
            for answers in ("", "\nbind\n", "yes\nbind\n", "bind\nbind\n"):
                with self.subTest(setup=setup, answers=answers):
                    found = self.run_bind(answers, setup=setup)
                    self.assert_refused(found, refused)
                    self.assertNotIn(other, found[2])
                    self.assertNotIn("Type bind", found[1])
        self.assertIsNone(self.recorded())

    def test_with_darkroom_s_own_syncthing_the_prompts_say_darkroom_pauses_it(self):
        code, out, err = self.run_bind(setup=OWN_SYNCTHING)
        self.assertEqual((code, err), (0, '{"event": "share bound"}\n'))
        self.assertIn(OWN_PROMPT + "Type bind to write a new sentinel on this share and record it: ", out)
        self.assertIn(OWN_RESUME, out)
        for outside in ("configured by hand", "Pause it", "Resume the outside Syncthing"):
            self.assertNotIn(outside, out)
        self.assertRegex(self.recorded(), r"^[0-9a-f]{32}$")

    def test_with_an_outside_syncthing_the_prompts_ask_the_operator_to_pause_and_resume_it(self):
        for setup in (OUTSIDE_SYNCTHING, SYNCTHING_OUTSIDE_THE_ONE_CONTAINER):
            with self.subTest(setup=setup):
                (self.root / share.SHARE_SENTINEL_FILE).unlink(missing_ok=True)
                code, out, err = self.run_bind(setup=setup)
                self.assertEqual((code, err), (0, '{"event": "share bound"}\n'))
                self.assertIn(OUTSIDE_PROMPT + "Type bind to write a new sentinel on this share and record it: ", out)
                self.assertIn(OUTSIDE_RESUME, out)
                for own in ("Darkroom's own", "No other Syncthing", "nothing to resume by hand"):
                    self.assertNotIn(own, out)

    def test_the_bind_is_refused_on_the_containers_own_filesystem(self):
        self.assert_refused(self.run_bind(mounted=False), share_bind.NOT_A_MOUNT)
        with mock.patch.object(share, "device_of", return_value=None):
            self.assert_refused(self.run_bind(), share_bind.NOT_A_MOUNT)
        self.assertIsNone(self.recorded())

    def test_the_bind_is_refused_without_the_typed_confirmation(self):
        for answers in ("paused\n", "paused\nyes\n", "paused\nBIND please\n"):
            with self.subTest(answers=answers):
                self.assert_refused(self.run_bind(answers), "not confirmed; nothing was written")
        self.assertIsNone(self.recorded())

    def test_a_share_replaced_and_bound_between_two_cycles_is_checked_for_missing_copies_again(self):
        _prunable, missing, queued = self.busy_share()
        self.assertEqual(self.run_bind()[0], 0)
        scheduler = self.scheduler()
        scheduler.run_one("pixels.tend", NOW + HOUR)
        self.assertEqual(self.states(), ["pruned", "placed", "placed"])
        shutil.rmtree(self.root / "_pixels")
        (self.root / share.SHARE_SENTINEL_FILE).unlink()
        self.assertEqual(self.run_bind()[0], 0)
        with mock.patch.object(monitoring, "log", wraps=monitoring.log) as log:
            scheduler.run_one("pixels.tend", NOW + 2 * HOUR)
        log.assert_any_call("placed copies checked", checked=2, intact=0, requeued=2, adopted=0, unaccounted=0, held=1)
        log.assert_any_call("pixel folders tended", placed=2, pruned=0, skipped=0, deferred=0, failed=0, refused=0, held=0)
        self.assertTrue(self.copy_path(self.jo_pixel, missing).is_file())
        self.assertTrue(self.copy_path(self.jo_pixel, queued).is_file())
        self.assertEqual(self.events(share_check.FAILED), [])
        with mock.patch.object(monitoring, "log", wraps=monitoring.log) as log:
            scheduler.run_one("pixels.tend", NOW + 3 * HOUR)
        self.assertNotIn("placed copies checked", [call.args[0] for call in log.call_args_list])

    def test_a_sentinel_whose_contents_cannot_be_written_is_removed_so_the_bind_can_run_again(self):
        self.busy_share()
        with mock.patch.object(share.os, "write", side_effect=OSError(28, "No space left on device")):
            self.assert_refused(self.run_bind(), "the sentinel could not be written (OSError)")
        self.assertFalse(os.path.lexists(self.root / share.SHARE_SENTINEL_FILE))
        code, _, err = self.run_bind()
        self.assertEqual((code, err), (0, '{"event": "share bound"}\n'))
        self.assertEqual((self.root / share.SHARE_SENTINEL_FILE).read_bytes(), share.sentinel_content(self.recorded()))
        self.assertIsNone(share_check.sentinel_problem(self.root, self.recorded()))

    def test_a_failed_write_never_removes_a_sentinel_that_was_put_in_its_place(self):
        sentinel = self.root / share.SHARE_SENTINEL_FILE
        aside = self.root.parent / f"{self.root.name}-aside"
        self.addCleanup(aside.unlink, missing_ok=True)

        def substitute(fd):
            sentinel.rename(aside)
            sentinel.write_bytes(b"someone else's")
            raise OSError(5, "I/O error")

        with mock.patch.object(share.os, "fsync", side_effect=substitute), self.assertRaises(OSError):
            share.write_sentinel(self.root, share.sentinel_content("e" * 32))
        self.assertEqual(sentinel.read_bytes(), b"someone else's")
        sentinel.unlink()
        with mock.patch.object(share.os, "fsync", side_effect=OSError(5, "I/O error")), self.assertRaises(OSError):
            share.write_sentinel(self.root, share.sentinel_content("e" * 32))
        self.assertFalse(os.path.lexists(sentinel))

        real_fsync = share.os.fsync

        def folder_fsync_fails(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(5, "I/O error")
            real_fsync(fd)

        with mock.patch.object(share.os, "fsync", side_effect=folder_fsync_fails), self.assertRaises(OSError):
            share.write_sentinel(self.root, share.sentinel_content("e" * 32))
        self.assertFalse(os.path.lexists(sentinel))

        def vanish(fd):
            sentinel.unlink()
            raise OSError(5, "I/O error")

        with mock.patch.object(share.os, "fsync", side_effect=vanish), self.assertRaises(OSError):
            share.write_sentinel(self.root, share.sentinel_content("e" * 32))
        self.assertFalse(os.path.lexists(sentinel))

    def test_the_bind_is_refused_when_another_sentinel_is_present(self):
        for make in (lambda path: path.write_bytes(b"anything"), lambda path: path.symlink_to("/nowhere"), lambda path: path.mkdir()):
            sentinel = self.root / share.SHARE_SENTINEL_FILE
            make(sentinel)
            with self.subTest(made=sentinel.is_symlink()):
                self.assert_refused(self.run_bind(), share_bind.ANOTHER_SENTINEL, sentinel_before=True)
            sentinel.rmdir() if sentinel.is_dir() and not sentinel.is_symlink() else sentinel.unlink()
        self.assertIsNone(self.recorded())

    def test_the_bind_is_refused_before_the_database_is_set_up(self):
        self.environment["DATABASE_URL"] = f"sqlite:///{self.root.parent / (self.root.name + '-empty.db')}"
        self.addCleanup(Path(self.environment["DATABASE_URL"].removeprefix("sqlite:///")).unlink, missing_ok=True)
        self.assert_refused(self.run_bind(), "the database has no settings table yet")

    def test_a_sentinel_that_cannot_be_written_leaves_the_cycles_stopped_and_the_command_can_run_again(self):
        self.busy_share()
        with mock.patch.object(share, "write_sentinel", side_effect=PermissionError(13, "denied")):
            self.assert_refused(self.run_bind(), "the sentinel could not be written (PermissionError)")
        self.assertIsNotNone(self.recorded())
        self.assert_refused_untouched(self.scheduler(), share_check.MISSING)
        self.assertEqual(self.run_bind()[0], 0)
        self.assertEqual(self.events(share_check.BOUND), [None])

    def test_the_share_changing_while_the_operator_confirms_refuses_the_bind(self):
        found = share_bind.Found(self.root, self.mount, -1, 0, 0, 0)
        with mock.patch.object(share, "separate_mount", return_value=self.mount), share.bind_lock(Path(self.environment["DARKROOM_DATA_DIR"])) as held:
            with self.assertRaises(share_bind.Refused):
                share_bind.bind_share(self.opened, found, NOW, held)
        self.assertIsNone(self.recorded())
        self.assertFalse((self.root / share.SHARE_SENTINEL_FILE).exists())

    def test_a_bind_refuses_while_another_bind_holds_the_lock_and_works_once_it_is_released(self):
        data = Path(self.environment["DARKROOM_DATA_DIR"])
        with share.bind_lock(data):
            self.assert_refused(self.run_bind(), "another bind-share is running in this container; nothing was written")
        self.assertIsNone(self.recorded())
        self.assertEqual(self.run_bind()[0], 0)
        self.assertIsNone(share_check.sentinel_problem(self.root, self.recorded()))
        self.assertEqual(stat.S_IMODE(os.stat(data / share.BIND_LOCK_FILE).st_mode), 0o600)

    def test_two_overlapping_binds_leave_one_identity_that_matches_the_sentinel(self):
        data = Path(self.environment["DARKROOM_DATA_DIR"])
        surveyed, tried = threading.Event(), threading.Event()
        outcome = {}

        def first():
            with share.bind_lock(data) as held, db.Session(self.engine, expire_on_commit=False) as opened:
                found = share_bind.survey(opened, self.root, True, held)
                surveyed.set()
                tried.wait(30)
                share_bind.bind_share(opened, found, NOW, held)
                outcome["first"] = "bound"

        def second():
            surveyed.wait(30)
            try:
                with share.bind_lock(data):
                    outcome["second"] = "entered"
            except share.BindBusy:
                outcome["second"] = "refused"
            finally:
                tried.set()

        with mock.patch.object(share, "separate_mount", return_value=self.mount):
            threads = [threading.Thread(target=first), threading.Thread(target=second)]
            for each in threads:
                each.start()
            for each in threads:
                each.join(60)
        self.assertEqual(outcome, {"first": "bound", "second": "refused"})
        self.assertIsNone(share_check.sentinel_problem(self.root, self.recorded()))
        self.assertEqual(self.events(share_check.BOUND), [None])

    def test_a_bind_after_another_binds_failed_write_still_works(self):
        data = Path(self.environment["DARKROOM_DATA_DIR"])
        with mock.patch.object(share.os, "write", side_effect=OSError(28, "No space left on device")):
            self.assert_refused(self.run_bind(), "the sentinel could not be written (OSError)")
        with share.bind_lock(data):
            pass
        self.assertEqual(self.run_bind()[0], 0)
        self.assertIsNone(share_check.sentinel_problem(self.root, self.recorded()))

    def test_a_data_folder_that_cannot_hold_the_lock_refuses_the_bind(self):
        self.environment["DARKROOM_DATA_DIR"] = str(self.root.parent / f"{self.root.name}-no-such-folder")
        self.assert_refused(self.run_bind(), "the lock in DARKROOM_DATA_DIR could not be taken (FileNotFoundError)")

    def test_the_record_is_kept_apart_from_the_settings_the_environment_can_set(self):
        self.assertNotIn("share_sentinel", stored_settings.BY_VARIABLE)
        with self.assertRaises(stored_settings.NotStorable):
            stored_settings.store(self.opened, "share_sentinel", "x", "key", NOW)
        stored_settings.record_share(self.opened, "c" * 32, NOW)
        self.opened.commit()
        self.assertEqual(stored_settings.effective(self.settings, self.engine).recorded, {"share_sentinel": "c" * 32})
        self.assertEqual(self.opened.get(StoredSetting, "share_sentinel").value_sealed, None)


class CheckConfigTests(ShareWorld):
    def check_config(self):
        data = tempfile.TemporaryDirectory()
        self.addCleanup(data.cleanup)
        url = own_database_url(self)
        engine = db.make_engine(url)
        db.Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        values = {"DARKROOM_PHOTOS_ROOT": str(self.root), "DARKROOM_DATA_DIR": data.name, "DATABASE_URL": url, "DARKROOM_SECRET_KEY": "k"}
        return values, engine

    def run_check(self, values):
        with mock.patch.dict(os.environ, values, clear=True), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err, \
                mock.patch.object(run.exiftool, "version", return_value="13.0"):
            run.main(["check-config"])
        return err.getvalue()

    def test_check_config_warns_about_a_missing_or_changed_sentinel_and_is_quiet_once_bound(self):
        values, engine = self.check_config()
        self.assertIn(f"config: warning: the working share is not bound ({share_check.NOT_BOUND})", self.run_check(values))
        bound_share(engine, self.root, "d" * 32)
        self.assertNotIn("the working share is not bound", self.run_check(values))
        (self.root / share.SHARE_SENTINEL_FILE).unlink()
        self.assertIn(f"({share_check.MISSING})", self.run_check(values))
        (self.root / share.SHARE_SENTINEL_FILE).write_bytes(b"other")
        self.assertIn(f"({share_check.CHANGED})", self.run_check(values))
        self.assertIn("python run.py bind-share", self.run_check(values))

    def test_check_config_says_nothing_about_the_share_of_a_demo(self):
        values, _ = self.check_config()
        self.assertNotIn("the working share is not bound", self.run_check({**values, "DARKROOM_DEMO": "1"}))

    def test_check_config_says_nothing_about_the_share_when_the_database_cannot_be_read(self):
        values, _ = self.check_config()
        with mock.patch.object(stored_settings, "effective", side_effect=stored_settings.OperationalError("x", None, Exception())):
            err = self.run_check(values)
        self.assertIn(stored_settings.NOT_READ, err)
        self.assertNotIn("the working share is not bound", err)


if __name__ == "__main__":
    unittest.main()
