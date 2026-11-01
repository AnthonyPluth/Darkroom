import ast
import re
import unittest
from pathlib import Path

from darkroom_browser import live, selectors

ROOT = Path(__file__).resolve().parent.parent.parent
WORKER = ROOT / "darkroom_browser"
CREDENTIAL_WORDS = re.compile(r"(?i)passw(or)?d|passwd|\botp\b|one[- ]time[- ](code|password)|verification[- ]code|\b2fa\b|two[- ]factor|totp|authenticator|security[- ]key|backup[- ]code|\bpin\b|recovery")
POLICY_FILES = [WORKER / "selectors.py", *sorted((WORKER / "jobs").rglob("*.py"))]
CREDENTIAL_SELECTOR = re.compile(r"(?i)type\s*=\s*['\"]?(password|tel)|name\s*=\s*['\"]?(password|passwd|pin|otp|code|totp)|identifierId|type\s*=\s*['\"]?email")


class NoCredentialsPolicyTests(unittest.TestCase):
    def test_selectors_and_jobs_mention_no_credential_fields(self):
        self.assertTrue(POLICY_FILES)
        for path in POLICY_FILES:
            with self.subTest(file=path.relative_to(ROOT).as_posix()):
                for number, line in enumerate(path.read_text().splitlines(), 1):
                    self.assertIsNone(CREDENTIAL_WORDS.search(line), f"{path.name}:{number} names a credential: the worker never handles one (AGENTS.md, promise 4)")

    def test_every_selector_is_a_named_constant_without_credential_fields(self):
        module = ast.parse((WORKER / "selectors.py").read_text())
        names = []
        for node in module.body:
            if isinstance(node, ast.Assign):
                names += [target.id for target in node.targets if isinstance(target, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.append(node.target.id)
            else:
                self.assertIsInstance(node, ast.ImportFrom | ast.Import, "selectors.py holds named constants only")
        self.assertTrue(names)
        for name in names:
            self.assertTrue(name.isupper(), f"{name} should be a NAMED_CONSTANT")
            value = getattr(selectors, name)
            if isinstance(value, str):
                self.assertIsNone(CREDENTIAL_SELECTOR.search(value), f"{name} points at a credential field")

    def test_typeable_selectors_are_none_of_the_credential_fields(self):
        self.assertIsInstance(selectors.TYPEABLE, frozenset)
        for selector in selectors.TYPEABLE:
            self.assertIsNone(CREDENTIAL_SELECTOR.search(selector), f"{selector} is typeable but looks like a credential field")
            self.assertIsNone(CREDENTIAL_WORDS.search(selector))

    def test_type_text_refuses_any_selector_outside_the_allowlist(self):
        class Page:
            def fill(self, selector, text, timeout):
                raise AssertionError("type_text reached the page for a selector that is not allowlisted")

        browser = live.Live(Page())
        for selector in ("input[type=password]", "#identifierId", "input[name=totpPin]", "input", "body"):
            with self.subTest(selector=selector):
                with self.assertRaises(live.NotTypeable):
                    browser.type_text(selector, "anything")

    def test_type_text_is_only_called_through_live(self):
        for path in WORKER.rglob("*.py"):
            if path.name == "live.py":
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("fill", "type", "press_sequentially", "keyboard"):
                    self.fail(f"{path.relative_to(ROOT)} drives typing itself; only live.type_text may, with an allowlisted selector")

    def test_only_live_imports_playwright(self):
        importers = []
        for path in WORKER.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
                if any(name.split(".")[0] == "playwright" for name in names):
                    importers.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(importers, ["darkroom_browser/live.py"])

    def test_agents_md_promise_points_here(self):
        text = (ROOT / "AGENTS.md").read_text()
        self.assertIn("never types a Google password or 2FA code", text)
        self.assertIn("tests/browser/test_no_credentials.py", text)


if __name__ == "__main__":
    unittest.main()
