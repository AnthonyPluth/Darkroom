import ast
import errno
import hashlib
import io
import os
import shutil
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import run
from darkroom.storage import browser_copy

ROOT = Path(__file__).resolve().parent.parent
STAMP = "20261114T120000Z"
XATTR_CALLS = ("getxattr", "setxattr", "listxattr", "removexattr")
SHUTIL_COPIES = ("copy", "copy2", "copyfile", "copymode", "copystat", "copytree", "move")


def refused(*_args, **_kwargs):
    raise PermissionError(errno.EPERM, "Operation not permitted")


def tree(root):
    found = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        info = path.lstat()
        if path.is_symlink():
            found[relative] = ("link", os.readlink(path))
        elif path.is_dir():
            found[relative] = ("folder", info.st_mode & 0o7777)
        else:
            found[relative] = ("file", info.st_mode & 0o7777, hashlib.sha256(path.read_bytes()).hexdigest(), info.st_mtime_ns)
    return found


class CopyWorld(unittest.TestCase):
    def setUp(self):
        share = tempfile.TemporaryDirectory()
        self.addCleanup(share.cleanup)
        appdata = tempfile.TemporaryDirectory()
        self.addCleanup(appdata.cleanup)
        self.share = Path(share.name)
        self.share_root = self.share
        self.appdata = Path(appdata.name)
        self.source = self.share / "_browser"
        self.target = self.appdata / "browser"
        profile = self.source / "1" / "profile"
        (profile / "Default" / "Local Storage" / "leveldb").mkdir(parents=True)
        (profile / "Default" / "Cookies").write_bytes(b"encrypted cookie jar" * 500)
        os.chmod(profile / "Default" / "Cookies", 0o600)
        (profile / "Default" / "Local Storage" / "leveldb" / "000003.log").write_bytes(os.urandom(3 * browser_copy.CHUNK + 17))
        (profile / "Local State").write_text("{}")
        (profile / "SingletonLock").symlink_to("host-1234")
        (profile / "SingletonSocket").symlink_to("/tmp/.org.chromium.Chromium.x/SingletonSocket")
        (profile / "SingletonCookie").symlink_to("123")
        (self.source / "1" / "failures" / "4").mkdir(parents=True)
        (self.source / "1" / "failures" / "4" / "screenshot.png").write_bytes(b"\x89PNG")
        os.chmod(profile / "Default", 0o700)
        os.chmod(self.source / "1", 0o2770)
        self.before = tree(self.source)

    def copy(self, set_aside=False):
        return browser_copy.copy_browser_state(self.source, self.target, STAMP, self.share_root, set_aside)

    def assert_source_untouched(self):
        self.assertEqual(tree(self.source), self.before)


