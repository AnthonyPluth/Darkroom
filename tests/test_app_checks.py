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
        self.assertEqual([c.__name__ for c in ac.CHECKS], ["semgrep_targets", "conflict_markers"])


class ConflictMarkers(unittest.TestCase):
    def found(self, files):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name, text in files.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(text)
            return ac.conflict_markers_in(root)

    def test_a_committed_conflict_is_named_with_its_line(self):
        text = "| a | b |\n<<<<<<< HEAD\n| c | d |\n=======\n| e | f |\n>>>>>>> branch\n"
        self.assertEqual(self.found({"docs/DECISIONS.md": text}), ["docs/DECISIONS.md:2: a merge conflict marker was committed; resolve the conflict"])
        self.assertEqual(len(self.found({"x.py": ">>>>>>> theirs\n"})), 1)

    def test_ordinary_text_and_skipped_folders_pass(self):
        self.assertEqual(self.found({"a.md": "Title\n=======\n\n    <<<<<<<< eight\nquote >>>>>>> mid-line\n",
                                     "node_modules/x.js": "<<<<<<< HEAD\n", "picture.png": "<<<<<<< HEAD\n"}), [])


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
