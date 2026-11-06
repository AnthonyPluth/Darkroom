from __future__ import annotations

import re
from pathlib import Path
from typing import Any

SECURITY_WORKFLOW = ".github/workflows/security.yml"
CODE_PATHS = re.compile(r"^\s*code-paths:\s*(\S+)\s*$", re.MULTILINE)
SEMGREP_PATHS = re.compile(r"^\s*semgrep-paths:\s*(.+?)\s*$", re.MULTILINE)
NOT_CODE = ("tools/run.py", "docs/SPEC.md")

IOS_RELEASE_WORKFLOW = ".github/workflows/ios-release.yml"
IOS_WORKFLOW = ".github/workflows/ios.yml"
PINNED_TOOLS = re.compile(r"^\s*(XCODEGEN|XCBEAUTIFY)_(VERSION|SHA256):\s*(\S+)\s*$", re.MULTILINE)
PULL_REQUEST_TRIGGER = re.compile(r"\bpull_request\w*\b")
WRITE_PERMISSION = re.compile(r"(:\s*write\b|\bwrite-all\b)")
TOP_LEVEL_NO_PERMISSIONS = re.compile(r"^permissions:\s*\{\}\s*$", re.MULTILINE)
RELEASE_ENVIRONMENT = re.compile(r"^\s+environment:\s*ios-release\s*$", re.MULTILINE)


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


def _code_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if not line.lstrip().startswith("#")]


def check_ios_release(release: str, ios: str) -> list[str]:
    code = "\n".join(_code_lines(release))
    problems = []
    if PULL_REQUEST_TRIGGER.search(code):
        problems.append(f"{IOS_RELEASE_WORKFLOW}: mentions a pull_request trigger or context; a pull request must never start the release or reach its secrets")
    if WRITE_PERMISSION.search(code):
        problems.append(f"{IOS_RELEASE_WORKFLOW}: requests a write permission; the release needs only contents: read")
    if not TOP_LEVEL_NO_PERMISSIONS.search(code):
        problems.append(f"{IOS_RELEASE_WORKFLOW}: the workflow needs a top-level 'permissions: {{}}'")
    if not RELEASE_ENVIRONMENT.search(code):
        problems.append(f"{IOS_RELEASE_WORKFLOW}: the release job must run in 'environment: ios-release', which is where its secrets live")
    if "persist-credentials: false" not in code:
        problems.append(f"{IOS_RELEASE_WORKFLOW}: checkouts need persist-credentials: false")
    ours = {m.group(1, 2): m.group(3) for m in PINNED_TOOLS.finditer(code)}
    theirs = {m.group(1, 2): m.group(3) for m in PINNED_TOOLS.finditer("\n".join(_code_lines(ios)))}
    if not theirs or ours != theirs:
        problems.append(f"{IOS_RELEASE_WORKFLOW}: the XcodeGen and xcbeautify pins and checksums must equal {IOS_WORKFLOW}'s")
    return problems


def ios_release_workflow(context: Any) -> list[str]:
    root: Path = context.root
    release = root / IOS_RELEASE_WORKFLOW
    ios = root / IOS_WORKFLOW
    if not release.is_file() or not ios.is_file():
        return [f"{IOS_RELEASE_WORKFLOW} or {IOS_WORKFLOW}: missing"]
    return check_ios_release(release.read_text(encoding="utf-8"), ios.read_text(encoding="utf-8"))


CHECKS = [semgrep_targets, ios_release_workflow]
