from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from darkroom.server import routes

OUTPUT = ROOT / "docs/feature-map.json"
ALLOWLIST = ROOT / "tools/feature_map_allowlist.txt"
TESTS = ROOT / "tests"
FRONTEND = ROOT / "frontend/src"
CALL = re.compile(r"apiCall<\s*[\"'](\w+ /[^\"']*)[\"']")


def files_mentioning(directory: Path, patterns: tuple[str, ...], suffixes: tuple[str, ...]) -> list[str]:
    if not directory.is_dir():
        return []
    return sorted(str(p.relative_to(ROOT)) for p in directory.rglob("*") if p.suffix in suffixes and any(pattern in p.read_text() for pattern in patterns))


def callers(directory: Path = FRONTEND) -> dict[str, list[str]]:
    found: dict[str, set[str]] = {}
    if not directory.is_dir():
        return {}
    for path in sorted(directory.rglob("*")):
        if path.suffix not in (".ts", ".svelte") or path.name.endswith((".test.ts", ".d.ts")) or "node_modules" in path.parts:
            continue
        for key in CALL.findall(path.read_text()):
            found.setdefault(key, set()).add(str(path.relative_to(ROOT)))
    return {key: sorted(files) for key, files in found.items()}


def build() -> dict[str, object]:
    calls = callers()
    entries = []
    for route in routes.ROUTES:
        key = f"{route.method} {route.path}"
        entries.append({
            "route": key,
            "auth": route.auth,
            "handler": f"{route.handler.__module__}.{route.handler.__name__}",
            "frontend": calls.get(key, []),
            "tests": files_mentioning(TESTS, (route.path,), (".py",)),
        })
    return {"routes": entries}


def render() -> str:
    return json.dumps(build(), indent=2) + "\n"


def untested(data: dict[str, object]) -> list[str]:
    allowed = set(ALLOWLIST.read_text().split()) if ALLOWLIST.exists() else set()
    entries = data["routes"]
    assert isinstance(entries, list)
    return [e["route"] for e in entries if not e["tests"] and e["route"] not in allowed]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    text = render()
    if not args.check:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(text)
        return 0
    problems = [f"{route} has no test that mentions its path" for route in untested(json.loads(text))]
    if not OUTPUT.exists() or OUTPUT.read_text() != text:
        problems.append("docs/feature-map.json is stale: run `make feature-map` and commit the result")
    for problem in problems:
        print(problem, file=sys.stderr)
    if not problems:
        print("Feature map is current and every route has a test.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
