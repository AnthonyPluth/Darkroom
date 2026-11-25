from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.spikes import pixel_reupload


class PixelReuploadTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def invoke(self, *args: str | Path) -> tuple[int, str]:
        out = io.StringIO()
        status = pixel_reupload.main([str(arg) for arg in args], out)
        return status, out.getvalue()

    def test_fixture_roles_encode_different_bytes_in_each_run(self) -> None:
        def fake_encode(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            source = command[command.index("-i") + 1]
            Path(command[-1]).write_text(source)
            return subprocess.CompletedProcess(command, 0)

        runs = []
        with (mock.patch.object(pixel_reupload.shutil, "which", return_value="ffmpeg"),
              mock.patch.object(pixel_reupload.subprocess, "run", side_effect=fake_encode) as encode,
              mock.patch.object(pixel_reupload.secrets, "token_hex", side_effect=("first", "second")),
              mock.patch.object(pixel_reupload.secrets, "randbits", side_effect=(100, 200))):
            for name in ("private-one", "private-two"):
                destination = self.root / name
                status, output = self.invoke("prepare", "--out", destination, "--target-mb", "17")
                self.assertEqual(status, 0, output)
                self.assertNotIn(str(destination), output)
                videos = json.loads(output)["videos"]
                self.assertEqual(set(videos), {"live", "trash", "quota", "restore", "purge", "control"})
                self.assertEqual({video["file"] for video in videos.values()}, {path.name for path in destination.iterdir()})
                runs.extend(video["sha1"] for video in videos.values())
        self.assertEqual(len(set(runs)), 12)
        for call in encode.call_args_list:
            command = call.args[0]
            self.assertIn("-n", command)
            self.assertIn("libx264", command)
            expected_duration = "17" if command[-1].endswith(("-live.mp4", "-trash.mp4", "-quota.mp4")) else "2"
            self.assertEqual(command[command.index("-t") + 1], expected_duration)

    def test_prepare_refuses_to_write_into_an_existing_directory(self) -> None:
        sentinel = self.root / "private-input.mp4"
        sentinel.write_bytes(b"untouched")
        with (mock.patch.object(pixel_reupload.shutil, "which", return_value="ffmpeg"),
              mock.patch.object(pixel_reupload.subprocess, "run") as encode):
            status, output = self.invoke("prepare", "--out", self.root)
        self.assertEqual(status, 2)
        self.assertIn("new output directory", output)
        self.assertNotIn(str(self.root), output)
        self.assertEqual(sentinel.read_bytes(), b"untouched")
        encode.assert_not_called()

    def test_encoder_failure_keeps_paths_out_of_the_error(self) -> None:
        destination = self.root / "private-output"
        failure = subprocess.CalledProcessError(1, [str(destination)], stderr="private-value")
        with (mock.patch.object(pixel_reupload.shutil, "which", return_value="ffmpeg"),
              mock.patch.object(pixel_reupload.subprocess, "run", side_effect=failure)):
            status, output = self.invoke("prepare", "--out", destination)
        self.assertEqual(status, 2)
        self.assertIn("FFmpeg could not generate a fixture", output)
        self.assertNotIn("private", output)

    def test_compare_streams_matches_and_detects_changes_after_a_full_chunk(self) -> None:
        fixture = self.root / "private-fixture.mp4"
        download = self.root / "private-download.mp4"
        prefix = b"f" * pixel_reupload.CHUNK
        fixture.write_bytes(prefix + b"1")
        for observed, matches in ((prefix + b"1", True), (prefix + b"2", False), (prefix, False)):
            with self.subTest(length=len(observed), matches=matches):
                download.write_bytes(observed)
                status, output = self.invoke("compare", fixture, download)
                self.assertEqual(status, 0 if matches else 1)
                self.assertEqual(json.loads(output), {"exact_match": matches, "fixture_bytes": len(prefix) + 1, "observed_bytes": len(observed)})

    def test_compare_missing_or_empty_files_do_not_pass(self) -> None:
        fixture = self.root / "private-fixture.mp4"
        download = self.root / "private-download.mp4"
        fixture.touch()
        status, output = self.invoke("compare", fixture, download)
        self.assertEqual(status, 2)
        self.assertNotIn("private", output)
        download.touch()
        status, output = self.invoke("compare", fixture, download)
        self.assertEqual(status, 1)
        self.assertFalse(json.loads(output)["exact_match"])
