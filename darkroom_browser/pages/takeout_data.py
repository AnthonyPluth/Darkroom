from __future__ import annotations

import json
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import NamedTuple


class ExportSlots(NamedTuple):
    arity: int
    google_id: int
    created_ms: int
    expires_ms_when_ready: int
    expires_ms_when_expired: int
    parts: int


class PartSlots(NamedTuple):
    arity: int
    file_name: int
    size: int
    google_downloads: int
    position: int


EXPORT = ExportSlots(arity=32, google_id=1, created_ms=22, expires_ms_when_ready=24, expires_ms_when_expired=25, parts=8)
PART = PartSlots(arity=9, file_name=0, size=1, google_downloads=2, position=8)

CALLBACK = "AF_initDataCallback("
MAX_PAGE_CHARS = 32 * 1024 * 1024
MAX_SCRIPT_CHARS = 8 * 1024 * 1024
MAX_BLOCKS = 16
MAX_BLOCK_CHARS = 4 * 1024 * 1024
MAX_DEPTH = 24
MAX_VALUES = 250_000
MAX_DIGITS = 40
MAX_FIELDS = 8
MAX_ROWS = 400
MAX_EXPORTS = 100
MAX_PARTS = 200
MAX_PART_BYTES = 1 << 40
MAX_GOOGLE_DOWNLOADS = 1000
EARLIEST_MS = 1_420_070_400_000
LATEST_MS = 4_102_444_800_000
FUTURE_SLACK_MS = 3_600_000
LONGEST_LIFE_MS = 90 * 86_400_000
KEY = re.compile(r"ds:[0-9]{1,3}")
FIELD_NAME = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]{0,63}")
BARE = re.compile(r"[A-Za-z0-9_.$+-]{1,64}")
SPACE = re.compile(r"\s*")
TOKEN = re.compile(r""""(?:[^"\\\n\r]|\\.)*"|'(?:[^'\\\n\r]|\\.)*'|[\[\]{},"']""")
EXPORT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
ZIP_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,195}\.zip")

NONE_ON_THE_PAGE = "none on the page"
TOO_LARGE = "it was too large"
TOO_DEEP = "it was nested too deeply"
TOO_MANY = "it held too many values"
NOT_DATA = "it was not plain data"
UNCHECKED = "it did not pass its checks"


class Refused(ValueError):
    pass


class EmbeddedPart(NamedTuple):
    position: int
    file_name: str
    bytes: int
    google_downloads: int


@dataclass(frozen=True)
class EmbeddedExport:
    google_id: str
    created_at: float
    expires_at: float | None
    parts: tuple[EmbeddedPart, ...] | None


@dataclass(frozen=True)
class Embedded:
    exports: dict[str, EmbeddedExport] = field(default_factory=dict)
    unused: str | None = None


