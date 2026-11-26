import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOMAIN = ROOT / "darkroom" / "domain"

WRITERS = {
    "bring_in", "copy_into", "fill_original", "place_google_only", "place.copy_exactly", "place.copy_into", "place.place", "place.place_google_only",
    "paths.carry_sidecar", "paths.remove_losing_copy", "paths.set_library_copy_aside", "paths.set_sidecar_aside", "refresh_takeout",
    "takeout_copies.resolve_person", "xmp.fill", "xmp.refresh",
}

CALLERS = {
    ("immich_reconcile.py", "settle"), ("ingest/part_ingest.py", "place_decided"), ("ingest/part_ingest.py", "fill"), ("ingest/place.py", "place_google_only"),
    ("ingest/place.py", "hold_for_review"), ("ingest/place.py", "fill_original"), ("ingest/place.py", "refresh_takeout"), ("ingest/place.py", "place"),
    ("ingest/run.py", "stage_place"), ("legacy.py", "settle"), ("review.py", "keep_both"), ("review.py", "move_back"), ("review.py", "take_back"),
    ("review.py", "reassign"), ("takeout_copies.py", "finish"), ("takeout_copies.py", "finish_gone"),
}

GUARDS = {
    "darkroom/domain/downloads.py": {"finish": "run.admit_import"},
    "darkroom/domain/immich_reconcile.py": {"reconcile_person": "managed_switch.reconcile_paused"},
    "darkroom/domain/ingest/cleanup.py": {"switch_allows": "managed_switch.hold"},
    "darkroom/domain/ingest/finish_export.py": {"finish": "managed_switch.check"},
    "darkroom/domain/ingest/part_free.py": {"switch_guard": "managed_switch.hold"},
    "darkroom/domain/ingest/part_ingest.py": {"ingest_part": "managed_switch.begin", "queue": "run.admit_import"},
    "darkroom/domain/ingest/place.py": {"place_google_only": "check_folder", "matched_original": "can_fill_folder", "place": "can_fill_folder"},
    "darkroom/domain/ingest/run.py": {"import_export": "managed_switch.check", "request_import": "admit_import", "route_pending": "admit_import"},
    "darkroom/domain/ingest/unpack.py": {"register_zips": "managed_switch.hold"},
    "darkroom/domain/legacy.py": {"import_person": "managed_switch.hold"},
    "darkroom/domain/review.py": {"keep_both": "guard_folders", "undo": "guard_folders", "reassign": "guard_folders"},
    "run.py": {"unpack_only_export": "managed_switch.hold"},
}


def calls_by_function(path):
    tree = ast.parse(path.read_text())
    return {node.name: {ast.unparse(child.func) for child in ast.walk(node) if isinstance(child, ast.Call)}
            for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}


class OldFolderWriterTests(unittest.TestCase):
    def test_old_folder_copy_sidecar_and_removal_calls_stay_on_the_guarded_paths(self):
        actual = {(path.relative_to(DOMAIN).as_posix(), name) for path in DOMAIN.rglob("*.py")
                  for name, calls in calls_by_function(path).items() if calls & WRITERS}
        self.assertEqual(actual, CALLERS)

    def test_writer_and_admission_entry_points_call_their_guards(self):
        for path, guards in GUARDS.items():
            calls = calls_by_function(ROOT / path)
            for writer, guard in guards.items():
                with self.subTest(path=path, writer=writer):
                    self.assertIn(guard, calls.get(writer, set()))
