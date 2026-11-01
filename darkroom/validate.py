from __future__ import annotations

import json


class Invalid(ValueError):
    pass


def json_object(raw: bytes, *, limit: int = 1_000_000) -> dict[str, object]:
    if len(raw) > limit:
        raise Invalid("the request is too large")
    try:
        value = json.loads(raw or b"{}")
    except ValueError as error:
        raise Invalid("the request is not valid JSON") from error
    if not isinstance(value, dict):
        raise Invalid("the request must be a JSON object")
    return value


def text(body: dict[str, object], name: str, *, required: bool = True, limit: int = 200) -> str:
    value = body.get(name)
    if value is None or value == "":
        if required:
            raise Invalid(f"{name} is required")
        return ""
    if not isinstance(value, str):
        raise Invalid(f"{name} must be text")
    if len(value) > limit:
        raise Invalid(f"{name} is too long")
    return value.strip()


def whole_number(body: dict[str, object], name: str, *, minimum: int = 0, maximum: int = 2**31 - 1) -> int:
    value = body.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise Invalid(f"{name} must be a whole number")
    if not minimum <= value <= maximum:
        raise Invalid(f"{name} must be between {minimum} and {maximum}")
    return value
