import os
import stat
import subprocess
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASE = ROOT / "ios" / "release"

CANARY_KEY = "-----BEGIN PRIVATE KEY-----\nCANARYCANARYCANARY\n-----END PRIVATE KEY-----"
CONFIGURED = {
    "DARKROOM_BUNDLE_ID": "com.example.canarybundle",
    "DARKROOM_TEAM_ID": "ABCDE12345",
    "ASC_KEY_ID": "KEYID12345",
    "ASC_ISSUER_ID": "11111111-2222-3333-4444-555555555555",
    "ASC_KEY_P8": CANARY_KEY,
}
SECRET_NAMES = ("ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_KEY_P8")
VARIABLE_NAMES = ("DARKROOM_BUNDLE_ID", "DARKROOM_TEAM_ID")


def png(width, height, color_type):
    channels = {2: 3, 6: 4}[color_type]
    rows = (b"\x00" + bytes([200]) * (width * channels)) * height

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def contents(filename, size="1024x1024"):
    entry = f'"filename" : "{filename}", ' if filename else ""
    return '{"images" : [{' + entry + f'"idiom" : "universal", "platform" : "ios", "size" : "{size}"' + '}], "info" : {"version" : 1}}'


def make_ios_dir(root, *, setting=True, contents_json=None, image=None, image_name="icon.png"):
    ios = Path(root) / "ios"
    icon_set = ios / "Darkroom" / "Assets.xcassets" / "AppIcon.appiconset"
    icon_set.mkdir(parents=True)
    ios.joinpath("project.yml").write_text(
        "settings:\n  base:\n    ASSETCATALOG_COMPILER_APPICON_NAME: AppIcon\n" if setting else "settings: {}\n"
    )
    if contents_json is not None:
        (icon_set / "Contents.json").write_text(contents_json)
    if image is not None:
        (icon_set / image_name).write_bytes(image)
    return ios


def valid_ios_dir(root):
    return make_ios_dir(root, contents_json=contents("icon.png"), image=png(1024, 1024, 2))


def run(script, env, cwd):
    return run_with_args(script, [], env, cwd)


def run_with_args(script, args, env, cwd):
    return subprocess.run(
        ["bash", str(RELEASE / script), *args],
        env={"PATH": os.environ["PATH"], **env},
        cwd=cwd,
        capture_output=True,
        text=True,
    )


