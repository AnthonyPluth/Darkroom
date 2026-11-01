from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = ROOT / "darkroom/storage/migrations/versions"
MIGRATION_TESTS = ROOT / "tests/test_migrations.py"
WORKFLOWS = ROOT / ".github/workflows"
ACTIONS = ROOT / ".github/actions"

REVISION = re.compile(r"^(\d{4})_\w+\.py$")
DOWN_REVISION = re.compile(r"^down_revision\s*=\s*(?:[\"'](\d{4})[\"']|None)", re.MULTILINE)
MODEL_FAMILIES = ("Opus", "Sonnet", "Haiku", "Fable")
CO_AUTHOR = re.compile(r"^co-authored-by:\s*(.*)$", re.IGNORECASE | re.MULTILINE)
SESSION = re.compile(r"^claude-session:\s*\S", re.IGNORECASE | re.MULTILINE)
GOOD_CO_AUTHOR = re.compile(r"^Claude (" + "|".join(MODEL_FAMILIES) + r") \d+(\.\d+)* <noreply@anthropic\.com>$")
CLAUDE = re.compile(r"\bclaude\b|anthropic\.com", re.IGNORECASE)
USES = re.compile(r"^[ \t]*(?:-[ \t]*)?uses:[ \t]*([^\s#]+)(.*)$", re.MULTILINE)
PINNED = re.compile(r"@[0-9a-f]{40}$")
BASH_DEFAULT = re.compile(r"^defaults:[ \t]*\n[ \t]+run:[ \t]*\n(?:[ \t]{4,}\S.*\n)*?[ \t]{4,}shell:[ \t]*bash[ \t]*(?:#.*)?$", re.MULTILINE)
CHECKOUT = re.compile(r"uses:\s*actions/checkout@")
PERSIST = re.compile(r"persist-credentials:\s*false")
GH_API_LIST = re.compile(r"\bgh api\b[^\n]*per_page=")
REMOVES = re.compile(r"^removes-test:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
SKIPS = re.compile(r"^skips-test:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
PY_TEST = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)\s*\(", re.MULTILINE)
SKIP_CALL = re.compile(r"\bunittest\.skip(?:If|Unless)?\b|\bskipTest\(")


def migrations(directory: Path = MIGRATIONS) -> list[dict[str, str | None]]:
    found = []
    for path in sorted(directory.glob("*.py")) if directory.is_dir() else []:
        match = REVISION.match(path.name)
        if not match:
            continue
        down = DOWN_REVISION.search(path.read_text())
        found.append({"revision": match.group(1), "down": down.group(1) if down else None, "file": path.name})
    return found


def check_migrations(found: list[dict[str, str | None]], tests_text: str) -> list[str]:
    problems = []
    revisions = {m["revision"] for m in found}
    downs = {m["down"] for m in found if m["down"]}
    heads = revisions - downs
    if found and len(heads) != 1:
        problems.append(f"migrations: {len(heads)} heads ({', '.join(sorted(str(h) for h in heads))}); there must be exactly one")
    for m in found:
        if not re.search(rf"def test_{m['revision']}_\w+\(", tests_text):
            problems.append(f"migrations: {m['file']} has no test_{m['revision']}_… in tests/test_migrations.py")
    return problems


def check_commit(sha: str, message: str) -> list[str]:
    co_authors = [c.strip() for c in CO_AUTHOR.findall(message)]
    claude = [c for c in co_authors if CLAUDE.search(c)]
    if not SESSION.search(message) and not claude:
        return []
    subject = message.strip().splitlines()[0] if message.strip() else ""
    where = f"commit {sha[:10]} ({subject[:60]})"
    if not claude:
        return [f"{where}: an agent's commit needs a 'Co-Authored-By: Claude <Model> <version> <noreply@anthropic.com>' trailer"]
    if len(claude) > 1:
        return [f"{where}: more than one Claude Co-Authored-By trailer; keep the one for the model that wrote it"]
    if not GOOD_CO_AUTHOR.match(claude[0]):
        return [f"{where}: 'Co-Authored-By: {claude[0]}' should name the model, as 'Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>'"]
    return []


def check_workflow(name: str, text: str, workflow: bool = True) -> list[str]:
    problems = []
    if workflow and not BASH_DEFAULT.search(text):
        problems.append(f"{name}: no top-level `defaults: run: shell: bash` (steps then run without -o pipefail)")
    for m in USES.finditer(text):
        target, rest = m.group(1), m.group(2)
        if target.startswith(("./", "docker://")):
            continue
        if not PINNED.search(target):
            problems.append(f"{name}: {target} isn't pinned by its commit SHA")
        elif not re.search(r"#\s*v?\d", rest):
            problems.append(f"{name}: {target} has no version in a comment (`# v1.2.3`)")
    for block in re.split(r"(?m)^(?=[ \t]*-[ \t]*(?:name|uses):)", text):
        if CHECKOUT.search(block) and not PERSIST.search(block):
            problems.append(f"{name}: a checkout step without `persist-credentials: false`")
    for line in text.splitlines():
        if GH_API_LIST.search(line) and "--paginate" not in line:
            problems.append(f"{name}: `gh api ... per_page=` without --paginate reads only the first page: {line.strip()[:80]}")
    return problems


def check_workflows(workflows: Path = WORKFLOWS, actions: Path = ACTIONS) -> list[str]:
    problems = []
    for path in sorted(workflows.glob("*.y*ml")):
        problems += check_workflow(str(path.relative_to(ROOT)), path.read_text())
    for path in sorted(actions.glob("*/action.y*ml")):
        problems += check_workflow(str(path.relative_to(ROOT)), path.read_text(), workflow=False)
    return problems


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def _show(rev: str, path: str) -> str | None:
    done = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=ROOT, capture_output=True, text=True)
    return done.stdout if done.returncode == 0 else None


def is_test_file(path: str) -> bool:
    return path.startswith("tests/") and path.endswith(".py") and Path(path).name.startswith("test_")


def test_names(text: str) -> set[str]:
    return set(PY_TEST.findall(text))


def removed_tests(base: str, head: str = "HEAD") -> list[str]:
    removed = []
    for path in _git("diff", "--name-only", "--diff-filter=DM", f"{base}..{head}").split():
        if not is_test_file(path):
            continue
        before, after = _show(base, path), _show(head, path)
        if before is None:
            continue
        removed += [f"{path}::{name}" for name in sorted(test_names(before) - test_names(after or ""))]
    return removed


def added_skips(base: str, head: str = "HEAD") -> list[str]:
    found = []
    for line in _git("diff", "-U0", f"{base}..{head}", "--", "tests").splitlines():
        if line.startswith("+") and not line.startswith("+++") and SKIP_CALL.search(line):
            found.append(line[1:].strip()[:80])
    return found


def check_tests(base: str, messages: list[str], head: str = "HEAD") -> list[str]:
    problems = []
    declared_removed = " ".join(m for text in messages for m in REMOVES.findall(text))
    declared_skips = SKIPS.findall("\n".join(messages))
    for name in removed_tests(base, head):
        if name.split("::")[1] not in declared_removed:
            problems.append(f"{name} was removed or renamed: say why in a `Removes-Test: <name>: <reason>` trailer")
    if added_skips(base, head) and not declared_skips:
        problems.append("a test was skipped: say why in a `Skips-Test: <name>: <reason>` trailer")
    return problems


def commits(revision_range: str) -> list[tuple[str, str]]:
    out = _git("log", "--format=%H%x00%B%x1e", revision_range, "--")
    return [(rec.split("\0", 1)[0].strip(), rec.split("\0", 1)[1]) for rec in out.split("\x1e") if rec.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commits", metavar="BASE..HEAD", help="also check these commits' trailers (a pull request's)")
    args = parser.parse_args(argv)
    problems = check_migrations(migrations(), MIGRATION_TESTS.read_text() if MIGRATION_TESTS.exists() else "")
    problems += check_workflows()
    if args.commits:
        found = commits(args.commits)
        for sha, message in found:
            problems += check_commit(sha, message)
        base, head = args.commits.split("..", 1)[0], (args.commits.split("..", 1)[1] or "HEAD")
        problems += check_tests(base, [message for _, message in found], head)
    for problem in problems:
        print(f"::error::{problem}" if "GITHUB_ACTIONS" in os.environ else problem, file=sys.stderr)
    if not problems:
        print("Fleet checks passed: one migration head, migrations tested, workflow conventions"
              + (", commit trailers, no test removed or skipped without a reason." if args.commits else "."))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
