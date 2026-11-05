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
    stripped = value.strip()
    if required and not stripped:
        raise Invalid(f"{name} is required")
    return stripped


def whole_number(body: dict[str, object], name: str, *, minimum: int = 0, maximum: int = 2**31 - 1) -> int:
    value = body.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise Invalid(f"{name} must be a whole number")
    if not minimum <= value <= maximum:
        raise Invalid(f"{name} must be between {minimum} and {maximum}")
    return value


def boolean(body: dict[str, object], name: str, *, required: bool = True) -> bool:
    value = body.get(name)
    if value is None and not required:
        return False
    if not isinstance(value, bool):
        raise Invalid(f"{name} must be true or false")
    return value


def present(body: dict[str, object], name: str) -> bool:
    return name in body


def text_or_none(body: dict[str, object], name: str, *, limit: int = 200) -> str | None:
    if body.get(name) is None:
        return None
    return text(body, name, required=False, limit=limit) or None


def whole_numbers(body: dict[str, object], name: str, *, minimum: int = 1, maximum: int = 2**31 - 1, most: int = 50) -> list[int]:
    value = body.get(name)
    if not isinstance(value, list):
        raise Invalid(f"{name} must be a list of whole numbers")
    if len(value) > most:
        raise Invalid(f"{name} has too many entries")
    found: list[int] = []
    for entry in value:
        if isinstance(entry, bool) or not isinstance(entry, int) or not minimum <= entry <= maximum:
            raise Invalid(f"{name} must be a list of whole numbers")
        if entry in found:
            raise Invalid(f"{name} lists the same number twice")
        found.append(entry)
    return found


def email(body: dict[str, object], name: str) -> str:
    value = text(body, name, limit=254)
    local, at, domain = value.partition("@")
    if not at or not local or "." not in domain or domain.startswith(".") or domain.endswith(".") or any(c.isspace() or c in "<>(),;:\\\"" for c in value) or "@" in domain:
        raise Invalid(f"{name} must be an email address")
    return value
