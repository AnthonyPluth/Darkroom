from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from typing import Literal, NamedTuple

from darkroom_browser import selectors

SIGNED_IN = "SIGNED_IN"
SIGN_IN_NEEDED = "SIGN_IN_NEEDED"
VERIFY_ITS_YOU = "VERIFY_ITS_YOU"
UNKNOWN = "UNKNOWN"

TAGS = re.compile(r"(?is)<(script|style)\b.*?</\1>|<[^>]+>")
SPACES = re.compile(r"\s+")
VERIFY_PHRASES = ("verify it's you", "verify its you", "confirm it's you", "confirm its you", "verify that it's you", "verify your identity",
                  "to help keep your account safe, google wants to make sure it's really you", "this device isn't recognized", "unusual activity")
SIGN_IN_PHRASES = ("sign in to continue", "use your google account", "sign in with your google account", "choose an account", "to continue to google takeout")
SIGN_IN_FORMS = ("type=\"email\"", "type='email'", "identifierid", "accounts.google.com/signin", "accounts.google.com/v3/signin", "accounts.google.com/accountchooser")
SIGNED_IN_PHRASES = ("manage your exports", "create a new export", "your google data", "export progress", "select data to include", "download your data",
                     "exports available")


def visible_text(page: str) -> str:
    text = html.unescape(TAGS.sub(" ", page))
    return SPACES.sub(" ", text).lower().replace("’", "'")


def classify(page: str) -> str:
    text = visible_text(page)
    lowered = page.lower().replace("’", "'")
    if any(phrase in text for phrase in VERIFY_PHRASES):
        return VERIFY_ITS_YOU
    if any(phrase in text for phrase in SIGN_IN_PHRASES) or any(marker in lowered for marker in SIGN_IN_FORMS):
        return SIGN_IN_NEEDED
    if "takeout" in lowered and any(phrase in text for phrase in SIGNED_IN_PHRASES):
        return SIGNED_IN
    return UNKNOWN


CONFIRMATION_PHRASES = ("export created", "we've started creating a copy", "your export has been scheduled", "export scheduled")
RECURRING = re.compile(r"every 2 months for (?:1 year|a year|12 months)")


@dataclass(frozen=True)
class ScheduleConfirmation:
    frequency: str


def parse_schedule_confirmation(page: str) -> ScheduleConfirmation | None:
    text = visible_text(page)
    if not any(phrase in text for phrase in CONFIRMATION_PHRASES):
        return None
    recurring = RECURRING.search(text)
    return ScheduleConfirmation(recurring.group(0)) if recurring is not None else None


ACCOUNT_LABEL = re.compile(r"""aria-label\s*=\s*["']""" + re.escape(selectors.SIGNED_IN_ACCOUNT_LABEL_PREFIX) + r"""[^"']*?\(([^()"'\s]+@[^()"'\s]+)\)""", re.IGNORECASE)


def signed_in_email(page: str) -> str | None:
    found = ACCOUNT_LABEL.search(page)
    return found.group(1).lower() if found is not None else None


ExportState = Literal["in_progress", "ready", "expired"]
LIST_TAGS = ("ul", "ol", "div", "section")
SIZE_UNITS = {"kb": 1024, "mb": 1024**2, "gb": 1024**3, "tb": 1024**4}
SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*(kb|mb|gb|tb)\b")
PART_NUMBER = re.compile(r"\bpart\s+(\d{1,4})\b")
IN_PROGRESS_PHRASES = ("in progress", "being created", "creating your export", "we're creating")
MAX_EXPORTS = 100
MAX_PARTS = 200


class UnreadableExports(ValueError):
    pass


class ListedPart(NamedTuple):
    number: int
    bytes: int
    href: str


@dataclass(frozen=True)
class ExportListing:
    google_id: str
    created_at: float
    state: ExportState
    expires_at: float | None
    parts: tuple[ListedPart, ...]


@dataclass
class RawLink:
    href: str
    label: str


@dataclass
class RawItem:
    attributes: dict[str, str]
    text: list[str]
    times: dict[str, str]
    links: list[RawLink]


def has_role(attributes: dict[str, str], role: str) -> bool:
    return attributes.get("role", "").lower() == role


