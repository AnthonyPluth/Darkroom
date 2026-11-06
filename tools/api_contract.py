from __future__ import annotations

import argparse
import inspect
import json
import sys
import types
from pathlib import Path
from typing import Annotated, Any, Literal, NotRequired, Required, Union, get_args, get_origin, get_type_hints, is_typeddict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from darkroom.domain import devices
from darkroom.server import contract, routes

OPENAPI_OUT = ROOT / "docs/openapi.json"
TS_OUT = ROOT / "frontend/src/lib/api-types.ts"
JSON_TYPES: dict[object, dict[str, object]] = {bool: {"type": "boolean"}, int: {"type": "integer"}, float: {"type": "number"},
                                               str: {"type": "string"}, type(None): {"type": "null"}}
ERROR = {"description": "Refused (4xx) or failed (5xx): what went wrong, to show as it is. A device route's 401 for a token that matches a device also says why, in `reason`.",
         "content": {"application/json": {"schema": {"type": "object", "required": ["error"], "properties": {
             "error": {"type": "string"}, "reason": {"type": "string", "enum": list(devices.REFUSALS)}}}}}}
TS_SCALARS = {"string": "string", "integer": "number", "number": "number", "boolean": "boolean", "null": "null"}


class ContractError(Exception):
    pass


def contract_types() -> dict[str, type]:
    return {name: kind for name, kind in vars(contract).items() if isinstance(kind, type) and is_typeddict(kind) and kind.__module__ == contract.__name__}


def type_schema(kind: object, used: set[str] | None = None) -> dict[str, object]:
    origin = get_origin(kind)
    if origin in (NotRequired, Required, Annotated):
        return type_schema(get_args(kind)[0], used)
    if kind in JSON_TYPES:
        return dict(JSON_TYPES[kind])
    if kind is list or origin is list:
        items = get_args(kind)
        return {"type": "array", "items": type_schema(items[0], used)} if items else {"type": "array"}
    if kind is dict:
        return {"type": "object"}
    if origin is dict:
        value = get_args(kind)[1]
        return {"type": "object", "additionalProperties": {} if value is object else type_schema(value, used)}
    if origin is Literal:
        values = list(get_args(kind))
        return {"const": values[0]} if len(values) == 1 else {"enum": values}
    if origin in (Union, types.UnionType):
        return {"anyOf": [type_schema(member, used) for member in get_args(kind)]}
    if isinstance(kind, type) and is_typeddict(kind):
        if used is None:
            return schema_of(kind)
        used.add(kind.__name__)
        return {"$ref": f"#/components/schemas/{kind.__name__}"}
    if kind is object or kind is Any:
        return {}
    return {"type": "object"}


def schema_of(reply: type, used: set[str] | None = None) -> dict[str, object]:
    hints = get_type_hints(reply, include_extras=True)
    total = getattr(reply, "__total__", True)
    required = sorted(name for name, kind in hints.items() if get_origin(kind) is Required or (total and get_origin(kind) is not NotRequired))
    return {"type": "object", "required": required, "properties": {name: type_schema(kind, used) for name, kind in hints.items()}, "additionalProperties": False}


def annotated_type(annotation: object, where: str) -> type | None:
    if get_origin(annotation) is not Annotated:
        return None
    for extra in get_args(annotation)[1:]:
        if isinstance(extra, type) and is_typeddict(extra):
            if extra.__module__ != contract.__name__:
                raise ContractError(f"{where}: {extra.__name__} must be defined in darkroom/server/contract.py")
            return extra
    return None


def handler_types(route: routes.Route) -> tuple[type | None, type | None]:
    where = f"{route.handler.__module__}.{route.handler.__name__}"
    hints = get_type_hints(route.handler, include_extras=True)
    parameters = list(inspect.signature(route.handler).parameters)
    body = annotated_type(hints.get(parameters[0]), where) if parameters else None
    reply = annotated_type(hints.get("return"), where)
    if body is not None and reply is None:
        raise ContractError(f"{where}: its body is typed but not its reply; type both, or neither")
    if body is not None and route.method == "GET":
        raise ContractError(f"{where}: a GET has no body to type")
    return body, reply


