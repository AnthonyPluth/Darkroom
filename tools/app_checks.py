from __future__ import annotations

import re
from pathlib import Path
from typing import Any

SECURITY_WORKFLOW = ".github/workflows/security.yml"
CODE_PATHS = re.compile(r"^\s*code-paths:\s*(\S+)\s*$", re.MULTILINE)
SEMGREP_PATHS = re.compile(r"^\s*semgrep-paths:\s*(.+?)\s*$", re.MULTILINE)
NOT_CODE = ("tools/run.py", "docs/SPEC.md")


def check_semgrep_targets(workflow: str, root: Path) -> list[str]:
    code = CODE_PATHS.search(workflow)
    targets = SEMGREP_PATHS.search(workflow)
    if not code or not targets:
        return [f"{SECURITY_WORKFLOW}: no code-paths or semgrep-paths input to compare"]
    pattern = re.compile(code.group(1))
    problems = []
    for target in targets.group(1).split():
        changed = target if (root / target).is_file() else f"{target}/changed.py"
        if not pattern.search(changed):
            problems.append(f"{SECURITY_WORKFLOW}: a change to {changed} would not run Semgrep (code-paths {code.group(1)}); add {target} to code-paths")
    for path in NOT_CODE:
        if pattern.search(path):
            problems.append(f"{SECURITY_WORKFLOW}: code-paths matches {path}, which is not code Semgrep scans")
    return problems


def semgrep_targets(context: Any) -> list[str]:
    root: Path = context.root
    workflow = root / SECURITY_WORKFLOW
    if not workflow.is_file():
        return [f"{SECURITY_WORKFLOW}: missing"]
    return check_semgrep_targets(workflow.read_text(encoding="utf-8"), root)


CONFLICT_MARKER = re.compile(r"^(<{7}|>{7})( |$)", re.MULTILINE)
CHECKED_SUFFIXES = {".py", ".md", ".ts", ".js", ".svelte", ".css", ".html", ".yml", ".yaml", ".toml", ".json", ".sh", ".cfg", ".ini"}
SKIPPED_FOLDERS = {".git", "node_modules", ".venv", "static", "__pycache__", "artifacts", ".claude"}


def conflict_markers_in(root: Path) -> list[str]:
    problems = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in SKIPPED_FOLDERS for part in relative.parts) or path.suffix not in CHECKED_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        found = CONFLICT_MARKER.search(text)
        if found:
            line = text.count("\n", 0, found.start()) + 1
            problems.append(f"{relative}:{line}: a merge conflict marker was committed; resolve the conflict")
    return problems


def conflict_markers(context: Any) -> list[str]:
    return conflict_markers_in(context.root)


CHECKS = [semgrep_targets, conflict_markers]
