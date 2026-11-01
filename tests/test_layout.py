import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ("darkroom", "darkroom_browser")


class LayoutTests(unittest.TestCase):
    def test_every_folder_with_python_files_is_a_regular_package(self):
        missing = []
        for package in PACKAGES:
            for folder in [ROOT / package, *(p for p in (ROOT / package).rglob("*") if p.is_dir())]:
                if "__pycache__" in folder.parts:
                    continue
                if any(folder.glob("*.py")) and not (folder / "__init__.py").exists():
                    missing.append(str(folder.relative_to(ROOT)))
        self.assertEqual(missing, [], "import-linter only sees regular packages: add an __init__.py")

    def test_every_package_folder_is_listed_here(self):
        self.assertEqual(sorted(p.name for p in ROOT.iterdir() if p.is_dir() and (p / "__init__.py").exists()), [*sorted(PACKAGES), "tests", "tools"])


if __name__ == "__main__":
    unittest.main()
