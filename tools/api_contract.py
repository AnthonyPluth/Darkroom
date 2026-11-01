from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import get_args, get_origin, get_type_hints

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from darkroom.server import contract, routes

OUTPUT = ROOT / "docs/openapi.json"
JSON_TYPES = {bool: "boolean", int: "integer", float: "number", str: "string"}


def type_schema(kind: object) -> dict[str, object]:
    if kind is list or get_origin(kind) is list:
        items = get_args(kind)
        return {"type": "array", "items": type_schema(items[0])} if items else {"type": "array"}
    return {"type": JSON_TYPES.get(kind, "object")} if isinstance(kind, type) else {"type": "object"}


def schema_of(reply: type) -> dict[str, object]:
    hints = get_type_hints(reply)
    return {"type": "object", "required": sorted(hints), "properties": {name: type_schema(kind) for name, kind in hints.items()}}


def build() -> dict[str, object]:
    paths: dict[str, dict[str, object]] = {}
    for route in routes.ROUTES:
        key = f"{route.method} {route.path}"
        reply = contract.REPLIES.get(key)
        operation: dict[str, object] = {"operationId": route.handler.__name__, "x-auth": route.auth}
        operation["responses"] = {"200": {"description": "OK", **({"content": {"application/json": {"schema": schema_of(reply)}}} if reply else {})}}
        paths.setdefault(route.path, {})[route.method.lower()] = operation
    return {"openapi": "3.1.0", "info": {"title": "Darkroom", "version": "1"}, "paths": paths}


def render() -> str:
    return json.dumps(build(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    text = render()
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text() != text:
            print("docs/openapi.json is stale: run `make api-contract` and commit the result", file=sys.stderr)
            return 1
        print("API contract is current.")
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
