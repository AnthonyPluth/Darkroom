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
        return browser_copy.copy_browser_state(self.source, self.target, STAMP, set_aside)

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
                browser_copy.copy_browser_state(source, target, STAMP)
        self.target.symlink_to(self.share, target_is_directory=True)
        with self.assertRaises(browser_copy.CopyRefused):
            self.copy(set_aside=True)
        self.assert_source_untouched()

    def test_the_copy_back_to_the_share_sets_the_stale_profiles_aside_and_deletes_nothing(self):
        self.copy()
        (self.target / "2" / "profile").mkdir(parents=True)
        (self.target / "2" / "profile" / "Cookies").write_bytes(b"added in the one container")
        copied = browser_copy.copy_browser_state(self.target, self.source, STAMP, set_aside=True)
        self.assertEqual(tree(copied.set_aside), self.before)
        self.assertEqual((self.source / "2" / "profile" / "Cookies").read_bytes(), b"added in the one container")
        self.assertEqual(tree(self.source), tree(self.target))


class CommandTests(CopyWorld):
    def run_command(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
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
