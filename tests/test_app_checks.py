import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tools import app_checks as ac

ROOT = Path(__file__).resolve().parent.parent


def context(root=ROOT):
    return SimpleNamespace(root=root, config=None, commits=None)


def workflow(code_paths, semgrep_paths):
    return f"    with:\n      code-paths: {code_paths}\n      semgrep-paths: {semgrep_paths}\n"


class Checks(unittest.TestCase):
    def test_the_repository_passes_every_check(self):
        for check in ac.CHECKS:
            self.assertEqual(check(context()), [], check.__name__)

    def test_the_checks_are_registered(self):
        self.assertEqual([c.__name__ for c in ac.CHECKS], ["semgrep_targets", "ios_release_workflow"])


class SemgrepTargets(unittest.TestCase):
    def problems(self, code_paths, semgrep_paths, files=("run.py",)):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name in files:
                (root / name).write_text("")
            return ac.check_semgrep_targets(workflow(code_paths, semgrep_paths), root)

    def test_every_semgrep_target_is_in_the_security_scans_change_filter(self):
        self.assertEqual(self.problems(r"^((darkroom|frontend/src)/|run\.py$)", "darkroom frontend/src run.py"), [])

    def test_a_target_the_filter_misses_is_named(self):
        found = self.problems(r"^(darkroom/|run\.py$)", "darkroom darkroom_browser run.py")
        self.assertEqual(len(found), 1)
        self.assertIn("darkroom_browser/changed.py", found[0])

    def test_a_file_target_is_matched_as_the_file(self):
        self.assertEqual(len(self.problems(r"^darkroom/", "darkroom run.py")), 1)

    def test_a_filter_that_matches_non_code_is_named(self):
        found = self.problems(r"^(darkroom/|run\.py$|tools/|docs/)", "darkroom run.py")
        self.assertEqual(len(found), 2)

    def test_a_workflow_without_the_inputs_is_a_problem(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(len(ac.check_semgrep_targets("name: x\n", Path(d))), 1)

    def test_a_missing_workflow_is_a_problem(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(ac.semgrep_targets(context(Path(d))), [f"{ac.SECURITY_WORKFLOW}: missing"])


IOS = """env:
  XCODEGEN_VERSION: "2.46.0"
  XCODEGEN_SHA256: "aa"
  XCBEAUTIFY_VERSION: "3.2.1"
  XCBEAUTIFY_SHA256: "bb"
"""

RELEASE = """on:
  push:
    tags: ["ios-v*"]
  workflow_dispatch:
# a pull_request_target comment is not a trigger
permissions: {}
""" + IOS + """jobs:
  release:
    environment: ios-release
    permissions:
      contents: read
    steps:
      - run: swift test
      - run: ios/release/write-key.sh
      - run: xcodebuild archive
      - uses: actions/checkout@abc   # v1
        with:
          persist-credentials: false
"""


class IosRelease(unittest.TestCase):
    def problems(self, release=RELEASE, ios=IOS):
        return ac.check_ios_release(release, ios)

    def test_a_workflow_that_follows_the_rules_passes(self):
        self.assertEqual(self.problems(), [])

    def test_a_pull_request_trigger_is_named(self):
        for trigger in ("pull_request:", "pull_request_target:", "[pull_request]"):
            found = self.problems(RELEASE.replace("workflow_dispatch:", f"workflow_dispatch:\n  {trigger}"))
            self.assertEqual(len(found), 1, trigger)
            self.assertIn("pull_request", found[0])

    def test_a_write_permission_is_named(self):
        self.assertEqual(len(self.problems(RELEASE.replace("contents: read", "contents: write"))), 1)
        self.assertEqual(len(self.problems(RELEASE.replace("permissions: {}", "permissions: write-all"))), 2)

    def test_a_missing_environment_is_named(self):
        found = self.problems(RELEASE.replace("environment: ios-release", "environment: other"))
        self.assertEqual(len(found), 1)
        self.assertIn("environment: ios-release", found[0])

    def test_a_missing_empty_top_level_permissions_is_named(self):
        self.assertEqual(len(self.problems(RELEASE.replace("permissions: {}\n", ""))), 1)

    def test_credentials_left_in_the_checkout_are_named(self):
        self.assertEqual(len(self.problems(RELEASE.replace("persist-credentials: false", "persist-credentials: true"))), 1)

    def test_toolchain_pins_that_differ_from_the_unsigned_build_are_named(self):
        found = self.problems(ios=IOS.replace("2.46.0", "2.47.0"))
        self.assertEqual(len(found), 1)
        self.assertIn("pins", found[0])

    def test_a_variable_in_the_job_env_is_named(self):
        found = self.problems(RELEASE.replace("    steps:\n", "    env:\n      DARKROOM_TEAM_ID: x\n    steps:\n"))
        self.assertEqual(len(found), 1)
        self.assertIn("DARKROOM_", found[0])

    def test_a_variable_in_a_step_env_passes(self):
        self.assertEqual(self.problems(RELEASE + "        env:\n          DARKROOM_TEAM_ID: x\n"), [])

    def test_the_key_written_before_the_tests_or_after_the_archive_is_named(self):
        early = RELEASE.replace("      - run: swift test\n      - run: ios/release/write-key.sh\n", "      - run: ios/release/write-key.sh\n      - run: swift test\n")
        late = RELEASE.replace("      - run: ios/release/write-key.sh\n      - run: xcodebuild archive\n", "      - run: xcodebuild archive\n      - run: ios/release/write-key.sh\n")
        self.assertEqual(len(self.problems(early)), 1)
        self.assertEqual(len(self.problems(late)), 1)
        self.assertEqual(len(self.problems(RELEASE.replace("ios/release/write-key.sh", "true"))), 1)

    def test_a_missing_file_is_a_problem(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(len(ac.ios_release_workflow(context(Path(d)))), 1)
