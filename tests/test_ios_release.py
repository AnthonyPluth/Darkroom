import os
import stat
import subprocess
import tempfile
import unittest
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


def run(script, env, cwd):
    return subprocess.run(
        ["bash", str(RELEASE / script)],
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
        self.env = {"RUNNER_TEMP": self.temp.name, "GITHUB_STEP_SUMMARY": str(self.summary)}

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