def build() -> dict[str, object]:
    used: set[str] = set()
    paths: dict[str, dict[str, object]] = {}
    for route in routes.ROUTES:
        body, reply = handler_types(route)
        operation: dict[str, object] = {"operationId": route.handler.__name__, "x-auth": route.auth}
        if body is not None:
            operation["requestBody"] = {"required": True, "content": {"application/json": {"schema": type_schema(body, used)}}}
        success: dict[str, object] = {"description": "OK"}
        if reply is not None:
            success["content"] = {"application/json": {"schema": type_schema(reply, used)}}
        operation["responses"] = {"2XX": success, "default": {"$ref": "#/components/responses/Error"}}
        paths.setdefault(route.path, {})[route.method.lower()] = operation
    known = contract_types()
    done: dict[str, dict[str, object]] = {}
    while missing := sorted(used - set(done)):
        for name in missing:
            done[name] = schema_of(known[name], used)
    return {
        "openapi": "3.1.0",
        "info": {"title": "Darkroom", "version": "1",
                 "description": "Generated by tools/api_contract.py (`make api-contract`) from the routes and their handlers' annotations; don't edit by hand."},
        "paths": paths,
        "components": {"schemas": done, "responses": {"Error": ERROR}},
    }


def ts_type(schema: dict[str, Any]) -> str:
    if "$ref" in schema:
        return str(schema["$ref"]).rsplit("/", 1)[1]
    if "const" in schema or "enum" in schema:
        return " | ".join(json.dumps(value) for value in (schema["enum"] if "enum" in schema else [schema["const"]]))
    if "anyOf" in schema:
        return " | ".join(dict.fromkeys(ts_type(member) for member in schema["anyOf"]))
    kind = schema.get("type")
    if kind == "array":
        item = ts_type(schema["items"]) if "items" in schema else "unknown"
        return f"({item})[]" if " | " in item else f"{item}[]"
    if kind == "object":
        if "properties" in schema:
            return "{ " + "; ".join(ts_field(name, value, name in schema.get("required", [])) for name, value in schema["properties"].items()) + " }"
        return f"Record<string, {ts_type(schema.get('additionalProperties', {}))}>"
    return TS_SCALARS.get(str(kind), "unknown")


def ts_field(name: str, schema: dict[str, Any], required: bool) -> str:
    return f"{name}{'' if required else '?'}: {ts_type(schema)}"


def json_schema_of(operation: dict[str, Any], *keys: str) -> dict[str, Any] | None:
    found: Any = operation
    for key in keys:
        if not isinstance(found, dict) or key not in found:
            return None
        found = found[key]
    return found if isinstance(found, dict) else None


def render_ts(document: dict[str, Any]) -> str:
    lines: list[str] = []
    for name, schema in sorted(document["components"]["schemas"].items()):
        if not schema["properties"]:
            lines += [f"export type {name} = Record<string, never>;", ""]
            continue
        lines.append(f"export interface {name} {{")
        lines += [f"  {ts_field(field, value, field in schema['required'])};" for field, value in schema["properties"].items()]
        lines += ["}", ""]
    lines.append("export interface Endpoints {")
    for path, operations in document["paths"].items():
        for method, operation in operations.items():
            reply = json_schema_of(operation, "responses", "2XX", "content", "application/json", "schema")
            if reply is None:
                continue
            body = json_schema_of(operation, "requestBody", "content", "application/json", "schema")
            lines.append(f"  \"{method.upper()} {path}\": {{ body: {ts_type(body) if body else 'never'}; reply: {ts_type(reply)} }};")
    lines.append("}")
    return "\n".join(lines) + "\n"


def render_json(document: dict[str, object]) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def render() -> str:
    return render_json(build())


def wanted() -> dict[Path, str]:
    document = build()
    return {OPENAPI_OUT: render_json(document), TS_OUT: render_ts(document)}


def stale(files: dict[Path, str]) -> list[Path]:
    return [path for path, text in files.items() if not path.exists() or path.read_text() != text]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        files = wanted()
    except ContractError as error:
        print(f"tools/api_contract.py: {error}", file=sys.stderr)
        return 1
    if args.check:
        out_of_date = stale(files)
        for path in out_of_date:
            print(f"{path.relative_to(ROOT)} is stale: run `make api-contract` and commit the result", file=sys.stderr)
        if not out_of_date:
            print("API contract is current.")
        return 1 if out_of_date else 0
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
