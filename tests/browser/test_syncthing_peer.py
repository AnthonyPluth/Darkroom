import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom.domain import events, pixel_folder, transport
from darkroom.providers import syncthing
from darkroom.storage.models import Pixel, PixelCopy
from darkroom.storage.paths import Photos
from tests.browser import syncthing_host, syncthing_seed
from tests.shared import own_database

ROOT = Path(__file__).resolve().parent.parent.parent
NOW = 1_800_000_000.0
DEVICE = syncthing.device_id_of_certificate(b"made-up certificate of the peer in the real-Syncthing check")
SANDBOX = "--cap-drop=ALL --security-opt no-new-privileges:true --security-opt seccomp=deploy/browser/seccomp.json"


def write(path, data=b"photo"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class ShareSnapshotTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        write(self.root / "_pixels/1/ci/2026/IMG_0001.jpg", b"one")
        write(self.root / "_pixels/1/ci/2026/IMG_0002.jpg", b"two")
        write(self.root / "_pixels/1/.stignore", b"*.xmp\n")
        write(self.root / "ci/originals/2026/IMG_0001.jpg", b"one")
        write(self.root / ".darkroom-share", b"sentinel\n")
        self.before = syncthing_host.snapshot(self.root)

    def after(self):
        return syncthing_host.snapshot(self.root)

    def test_an_untouched_share_has_the_same_whole_tree_sha1(self):
        self.assertEqual(syncthing_host.changed(self.before, self.after()), [])
        self.assertEqual(syncthing_host.digest(self.before), syncthing_host.digest(self.after()))
        self.assertRegex(syncthing_host.digest(self.before), r"^[0-9a-f]{40}$")

    def test_every_kind_of_change_anywhere_on_the_share_is_found(self):
        cases = {
            "appended": lambda: (self.root / "_pixels/1/ci/2026/IMG_0001.jpg").write_bytes(b"one more"),
            "same size, new content": lambda: (self.root / "_pixels/1/ci/2026/IMG_0002.jpg").write_bytes(b"owt"),
            "deleted": lambda: (self.root / "_pixels/1/ci/2026/IMG_0002.jpg").unlink(),
            "added in a new folder": lambda: write(self.root / "_pixels/1/ci/added/IMG_9002.jpg"),
            "an original touched": lambda: os.utime(self.root / "ci/originals/2026/IMG_0001.jpg", (1, 1)),
            "mode": lambda: (self.root / ".darkroom-share").chmod(0o600),
            "a link in place of a file": lambda: ((self.root / "_pixels/1/ci/2026/IMG_0001.jpg").unlink(), (self.root / "_pixels/1/ci/2026/IMG_0001.jpg").symlink_to("/etc")),
            "the ignore patterns": lambda: (self.root / "_pixels/1/.stignore").write_bytes(b"\n"),
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.setUp()
                change()
                self.assertTrue(syncthing_host.changed(self.before, self.after()), name)
                self.assertNotEqual(syncthing_host.digest(self.before), syncthing_host.digest(self.after()))

    def test_only_syncthing_s_own_folder_marker_is_left_out(self):
        write(self.root / "_pixels/1/.stfolder/syncthing-folder-abc123.txt", b"marker")
        self.assertEqual(syncthing_host.changed(self.before, self.after()), [])
        (self.root / "_pixels/1/.stfolder/syncthing-folder-abc123.txt").unlink()
        (self.root / "_pixels/1/.stfolder").rmdir()
        self.assertEqual(syncthing_host.changed(self.before, self.after()), [])
        write(self.root / "_pixels/1/.stfolder-not/IMG_0001.jpg")
        self.assertEqual(syncthing_host.changed(self.before, self.after()), ["_pixels/1/.stfolder-not", "_pixels/1/.stfolder-not/IMG_0001.jpg"])

    def test_only_the_paths_of_copies_darkroom_placed_or_pruned_in_between_are_excused(self):
        rows = [{"id": 1, "state": "placed", "path": "_pixels/1/ci/2026/IMG_0001.jpg"}, {"id": 2, "state": "queued", "path": None}]
        later = [rows[0], {"id": 2, "state": "placed", "path": "_pixels/1/ci/2027/IMG_0003.jpg"}]
        write(self.root / "_pixels/1/ci/2027/IMG_0003.jpg")
        (self.root / "_pixels/1/ci/2026/IMG_0001.jpg").write_bytes(b"changed by someone else")
        allowed = syncthing_host.copies_changed(rows, later)
        self.assertEqual(allowed, ["_pixels/1/ci/2027/IMG_0003.jpg"])
        self.assertEqual(syncthing_host.changed(self.before, self.after(), allowed), ["_pixels/1/ci/2026/IMG_0001.jpg"])
        self.assertEqual(syncthing_host.copies_changed(rows, rows), [])
        self.assertEqual(syncthing_host.copies_changed(rows, rows[:1]), [])

    def test_compare_exits_with_the_share_changed_code_and_names_the_path(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        saved = Path(folder.name)
        (saved / "before.json").write_text(json.dumps(self.before))
        (saved / "same.json").write_text(json.dumps(self.after()))
        shown, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(shown), contextlib.redirect_stderr(errors):
            self.assertEqual(syncthing_host.compare(str(saved / "before.json"), str(saved / "same.json")), 0)
            write(self.root / "_pixels/1/ci/2026/IMG_9001.jpg")
            (saved / "after.json").write_text(json.dumps(self.after()))
            self.assertEqual(syncthing_host.compare(str(saved / "before.json"), str(saved / "after.json")), syncthing_host.CHANGED)
        self.assertIn(f"whole-tree SHA-1 before {syncthing_host.digest(self.before)}", shown.getvalue())
        self.assertIn("changed: _pixels/1/ci/2026/IMG_9001.jpg: None -> ", errors.getvalue())
        self.assertEqual(syncthing_host.CHANGED, 3)

    def test_matches_compares_the_photos_on_both_sides_and_leaves_out_syncthing_s_own_files(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        peer = Path(folder.name)
        write(peer / "ci/2026/IMG_0001.jpg", b"one")
        write(peer / "ci/2026/IMG_0002.jpg", b"two")
        write(peer / ".stfolder/syncthing-folder-abc123.txt")
        self.assertEqual(syncthing_host.contents(self.root / "_pixels/1"), syncthing_host.contents(peer))
        write(peer / "ci/2026/IMG_0002.jpg", b"tow")
        self.assertNotEqual(syncthing_host.contents(self.root / "_pixels/1"), syncthing_host.contents(peer))

    def test_retype_changes_only_the_named_folder_and_refuses_an_unknown_one(self):
        config = self.root / "config.xml"
        config.write_text('<configuration version="51"><folder id="darkroom-pixel-1" path="/photos/_pixels/1" type="sendonly"><device id="A"></device></folder>'
                          '<folder id="darkroom-pixel-2" path="/photos/_pixels/2" type="sendonly"></folder><gui><apikey>k</apikey></gui></configuration>')
        syncthing_host.retype(str(config), "darkroom-pixel-1", "sendreceive")
        folders = {folder.get("id"): folder for folder in ElementTree.parse(config).getroot().iter("folder")}
        self.assertEqual(folders["darkroom-pixel-1"].get("type"), "sendreceive")
        self.assertEqual(folders["darkroom-pixel-1"].get("path"), "/photos/_pixels/1")
        self.assertEqual(folders["darkroom-pixel-2"].get("type"), "sendonly")
        with self.assertRaises(SystemExit):
            syncthing_host.retype(str(config), "darkroom-pixel-3", "sendreceive")


class SeedTests(unittest.TestCase):
    def setUp(self):
        self.opened = Session(own_database(self), expire_on_commit=False)
        self.addCleanup(self.opened.close)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.photos = Photos(Path(folder.name).resolve())

    def test_seed_adds_a_made_up_pixel_with_the_peer_s_device_and_five_queued_copies_of_indexed_originals(self):
        pixel_id = syncthing_seed.seed(self.opened, self.photos, DEVICE, NOW)
        pixel = self.opened.get(Pixel, pixel_id)
        self.assertEqual((pixel.name, pixel.syncthing_device_id), ("CI Pixel", DEVICE))
        rows = syncthing_seed.copies(self.opened)
        self.assertEqual([row["state"] for row in rows], ["queued"] * syncthing_seed.PHOTOS)
        self.assertTrue(all(row["path"] is None for row in rows))
        self.assertEqual(sorted(path.name for path in self.photos.originals("ci").rglob("*.jpg")), [f"IMG_{number:04d}.jpg" for number in range(1, 6)])
        tended = pixel_folder.tend(self.opened, self.photos, pixel_folder.GIGABYTE, NOW + 1)
        self.assertEqual(tended.placed, syncthing_seed.PHOTOS)
        placed = syncthing_seed.copies(self.opened)
        self.assertEqual([row["path"] for row in placed], [f"_pixels/{pixel_id}/ci/2026/IMG_{number:04d}.jpg" for number in range(1, 6)])
        for row in placed:
            self.assertEqual(syncthing_host.file_sha1(self.photos.root / row["path"]), row["sha1"])

    def test_device_clears_and_sets_the_pixel_s_device_id(self):
        pixel_id = syncthing_seed.seed(self.opened, self.photos, DEVICE, NOW)
        syncthing_seed.set_device(self.opened, pixel_id, None, NOW + 1)
        self.assertIsNone(self.opened.get(Pixel, pixel_id).syncthing_device_id)
        syncthing_seed.set_device(self.opened, pixel_id, DEVICE, NOW + 2)
        self.assertEqual(self.opened.get(Pixel, pixel_id).syncthing_device_id, DEVICE)
        self.assertEqual(len(self.opened.execute(select(PixelCopy)).all()), syncthing_seed.PHOTOS)

    def test_removed_finds_only_the_reconciler_s_folder_removed_event_for_that_pixel(self):
        pixel_id = syncthing_seed.seed(self.opened, self.photos, DEVICE, NOW)
        self.assertFalse(syncthing_seed.folder_removed(self.opened, pixel_id))
        events.record(self.opened, NOW, transport.CORRECTED, f"pixel {pixel_id}", transport.NOT_ASKED_FOR)
        events.record(self.opened, NOW, transport.FOLDER_REMOVED, f"pixel {pixel_id + 1}")
        self.assertFalse(syncthing_seed.folder_removed(self.opened, pixel_id))
        events.record(self.opened, NOW, transport.FOLDER_REMOVED, f"pixel {pixel_id}")
        self.assertTrue(syncthing_seed.folder_removed(self.opened, pixel_id))


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        workflow = (ROOT / ".github/workflows/docker.yml").read_text()
        self.job = workflow[workflow.index("\n  single:\n"):workflow.index("\n  publish:\n")]
        self.script = (ROOT / "tests/browser/syncthing_peer.sh").read_text()

    def test_image_ci_runs_the_hostile_peer_after_the_paused_check_and_then_the_send_and_receive_build_that_must_fail_on_the_share(self):
        run = "run: bash tests/browser/syncthing_peer.sh darkroom-single:smoke 8768"
        self.assertIn(run, self.job)
        self.assertLess(self.job.index("syncthing_paused.sh"), self.job.index(run))
        mutation = self.job[self.job.index(run):]
        self.assertIn("FROM darkroom-single:smoke", mutation)
        self.assertIn("bash tests/browser/syncthing_peer.sh darkroom-single:sendreceive 8769 || found=$?", mutation)
        self.assertIn('[ "$found" = 3 ]', mutation)
        self.assertEqual(re.findall(r"(?m)^(share|peer)_changed=(\d+)$", self.script), [("share", "3"), ("peer", "4")])

    def test_the_send_and_receive_build_still_finds_the_line_it_changes(self):
        provider = (ROOT / "darkroom/providers/syncthing.py").read_text()
        self.assertEqual(re.findall(r"(?m)^SEND_ONLY = .*$", provider), ['SEND_ONLY = "sendonly"'])
        self.assertEqual(syncthing.SEND_ONLY, "sendonly")
        self.assertIn("""sed -i 's/^SEND_ONLY = "sendonly"$/SEND_ONLY = "sendreceive"/' /app/darkroom/providers/syncthing.py""", self.job)
        self.assertIn("""grep -qx 'SEND_ONLY = "sendreceive"' /app/darkroom/providers/syncthing.py""", self.job)

    def test_the_script_runs_both_instances_under_the_deployment_sandbox_and_exits_3_only_on_a_changed_share(self):
        self.assertIn(f"sandbox=({SANDBOX})", self.script)
        self.assertEqual(self.script.count('"${sandbox[@]}"'), 2)
        self.assertIn("-e DARKROOM_SYNCTHING=1", self.script)
        self.assertNotIn("--network host", self.script)
        self.assertNotRegex(self.script, r"-p [^ ]*(22000|21027|8384)")
        self.assertIn(f"differs={syncthing_host.CHANGED}\n", self.script)
        self.assertIn("share_changed=3\n", self.script)
        self.assertIn("peer_changed=4\n", self.script)
        share = self.script[self.script.index("unchanged() {"):self.script.index("peer_unchanged() {")]
        peer = self.script[self.script.index("peer_unchanged() {"):self.script.index("\n}\n", self.script.index("peer_unchanged() {"))]
        self.assertIn('if [ "$found" = "$differs" ]; then echo "$2" >&2; logs; exit "$share_changed"; fi', share)
        self.assertIn('if [ "$found" = "$differs" ]; then echo "$2" >&2; logs; exit "$peer_changed"; fi', peer)
        self.assertEqual(self.script.count('exit "$share_changed"'), 1)
        self.assertEqual(self.script.count('exit "$peer_changed"'), 1)
        self.assertNotRegex(self.script, r"exit [34]\b")

    def test_the_script_compares_the_whole_share_around_each_hostile_step(self):
        for step in ("hostile", "removal", "persisted"):
            self.assertIn(f"snapshot {step}\n", self.script)
            self.assertIn(f"unchanged {step} ", self.script)
        self.assertIn('host snapshot "$work/share"', self.script)
        self.assertIn('-v "$work/share:/photos"', self.script)
        self.assertIn("peer_unchanged removal ", self.script)
        self.assertLess(self.script.index("unchanged hostile"), self.script.index("probe \"$darkroom\" need"))
        self.assertLess(self.script.index("unchanged hostile"), self.script.index('field "$shown" type'))
        self.assertLess(self.script.index('host retype "$work/data/syncthing/config.xml" "$folder" sendreceive'), self.script.index("start_darkroom start"))
        self.assertLess(self.script.index("docker stop \"$darkroom\""), self.script.index("host retype"))


class ComparisonExitCodeTests(unittest.TestCase):
    def setUp(self):
        script = (ROOT / "tests/browser/syncthing_peer.sh").read_text()
        lines = script.splitlines()
        settings = [line for line in lines if re.fullmatch(r"(differs|share_changed|peer_changed)=\d+", line)]
        one_liners = [line for line in lines if line.startswith(("fail() {", "snapshot() {"))]
        blocks = [script[script.index(f"\n{name}() {{\n") + 1:script.index("\n}\n", script.index(f"\n{name}() {{\n")) + 3] for name in ("unchanged", "peer_unchanged")]
        self.assertEqual((len(settings), len(one_liners)), (3, 2))
        self.functions = "\n".join([*settings, *one_liners, *blocks])
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        write(self.work / "share/_pixels/1/ci/2026/IMG_0001.jpg")
        write(self.work / "peer/pixel/ci/2026/IMG_0001.jpg")

    def run_steps(self, between):
        program = "\n".join([
            "set -eo pipefail",
            f"work={self.work}",
            f"here={ROOT / 'tests/browser'}",
            f'host() {{ "{sys.executable}" -I "$here/syncthing_host.py" "$@"; }}',
            "seed() { echo '[]'; }",
            "logs() { :; }",
            self.functions,
            "snapshot step",
            between,
            'unchanged step "the share changed"',
            'peer_unchanged step "the peer changed"',
        ])
        return subprocess.run([shutil.which("bash"), "-c", program], capture_output=True, text=True, check=False)

    def test_an_unchanged_share_and_peer_pass(self):
        self.assertEqual(self.run_steps(":").returncode, 0)

    def test_a_changed_share_exits_3_with_the_step_s_message(self):
        ran = self.run_steps('printf more >> "$work/share/_pixels/1/ci/2026/IMG_0001.jpg"')
        self.assertEqual(ran.returncode, 3)
        self.assertIn("the share changed", ran.stderr)
        self.assertNotIn("the peer changed", ran.stderr)

    def test_a_changed_peer_exits_4_not_the_share_comparison_s_3(self):
        ran = self.run_steps('rm "$work/peer/pixel/ci/2026/IMG_0001.jpg"')
        self.assertEqual(ran.returncode, 4)
        self.assertIn("the peer changed", ran.stderr)

    def test_a_share_that_cannot_be_compared_fails_with_1_not_3(self):
        ran = self.run_steps('rm "$work/step.share.json"')
        self.assertEqual(ran.returncode, 1)
        self.assertIn("the share could not be compared", ran.stderr)

    def test_the_send_and_receive_step_s_expected_code_is_the_share_comparison_s(self):
        workflow = (ROOT / ".github/workflows/docker.yml").read_text()
        expected = re.search(r'\[ "\$found" = (\d+) \]', workflow[workflow.index("darkroom-single:sendreceive 8769"):])
        ran = self.run_steps('printf more >> "$work/share/_pixels/1/ci/2026/IMG_0001.jpg"; rm "$work/peer/pixel/ci/2026/IMG_0001.jpg"')
        self.assertEqual(str(ran.returncode), expected.group(1))


if __name__ == "__main__":
    unittest.main()
