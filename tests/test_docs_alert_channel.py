import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SELF = "tests/test_docs_alert_channel.py"

NTFY_HISTORY = {
    "docs/DECISIONS.md": "the owner's decision log records that ntfy was chosen first and replaced by Resend",
    ".github/agent-review/invariants.md": "append-only notes about the push, including the replacement",
    "darkroom/config.py": "the warning for a leftover NTFY_* variable in an old .env",
    "darkroom/server/jobs.py": "the startup warning for a leftover NTFY_* variable",
    "tests/test_resend.py": "the test of that warning",
    SELF: "this check",
}

SUPERSEDED = (
    re.compile(r"no delivery channel", re.I),
    re.compile(r"nothing is sent to a phone or an inbox", re.I),
    re.compile(r"alert channel[^\n|]*\|\s*not decided", re.I),
    re.compile(r"(alert|channel)[^\n]{0,80}(undecided|not decided|is not decided)|(undecided|not decided)[^\n]{0,80}(alert|channel)", re.I),
    re.compile(r"no alert channel exists", re.I),
)


def prose_files() -> list[Path]:
    found = [*ROOT.glob("*.md"), *(ROOT / "docs").rglob("*.md"), ROOT / ".env.example"]
    return sorted(path for path in found if path.is_file() and "design" not in path.relative_to(ROOT).parts)


def source_files() -> list[Path]:
    folders = [ROOT / "darkroom", ROOT / "darkroom_browser", ROOT / "tests", ROOT / ".github"]
    found = [ROOT / "run.py", ROOT / "pyproject.toml", ROOT / "docker-compose.yml"]
    for folder in folders:
        found += [path for path in folder.rglob("*") if path.is_file() and path.suffix in {".py", ".md", ".yml", ".toml"}]
    return sorted(found)


class AlertChannelDocsTests(unittest.TestCase):
    def test_no_document_says_the_alert_channel_is_missing_or_undecided(self):
        for path in prose_files():
            text = path.read_text()
            for pattern in SUPERSEDED:
                match = pattern.search(text)
                self.assertIsNone(match, f"{path.relative_to(ROOT)} says '{match and match.group(0)}', but alerts are emailed through Resend when it is set up (docs/deploy/alerts-email.md): reword it")

    def test_ntfy_appears_only_where_it_is_history(self):
        for path in source_files() + prose_files():
            relative = str(path.relative_to(ROOT))
            if relative in NTFY_HISTORY:
                continue
            self.assertNotRegex(path.read_text(), re.compile("ntfy", re.I), f"{relative} mentions ntfy, which Resend replaced: remove it, or add the file to NTFY_HISTORY in {SELF} with the reason it is history")