class CopyTests(CopyWorld):
    def test_the_copy_matches_every_entry_mode_and_checksum_and_leaves_the_source_alone(self):
        copied = self.copy()
        self.assertEqual((copied.files, copied.locks_left_out, copied.set_aside), (4, 3, None))
        expected = {name: entry for name, entry in self.before.items() if entry[0] != "link"}
        self.assertEqual(tree(self.target), expected)
        self.assert_source_untouched()
        self.assertEqual(browser_copy.leftovers(self.target), [])

    def test_the_copy_works_where_the_deployment_profile_refuses_extended_attributes(self):
        with ExitStack() as stack:
            for name in XATTR_CALLS:
                if hasattr(os, name):
                    stack.enter_context(mock.patch.object(os, name, refused))
            for name in SHUTIL_COPIES:
                stack.enter_context(mock.patch.object(shutil, name, refused))
            self.copy()
        self.assertEqual(tree(self.target), {name: entry for name, entry in self.before.items() if entry[0] != "link"})
        self.assert_source_untouched()

    def test_the_copy_calls_nothing_that_touches_extended_attributes(self):
        source = (ROOT / "darkroom/storage/browser_copy.py").read_text()
        used = {node.attr for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Attribute)}
        self.assertFalse(used & {*XATTR_CALLS, "copy2", "copytree", "copystat", "copymode", "copyfile", "move"})
        self.assertNotIn("import shutil", source)

    def test_an_existing_target_is_refused_and_nothing_is_written(self):
        self.target.mkdir()
        (self.target / "newer").write_bytes(b"kept")
        with self.assertRaises(browser_copy.CopyRefused):
            self.copy()
        self.assertEqual(sorted(each.name for each in self.appdata.iterdir()), ["browser"])
        self.assertEqual((self.target / "newer").read_bytes(), b"kept")
        self.assert_source_untouched()

    def test_set_aside_renames_the_existing_target_after_the_copy_is_verified(self):
        self.target.mkdir()
        (self.target / "newer").write_bytes(b"kept")
        copied = self.copy(set_aside=True)
        self.assertEqual(copied.set_aside, self.appdata / f"browser.before-{STAMP}")
        self.assertEqual((copied.set_aside / "newer").read_bytes(), b"kept")
        self.assertTrue((self.target / "1" / "profile" / "Default" / "Cookies").is_file())
        self.assert_source_untouched()

    def test_a_copy_that_differs_is_not_put_in_place_and_is_left_for_the_owner(self):
        real = browser_copy.copy_file

        def damaged(source, target, found):
            real(source, target, found)
            if source.name == "Cookies":
                target.write_bytes(b"truncated")

        with mock.patch.object(browser_copy, "copy_file", damaged), self.assertRaises(browser_copy.CopyDiffers):
            self.copy()
        self.assertFalse(self.target.exists())
        self.assertEqual(len(browser_copy.leftovers(self.target)), 1)
        self.assert_source_untouched()
        self.copy()
        self.assertTrue(self.target.is_dir())
        self.assertEqual(len(browser_copy.leftovers(self.target)), 1)

    def test_an_interrupted_copy_leaves_the_source_and_no_target(self):
        with mock.patch.object(browser_copy, "copy_file", side_effect=OSError(errno.ENOSPC, "No space left on device")), self.assertRaises(OSError):
            self.copy()
        self.assertFalse(self.target.exists())
        self.assert_source_untouched()

    def test_a_link_other_than_chromiums_locks_or_a_special_file_is_refused(self):
        (self.source / "1" / "profile" / "Default" / "elsewhere").symlink_to(self.share)
        before = tree(self.source)
        with self.assertRaises(browser_copy.CopyRefused):
            self.copy()
        self.assertEqual((list(self.appdata.iterdir()), tree(self.source)), ([], before))

    def test_profiles_that_change_while_they_are_copied_are_not_put_in_place(self):
        real = browser_copy.copy_file

        def worker_still_running(source, target, found):
            real(source, target, found)
            if source.name == "Local State":
                source.write_text('{"changed": true}')

        with mock.patch.object(browser_copy, "copy_file", worker_still_running), self.assertRaisesRegex(browser_copy.CopyDiffers, "changed while"):
            self.copy()
        self.assertFalse(self.target.exists())

    def test_a_linked_source_or_target_and_a_copy_into_itself_are_refused(self):
        linked = self.share / "linked"
        linked.symlink_to(self.source, target_is_directory=True)
        for source, target in ((linked, self.target), (self.source, self.source / "1" / "inside"), (self.source, self.appdata / "missing" / "browser")):
            with self.subTest(source=source.name, target=str(target)), self.assertRaises(browser_copy.CopyRefused):
                browser_copy.copy_browser_state(source, target, STAMP, self.share_root)
        self.target.symlink_to(self.share, target_is_directory=True)
        with self.assertRaises(browser_copy.CopyRefused):
            self.copy(set_aside=True)
        self.assert_source_untouched()

    def test_the_copy_back_to_the_share_sets_the_stale_profiles_aside_and_deletes_nothing(self):
        self.copy()
        (self.target / "2" / "profile").mkdir(parents=True)
        (self.target / "2" / "profile" / "Cookies").write_bytes(b"added in the one container")
        copied = browser_copy.copy_browser_state(self.target, self.source, STAMP, self.share_root, set_aside=True)
        self.assertEqual(tree(copied.set_aside), self.before)
        self.assertEqual((self.source / "2" / "profile" / "Cookies").read_bytes(), b"added in the one container")
        self.assertEqual(tree(self.source), tree(self.target))


NOBODY = 65534


def as_nobody(work):
    if os.geteuid() != 0:
        return work()
    child = os.fork()
    if child == 0:
        code = 1
        try:
            os.setgroups([])
            os.setgid(NOBODY)
            os.setuid(NOBODY)
            work()
            code = 0
        except browser_copy.Unreadable as error:
            code = 3 if str(error) == browser_copy.UNREADABLE else 4
        except BaseException:
            code = 5
        finally:
            os._exit(code)
    _, status = os.waitpid(child, 0)
    code = os.waitstatus_to_exitcode(status)
    if code == 3:
        raise browser_copy.Unreadable(browser_copy.UNREADABLE)
    if code:
        raise AssertionError(f"the copy as an unprivileged user ended with {code}")
    return None