class ExportPageReader(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[RawItem] = []
        self.list_stack: list[tuple[str, bool]] = []
        self.current: RawItem | None = None
        self.item_tag: str | None = None
        self.item_depth = 0
        self.skip = 0
        self.link: RawLink | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value or "" for name, value in attrs}
        if tag in ("script", "style"):
            self.skip += 1
            return
        if self.current is not None:
            self.read_inside(tag, attributes)
            return
        if tag in ("ul", "ol") or (tag in LIST_TAGS and has_role(attributes, "list")):
            self.list_stack.append((tag, True))
            return
        if self.list_stack and (tag == "li" or has_role(attributes, "listitem")):
            self.current = RawItem(attributes, [], {}, [])
            self.item_tag = tag
            self.item_depth = 1
        elif tag in LIST_TAGS:
            self.list_stack.append((tag, False))

    def read_inside(self, tag: str, attributes: dict[str, str]) -> None:
        assert self.current is not None
        if tag not in VOID_TAGS:
            self.item_depth += 1
        if tag == "time" and attributes.get(selectors.EXPORT_TIME_ATTRIBUTE) and attributes.get("datetime"):
            self.current.times[attributes[selectors.EXPORT_TIME_ATTRIBUTE]] = attributes["datetime"]
        if tag == "a" and attributes.get("href"):
            self.link = RawLink(attributes["href"], attributes.get("aria-label", ""))
            self.current.links.append(self.link)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
            return
        if self.current is not None:
            if tag not in VOID_TAGS:
                self.item_depth -= 1
            if tag == "a":
                self.link = None
            if self.item_depth == 0:
                self.items.append(self.current)
                self.current = None
            return
        if tag in LIST_TAGS and self.list_stack and self.list_stack[-1][0] == tag:
            self.list_stack.pop()

    def handle_data(self, data: str) -> None:
        if self.skip or self.current is None:
            return
        self.current.text.append(data)
        if self.link is not None:
            self.link.label += " " + data


VOID_TAGS = frozenset({"br", "hr", "img", "input", "meta", "link", "wbr", "area", "base", "col", "embed", "source", "track"})


def read_instant(raw: str) -> float:
    try:
        moment = datetime.fromisoformat(raw.strip())
    except ValueError:
        raise UnreadableExports("an export on the exports page has a date that could not be read") from None
    if moment.tzinfo is None:
        raise UnreadableExports("an export on the exports page has a date with no time zone")
    return moment.timestamp()


def size_in_bytes(text: str) -> int | None:
    found = SIZE.search(text)
    return None if found is None else round(float(found.group(1)) * SIZE_UNITS[found.group(2)])


DOWNLOAD_HREF = re.compile(r"https://" + re.escape(selectors.DOWNLOAD_HOST) + r"/takeout/download(?:[?#].*)?")
EXPORT_PARAMETER = re.compile(r"[?&]j=([^&#\s]+)")


def download_link(href: str) -> str | None:
    cleaned = html.unescape(href).strip()
    return cleaned if DOWNLOAD_HREF.fullmatch(cleaned) else None


def listed_parts(item: RawItem) -> tuple[ListedPart, ...]:
    parts: list[ListedPart] = []
    for link in item.links:
        href = download_link(link.href)
        if href is None:
            continue
        label = SPACES.sub(" ", html.unescape(link.label)).lower().replace("\u2019", "'")
        numbered = PART_NUMBER.search(label)
        number = int(numbered.group(1)) if numbered is not None else len(parts) + 1
        size = size_in_bytes(label)
        if size is None:
            raise UnreadableExports("a download link on the exports page has no readable size")
        parts.append(ListedPart(number, size, href))
    if len({part.number for part in parts}) != len(parts) or len(parts) > MAX_PARTS:
        raise UnreadableExports("an export on the exports page has parts that could not be told apart")
    return tuple(sorted(parts))


def export_id(item: RawItem, parts: tuple[ListedPart, ...]) -> str:
    named = item.attributes.get(selectors.EXPORT_ID_ATTRIBUTE, "").strip()
    if named:
        return named
    for part in parts:
        found = EXPORT_PARAMETER.search(part.href)
        if found is not None:
            return found.group(1)
    raise UnreadableExports("an export on the exports page has no identifier")


def export_state(text: str, parts: tuple[ListedPart, ...]) -> ExportState:
    if parts:
        return "ready"
    if re.search(r"\bexpired\b", text):
        return "expired"
    if any(phrase in text for phrase in IN_PROGRESS_PHRASES):
        return "in_progress"
    raise UnreadableExports("an export on the exports page is in a state that was not recognised")


def is_export(item: RawItem) -> bool:
    return bool(item.times) or any(download_link(link.href) is not None for link in item.links) or selectors.EXPORT_ID_ATTRIBUTE in item.attributes


def listing_of(item: RawItem) -> ExportListing:
    text = SPACES.sub(" ", html.unescape(" ".join(item.text))).lower().replace("\u2019", "'")
    parts = listed_parts(item)
    created = item.times.get(selectors.EXPORT_CREATED_TIME)
    if created is None:
        raise UnreadableExports("an export on the exports page has no creation date")
    expires = item.times.get(selectors.EXPORT_EXPIRES_TIME)
    return ExportListing(export_id(item, parts), read_instant(created), export_state(text, parts), None if expires is None else read_instant(expires), parts)


def parse_exports(page: str) -> list[ExportListing]:
    if "manage your exports" not in visible_text(page):
        raise UnreadableExports("the page is not Takeout's list of exports")
    reader = ExportPageReader()
    reader.feed(page)
    reader.close()
    listings = [listing_of(item) for item in reader.items if is_export(item)]
    if len(listings) > MAX_EXPORTS or len({listing.google_id for listing in listings}) != len(listings):
        raise UnreadableExports("the exports page lists exports that could not be told apart")
    return listings


def exports_page_ready(page: str) -> bool:
    return "manage your exports" in visible_text(page)