class ScriptReader(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.inside = False
        self.current: list[str] = []
        self.size = 0
        self.scripts: list[str] = []
        self.oversized = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self.inside, self.current, self.size = True, [], 0

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.inside:
            self.inside = False
            if self.size > MAX_SCRIPT_CHARS:
                self.oversized = True
            else:
                self.scripts.append("".join(self.current))

    def handle_data(self, data: str) -> None:
        if not self.inside:
            return
        self.size += len(data)
        if self.size <= MAX_SCRIPT_CHARS:
            self.current.append(data)


def skip_space(text: str, at: int) -> int:
    found = SPACE.match(text, at)
    return at if found is None else found.end()


def balanced_end(text: str, start: int) -> int:
    limit = min(len(text), start + MAX_BLOCK_CHARS)
    cut_short = Refused(TOO_LARGE if limit < len(text) else NOT_DATA)
    depth = values = 0
    for token in TOKEN.finditer(text, start, limit):
        mark = token.group()
        if mark in "[{":
            depth += 1
            values += 1
            if depth > MAX_DEPTH:
                raise Refused(TOO_DEEP)
        elif mark in "]}":
            depth -= 1
            if depth <= 0:
                return token.end()
        elif mark == ",":
            values += 1
            if values > MAX_VALUES:
                raise Refused(TOO_MANY)
        elif len(mark) == 1:
            raise cut_short
    raise cut_short


def quoted_end(text: str, start: int) -> int:
    found = TOKEN.match(text, start, min(len(text), start + MAX_BLOCK_CHARS))
    if found is None or len(found.group()) < 2:
        raise Refused(NOT_DATA)
    return found.end()


def value_end(text: str, at: int) -> int:
    if at >= len(text):
        raise Refused(NOT_DATA)
    if text[at] in "[{":
        return balanced_end(text, at)
    if text[at] in "\"'":
        return quoted_end(text, at)
    found = BARE.match(text, at)
    if found is None or text[skip_space(text, found.end()):skip_space(text, found.end()) + 1] == "(":
        raise Refused(NOT_DATA)
    return found.end()


def read_call(text: str, at: int) -> tuple[str | None, str | None]:
    at = skip_space(text, at + len(CALLBACK))
    if text[at:at + 1] != "{":
        raise Refused(NOT_DATA)
    at += 1
    fields: dict[str, str] = {}
    while True:
        at = skip_space(text, at)
        if text[at:at + 1] == "}":
            break
        name = FIELD_NAME.match(text, at)
        if name is None or name.group() in fields or len(fields) >= MAX_FIELDS:
            raise Refused(NOT_DATA)
        at = skip_space(text, name.end())
        if text[at:at + 1] != ":":
            raise Refused(NOT_DATA)
        start = skip_space(text, at + 1)
        at = value_end(text, start)
        fields[name.group()] = text[start:at]
        at = skip_space(text, at)
        if text[at:at + 1] == ",":
            at += 1
        elif text[at:at + 1] != "}":
            raise Refused(NOT_DATA)
    key = fields.get("key", "")
    plain_key = key[1:-1] if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'" else None
    return (plain_key if plain_key is not None and KEY.fullmatch(plain_key) else None), fields.get("data")


def no_constant(name: str) -> object:
    raise Refused(NOT_DATA)


def finite(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise Refused(NOT_DATA)
    return value


def bounded_int(text: str) -> int:
    if len(text.lstrip("-")) > MAX_DIGITS:
        raise Refused(NOT_DATA)
    return int(text)


def no_repeats(pairs: list[tuple[str, object]]) -> dict[str, object]:
    found = dict(pairs)
    if len(found) != len(pairs):
        raise Refused(NOT_DATA)
    return found


def plain_data(raw: str) -> object:
    try:
        return json.loads(raw, parse_constant=no_constant, parse_float=finite, parse_int=bounded_int, object_pairs_hook=no_repeats)
    except (ValueError, RecursionError) as error:
        if isinstance(error, Refused):
            raise
        raise Refused(NOT_DATA) from error


def calls_in(script: str) -> Iterator[int]:
    at = script.find(CALLBACK)
    while at >= 0:
        yield at
        at = script.find(CALLBACK, at + len(CALLBACK))


def is_export_row(value: list[object]) -> bool:
    found = value[EXPORT.google_id] if len(value) == EXPORT.arity else None
    return isinstance(found, str) and EXPORT_ID.fullmatch(found) is not None


def rows_in(value: object, depth: int, found: list[list[object]]) -> None:
    if not isinstance(value, list) or depth > MAX_DEPTH:
        return
    if is_export_row(value):
        if len(found) >= MAX_ROWS:
            raise Refused(UNCHECKED)
        found.append(value)
        return
    for item in value:
        rows_in(item, depth + 1, found)


def whole(value: object, lowest: int, highest: int) -> int:
    if type(value) is not int or not lowest <= value <= highest:
        raise Refused(UNCHECKED)
    return value


def part_of(raw: object, count: int) -> EmbeddedPart:
    if not isinstance(raw, list) or len(raw) != PART.arity:
        raise Refused(UNCHECKED)
    name = raw[PART.file_name]
    if not isinstance(name, str) or ZIP_NAME.fullmatch(name) is None:
        raise Refused(UNCHECKED)
    return EmbeddedPart(whole(raw[PART.position], 0, count - 1), name, whole(raw[PART.size], 1, MAX_PART_BYTES), whole(raw[PART.google_downloads], 0, MAX_GOOGLE_DOWNLOADS))


def parts_of(raw: object) -> tuple[EmbeddedPart, ...] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_PARTS:
        raise Refused(UNCHECKED)
    parts = tuple(sorted(part_of(each, len(raw)) for each in raw))
    if [part.position for part in parts] != list(range(len(parts))) or len({part.file_name for part in parts}) != len(parts):
        raise Refused(UNCHECKED)
    return parts


def export_of(row: list[object], now: float | None) -> EmbeddedExport:
    latest = LATEST_MS if now is None else min(LATEST_MS, int(now * 1000) + FUTURE_SLACK_MS)
    created = whole(row[EXPORT.created_ms], EARLIEST_MS, latest)
    parts = parts_of(row[EXPORT.parts])
    raw_expiry = row[EXPORT.expires_ms_when_ready if parts is not None else EXPORT.expires_ms_when_expired]
    expires = None if raw_expiry is None else whole(raw_expiry, created + 1, created + LONGEST_LIFE_MS)
    if parts is not None and expires is None:
        raise Refused(UNCHECKED)
    return EmbeddedExport(str(row[EXPORT.google_id]), created / 1000, None if expires is None else expires / 1000, parts)


def blocks_of(page: str) -> tuple[list[object], str | None]:
    reader = ScriptReader()
    reader.feed(page)
    reader.close()
    refusal = TOO_LARGE if reader.oversized else None
    calls = [(script, at) for script in reader.scripts for at in calls_in(script)]
    if len(calls) > MAX_BLOCKS:
        return [], TOO_MANY
    found: list[object] = []
    for script, at in calls:
        try:
            key, raw = read_call(script, at)
            if key is not None and raw is not None:
                found.append(plain_data(raw))
        except Refused as error:
            refusal = refusal or str(error)
    return found, refusal


def read_embedded(page: str, now: float | None = None) -> Embedded:
    if len(page) > MAX_PAGE_CHARS:
        return Embedded(unused=TOO_LARGE)
    blocks, refusal = blocks_of(page)
    rows: list[list[object]] = []
    try:
        for block in blocks:
            rows_in(block, 0, rows)
        exports: dict[str, EmbeddedExport] = {}
        for row in rows:
            export = export_of(row, now)
            if exports.setdefault(export.google_id, export) != export:
                raise Refused(UNCHECKED)
    except Refused as error:
        return Embedded(unused=str(error))
    if not exports:
        return Embedded(unused=refusal or NONE_ON_THE_PAGE)
    if len(exports) > MAX_EXPORTS:
        return Embedded(unused=UNCHECKED)
    return Embedded(exports)