class UnreadableTests(CopyWorld):
    def setUp(self):
        super().setUp()
        if os.geteuid() == 0:
            for top in (self.share, self.appdata):
                os.chmod(top, 0o755)
                for path in [top, *top.rglob("*")]:
                    os.lchown(path, NOBODY, NOBODY)

    def everything_in(self, top):
        return sorted(str(path.relative_to(top)) for path in top.rglob("*"))

    def assert_refused_and_nothing_published(self, source, target, set_aside=False):
        before = self.everything_in(target.parent)
        with self.assertRaises(browser_copy.Unreadable) as raised:
            as_nobody(lambda: browser_copy.copy_browser_state(source, target, STAMP, self.share_root, set_aside))
        self.assertEqual(str(raised.exception), browser_copy.UNREADABLE)
        self.assertNotIn(str(self.share), str(raised.exception))
        self.assertEqual(self.everything_in(target.parent), before)

    def test_an_unreadable_folder_in_the_profiles_stops_the_copy_and_publishes_nothing(self):
        os.chmod(self.source / "1" / "profile" / "Default", 0o000)
        self.addCleanup(os.chmod, self.source / "1" / "profile" / "Default", 0o700)
        self.assert_refused_and_nothing_published(self.source, self.target)
        self.assertFalse(self.target.exists())

    def test_an_unreadable_file_in_the_profiles_stops_the_copy_and_publishes_nothing(self):
        os.chmod(self.source / "1" / "profile" / "Default" / "Cookies", 0o000)
        self.assert_refused_and_nothing_published(self.source, self.target)
        self.assertFalse(self.target.exists())

    def test_a_folder_that_can_be_listed_but_not_entered_stops_the_copy(self):
        os.chmod(self.source / "1" / "profile" / "Default", 0o444)
        self.addCleanup(os.chmod, self.source / "1" / "profile" / "Default", 0o700)
        self.assert_refused_and_nothing_published(self.source, self.target)
        self.assertFalse(self.target.exists())

    def test_an_unreadable_folder_stops_the_copy_back_and_the_share_keeps_its_profiles(self):
        back = self.appdata / "browser"
        (back / "2" / "profile" / "Default").mkdir(parents=True)
        (back / "2" / "profile" / "Default" / "Cookies").write_bytes(b"signed in")
        if os.geteuid() == 0:
            for path in [back, *back.rglob("*")]:
                os.lchown(path, NOBODY, NOBODY)
        os.chmod(back / "2" / "profile" / "Default", 0o000)
        self.addCleanup(os.chmod, back / "2" / "profile" / "Default", 0o700)
        self.assert_refused_and_nothing_published(back, self.source, set_aside=True)
        self.assert_source_untouched()
        self.assertEqual(browser_copy.leftovers(self.source), [])


class UnreadableMidwayTests(CopyWorld):
    def failing_on(self, folder, call):
        real = os.scandir
        seen = []

        def scandir(path):
            if Path(path) == folder:
                seen.append(path)
                if len(seen) == call:
                    raise PermissionError(errno.EACCES, "Permission denied")
            return real(path)

        return mock.patch.object(browser_copy.os, "scandir", scandir)

    def test_a_folder_unreadable_during_the_copy_leaves_only_the_reported_hidden_copy(self):
        with self.failing_on(self.source / "1" / "profile" / "Default", 2), self.assertRaises(browser_copy.Unreadable):
            self.copy()
        self.assertFalse(self.target.exists())
        found = browser_copy.leftovers(self.target)
        self.assertEqual(len(found), 1)
        self.assertEqual(sorted(each.name for each in self.appdata.iterdir()), [found[0].name])
        self.assert_source_untouched()

    def test_a_folder_unreadable_when_the_source_is_listed_again_publishes_nothing(self):
        with self.failing_on(self.source / "1" / "profile" / "Default", 3), self.assertRaises(browser_copy.Unreadable):
            self.copy()
        self.assertFalse(self.target.exists())
        self.assertEqual(len(browser_copy.leftovers(self.target)), 1)

    def test_a_folder_unreadable_in_the_copy_publishes_nothing(self):
        real = os.scandir

        def scandir(path):
            if Path(path).name == "Default" and browser_copy.COPYING in str(path):
                raise PermissionError(errno.EACCES, "Permission denied")
            return real(path)

        with mock.patch.object(browser_copy.os, "scandir", scandir), self.assertRaises(browser_copy.Unreadable):
            self.copy()
        self.assertFalse(self.target.exists())

    def test_the_command_reports_an_unreadable_profile_without_naming_a_path(self):
        out, err = io.StringIO(), io.StringIO()
        with self.failing_on(self.source / "1" / "profile" / "Default", 1), redirect_stdout(out), redirect_stderr(err):
            code = run.main(["copy-browser-state", "--source", str(self.source), "--target", str(self.target)])
        self.assertEqual(code, 1)
        self.assertIn(browser_copy.UNREADABLE, err.getvalue())
        self.assertNotIn("Default", err.getvalue())
        self.assertFalse(self.target.exists())