class Preflight(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.summary = Path(self.temp.name) / "summary.md"
        ios = valid_ios_dir(Path(self.temp.name) / "fixture")
        self.env = {"RUNNER_TEMP": self.temp.name, "GITHUB_STEP_SUMMARY": str(self.summary), "IOS_DIR": str(ios)}

    def preflight(self, **values):
        return run("preflight.sh", {**self.env, **values}, self.temp.name)

    def test_a_complete_configuration_passes_and_writes_no_key(self):
        result = self.preflight(**CONFIGURED)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((Path(self.temp.name) / "asc").exists())

    def test_nothing_secret_is_printed(self):
        result = self.preflight(**CONFIGURED)
        output = result.stdout + result.stderr + (self.summary.read_text() if self.summary.exists() else "")
        for value in (CANARY_KEY, "CANARYCANARYCANARY", CONFIGURED["ASC_ISSUER_ID"], CONFIGURED["ASC_KEY_ID"]):
            self.assertNotIn(value, output)
        self.assertIn("::add-mask::" + CONFIGURED["DARKROOM_TEAM_ID"], output)

    def test_missing_secrets_and_variables_are_listed_by_name(self):
        result = self.preflight()
        self.assertEqual(result.returncode, 1)
        for name in SECRET_NAMES + VARIABLE_NAMES:
            self.assertIn(name, result.stdout)
        self.assertIn("ios-release", result.stdout)
        self.assertIn("ASC_KEY_ID", self.summary.read_text())
        self.assertFalse((Path(self.temp.name) / "asc").exists())

    def test_each_missing_name_is_reported_alone(self):
        for name in SECRET_NAMES + VARIABLE_NAMES:
            values = {k: v for k, v in CONFIGURED.items() if k != name}
            result = self.preflight(**values)
            self.assertEqual(result.returncode, 1, name)
            self.assertIn(name, result.stdout)
            for other in set(SECRET_NAMES + VARIABLE_NAMES) - {name}:
                self.assertNotIn(other, result.stdout, name)
            self.assertFalse((Path(self.temp.name) / "asc").exists(), name)

    def test_a_missing_icon_stops_an_upload_before_the_key_or_the_configuration_matters(self):
        for upload in ({}, {"UPLOAD": "true"}):
            with tempfile.TemporaryDirectory() as d:
                ios = make_ios_dir(d)
                result = self.preflight(**{**CONFIGURED, "IOS_DIR": str(ios), **upload})
            self.assertEqual(result.returncode, 1, upload)
            self.assertIn("::error", result.stdout)
            self.assertIn("no App Store icon", result.stdout)
            self.assertFalse((Path(self.temp.name) / "asc").exists())

    def test_a_missing_icon_only_warns_on_a_dry_run(self):
        with tempfile.TemporaryDirectory() as d:
            ios = make_ios_dir(d)
            result = self.preflight(**{**CONFIGURED, "IOS_DIR": str(ios), "UPLOAD": "false"})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("::warning", result.stdout)
        self.assertNotIn("::error", result.stdout)
        self.assertIn("no App Store icon", self.summary.read_text())

    def test_a_dry_run_without_an_icon_still_needs_the_configuration(self):
        with tempfile.TemporaryDirectory() as d:
            ios = make_ios_dir(d)
            result = self.preflight(IOS_DIR=str(ios), UPLOAD="false")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ASC_KEY_P8", result.stdout)

    def test_an_empty_value_counts_as_missing(self):
        result = self.preflight(**{**CONFIGURED, "ASC_KEY_P8": ""})
        self.assertEqual(result.returncode, 1)
        self.assertIn("ASC_KEY_P8", result.stdout)

    def test_a_malformed_value_is_named_without_being_printed(self):
        result = self.preflight(**{**CONFIGURED, "ASC_KEY_P8": "CANARYNOTAKEY", "ASC_KEY_ID": "short"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("ASC_KEY_P8", result.stdout)
        self.assertIn("ASC_KEY_ID", result.stdout)
        self.assertNotIn("CANARYNOTAKEY", result.stdout + result.stderr)
        self.assertFalse((Path(self.temp.name) / "asc").exists())


class CheckIcon(unittest.TestCase):
    def check(self, build):
        with tempfile.TemporaryDirectory() as d:
            ios = build(d)
            summary = Path(d) / "summary.md"
            result = run("check-icon.sh", {"IOS_DIR": str(ios), "GITHUB_STEP_SUMMARY": str(summary)}, d)
            return result

    def test_a_1024_opaque_png_in_the_app_icon_set_passes(self):
        self.assertEqual(self.check(valid_ios_dir).returncode, 0)

    def test_no_icon_set_fails_with_the_reason(self):
        def build(d):
            ios = Path(d) / "ios"
            ios.mkdir()
            ios.joinpath("project.yml").write_text("settings: {}\n")
            return ios

        result = self.check(build)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no App Store icon", result.stdout)
        self.assertIn("Contents.json is missing", result.stdout)
        self.assertIn("ASSETCATALOG_COMPILER_APPICON_NAME", result.stdout)

    def test_a_set_without_a_1024_image_fails(self):
        result = self.check(lambda d: make_ios_dir(d, contents_json=contents("small.png", "60x60")))
        self.assertEqual(result.returncode, 1)
        self.assertIn("no 1024x1024 image", result.stdout)

    def test_a_named_file_that_is_missing_fails(self):
        result = self.check(lambda d: make_ios_dir(d, contents_json=contents("gone.png")))
        self.assertEqual(result.returncode, 1)
        self.assertIn("gone.png", result.stdout)

    def test_a_wrong_size_fails(self):
        result = self.check(lambda d: make_ios_dir(d, contents_json=contents("icon.png"), image=png(512, 512, 2)))
        self.assertEqual(result.returncode, 1)
        self.assertIn("1024 x 1024 PNG", result.stdout)

    def test_an_alpha_channel_fails(self):
        result = self.check(lambda d: make_ios_dir(d, contents_json=contents("icon.png"), image=png(1024, 1024, 6)))
        self.assertEqual(result.returncode, 1)
        self.assertIn("alpha", result.stdout)

    def test_the_missing_setting_alone_fails(self):
        result = self.check(lambda d: make_ios_dir(d, setting=False, contents_json=contents("icon.png"), image=png(1024, 1024, 2)))
        self.assertEqual(result.returncode, 1)
        self.assertIn("ASSETCATALOG_COMPILER_APPICON_NAME", result.stdout)

    def test_the_warn_flag_reports_but_passes(self):
        with tempfile.TemporaryDirectory() as d:
            ios = make_ios_dir(d)
            result = run_with_args("check-icon.sh", ["--warn"], {"IOS_DIR": str(ios)}, d)
        self.assertEqual(result.returncode, 0)
        self.assertIn("::warning", result.stdout)

    def test_the_repository_is_blocked_until_the_icon_is_added(self):
        result = run("check-icon.sh", {}, str(ROOT))
        icon = ROOT / "ios" / "Darkroom" / "Assets.xcassets" / "AppIcon.appiconset" / "Contents.json"
        self.assertEqual(result.returncode, 0 if icon.exists() else 1)


class WriteKey(unittest.TestCase):
    def write(self, temp, **values):
        return run("write-key.sh", {"RUNNER_TEMP": temp, **values}, temp)

    def test_the_key_is_readable_only_by_its_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self.write(temp, ASC_KEY_ID="KEYID12345", ASC_KEY_P8=CANARY_KEY)
            self.assertEqual(result.returncode, 0, result.stderr)
            key = Path(temp) / "asc" / "AuthKey_KEYID12345.p8"
            self.assertEqual(key.read_text(), CANARY_KEY + "\n")
            self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(key.parent.stat().st_mode), 0o700)
            self.assertNotIn("CANARYCANARYCANARY", result.stdout + result.stderr)

    def test_without_a_key_nothing_is_written(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self.write(temp, ASC_KEY_ID="KEYID12345")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((Path(temp) / "asc").exists())


class CheckUpload(unittest.TestCase):
    SUCCESS = "Uploading package...\nNo errors uploading 'Darkroom.ipa'\nDelivery UUID: 1234\n"
    DUPLICATE = (
        "ERROR: [ContentDelivery.Uploader] Asset validation failed (-19232) The bundle version must be higher.\n"
        "Successfully uploaded package\n"
    )

    def check(self, status, output):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "altool.log"
            log.write_text(output)
            return subprocess.run(["bash", str(RELEASE / "check-upload.sh"), str(status), str(log)], capture_output=True, text=True)

    def test_a_delivered_build_passes(self):
        result = self.check(0, self.SUCCESS)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("Delivery verified", result.stdout)

    def test_an_error_record_fails_even_when_altool_exits_zero(self):
        result = self.check(0, self.DUPLICATE)
        self.assertEqual(result.returncode, 1)
        self.assertIn("error while exiting 0", result.stdout)

    def test_product_errors_in_structured_output_fail(self):
        self.assertEqual(self.check(0, '{"product-errors": [{"code": -19232}]}\nSuccessfully uploaded package').returncode, 1)

    def test_a_non_zero_exit_fails(self):
        result = self.check(1, self.SUCCESS)
        self.assertEqual(result.returncode, 1)
        self.assertIn("status 1", result.stdout)

    def test_output_with_no_sign_of_delivery_fails(self):
        for output in ("", "Uploading package...\n"):
            result = self.check(0, output)
            self.assertEqual(result.returncode, 1, output)
            self.assertIn("no sign", result.stdout)


class Plan(unittest.TestCase):
    def plan(self, **env):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "output"
            result = run("plan.sh", {"GITHUB_OUTPUT": str(out), **env}, d)
            return result, (out.read_text() if out.exists() else "")

    def test_a_tag_releases_its_version_and_uploads(self):
        result, out = self.plan(EVENT_NAME="push", REF="refs/tags/ios-v0.0.1", REF_NAME="ios-v0.0.1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(out, "version=0.0.1\nupload=true\n")

    def test_a_dispatch_from_main_dry_runs_when_asked(self):
        result, out = self.plan(
            EVENT_NAME="workflow_dispatch", REF="refs/heads/main", REF_NAME="main", INPUT_VERSION="1.2.3", INPUT_UPLOAD="false"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(out, "version=1.2.3\nupload=false\n")

    def test_a_dispatch_can_upload(self):
        _, out = self.plan(
            EVENT_NAME="workflow_dispatch", REF="refs/heads/main", REF_NAME="main", INPUT_VERSION="1.2.3", INPUT_UPLOAD="true"
        )
        self.assertEqual(out, "version=1.2.3\nupload=true\n")

    def test_a_dispatch_from_another_branch_is_refused(self):
        result, out = self.plan(
            EVENT_NAME="workflow_dispatch", REF="refs/heads/topic", REF_NAME="topic", INPUT_VERSION="1.2.3", INPUT_UPLOAD="true"
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("only from main", result.stderr)
        self.assertEqual(out, "")

    def test_a_push_of_a_branch_or_another_tag_is_refused(self):
        for ref in ("refs/heads/main", "refs/tags/v1.0.0"):
            result, out = self.plan(EVENT_NAME="push", REF=ref, REF_NAME=ref.rsplit("/", 1)[1])
            self.assertEqual(result.returncode, 1, ref)
            self.assertEqual(out, "", ref)

    def test_a_pull_request_event_is_refused(self):
        for event in ("pull_request", "pull_request_target"):
            result, out = self.plan(EVENT_NAME=event, REF="refs/pull/1/merge", REF_NAME="1/merge")
            self.assertEqual(result.returncode, 1, event)
            self.assertEqual(out, "", event)

    def test_a_version_that_is_not_three_numbers_is_refused(self):
        for version in ("1.2", "1.2.3-beta", "v1.2.3", "1.2.3; echo hi", ""):
            result, out = self.plan(
                EVENT_NAME="workflow_dispatch", REF="refs/heads/main", REF_NAME="main", INPUT_VERSION=version, INPUT_UPLOAD="false"
            )
            self.assertEqual(result.returncode, 1, version)
            self.assertEqual(out, "", version)
        result, _ = self.plan(EVENT_NAME="push", REF="refs/tags/ios-vbeta", REF_NAME="ios-vbeta")
        self.assertEqual(result.returncode, 1)

    def test_an_upload_input_that_is_not_a_boolean_is_refused(self):
        result, _ = self.plan(
            EVENT_NAME="workflow_dispatch", REF="refs/heads/main", REF_NAME="main", INPUT_VERSION="1.2.3", INPUT_UPLOAD="maybe"
        )
        self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main()