class InsideTheShareTests(CopyWorld):
    def setUp(self):
        super().setUp()
        self.original = self.share / "jo" / "originals" / "a.jpg"
        self.original.parent.mkdir(parents=True)
        self.original.write_bytes(b"an original")
        (self.share / "_pixels" / "1" / "jo").mkdir(parents=True)
        self.share_tree = tree(self.share)
        self.appdata_tree = tree(self.appdata)

    def assert_nothing_changed(self):
        self.assertEqual(tree(self.share), self.share_tree)
        self.assertEqual(tree(self.appdata), self.appdata_tree)
        self.assertEqual(self.original.read_bytes(), b"an original")

    def refused(self, source, target, set_aside):
        with self.assertRaises(browser_copy.CopyRefused) as raised:
            browser_copy.copy_browser_state(source, target, STAMP, self.share, set_aside)
        self.assertEqual(str(raised.exception), browser_copy.IN_THE_SHARE)
        self.assertNotIn(str(self.share), str(raised.exception))
        self.assert_nothing_changed()

    def test_a_target_inside_the_photo_share_other_than_its_browser_folder_is_refused_and_nothing_moves(self):
        linked = self.appdata / "linked-originals"
        linked.symlink_to(self.share / "jo" / "originals", target_is_directory=True)
        self.appdata_tree = tree(self.appdata)
        targets = {"a person's folder": self.share / "jo", "originals": self.share / "jo" / "originals", "a new folder in originals": self.share / "jo" / "originals" / "x",
                   "the share itself": self.share, "the Pixels' folder": self.share / "_pixels", "a Pixel's folder": self.share / "_pixels" / "1" / "jo",
                   "the inbox": self.share / "_inbox", "a folder holding the share": self.share.parent, "a link into originals": linked,
                   "a new folder through a link into originals": linked / "x"}
        for name, target in targets.items():
            for set_aside in (False, True):
                with self.subTest(target=name, set_aside=set_aside):
                    self.refused(self.source, target, set_aside)

    def test_a_source_inside_the_photo_share_other_than_its_browser_folder_is_refused(self):
        for source in (self.share / "jo", self.share / "jo" / "originals", self.share, self.share.parent):
            with self.subTest(source=str(source)):
                self.refused(source, self.target, False)

    def test_the_command_takes_the_photo_share_from_the_configuration(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"DARKROOM_PHOTOS_ROOT": str(self.share)}, clear=True), redirect_stdout(out), redirect_stderr(err):
            code = run.main(["copy-browser-state", "--source", str(self.source), "--target", str(self.share / "jo" / "originals"), "--set-aside"])
        self.assertEqual(code, 1)
        self.assertIn(browser_copy.IN_THE_SHARE, err.getvalue())
        self.assert_nothing_changed()

    def test_the_shares_browser_folder_and_appdata_still_work_both_ways(self):
        self.copy()
        browser_copy.copy_browser_state(self.target, self.source, STAMP, self.share, set_aside=True)
        self.assertTrue((self.share / f"_browser.before-{STAMP}" / "1" / "profile" / "Default" / "Cookies").is_file())
        self.assertEqual(self.original.read_bytes(), b"an original")


class CommandTests(CopyWorld):
    def run_command(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            with mock.patch.dict(os.environ, {"DARKROOM_PHOTOS_ROOT": str(self.share)}, clear=True):
                code = run.main(["copy-browser-state", *args])
        return code, out.getvalue(), err.getvalue()

    def test_the_command_copies_and_says_the_source_was_not_changed(self):
        code, out, _ = self.run_command("--source", str(self.source), "--target", str(self.target))
        self.assertEqual(code, 0)
        self.assertIn("Copied and verified 4 files", out)
        self.assertIn("The source was not changed.", out)
        self.assert_source_untouched()

    def test_the_command_needs_both_folders_and_refuses_an_existing_target(self):
        self.assertEqual(self.run_command("--source", str(self.source))[0], 1)
        self.target.mkdir()
        code, _, err = self.run_command("--source", str(self.source), "--target", str(self.target))
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        self.assert_source_untouched()


if __name__ == "__main__":
    unittest.main()
