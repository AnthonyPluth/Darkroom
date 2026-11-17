from __future__ import annotations

import html
import re
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, tzinfo
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
SIGN_IN_ACTIONS = ("https://accounts.google.com/signin", "https://accounts.google.com/v3/signin", "https://accounts.google.com/accountchooser")
SIGNED_IN_PHRASES = ("manage your exports", "create a new export", "your google data", "export progress", "select data to include", "download your data",
                     "exports available")


def visible_text(page: str) -> str:
    text = html.unescape(TAGS.sub(" ", page))
    return SPACES.sub(" ", text).lower().replace("’", "'")


class SignInFormReader(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: (value or "").lower() for name, value in attrs}
        if tag == "input" and (attributes.get("type") == "email" or attributes.get("id") == "identifierid"):
            self.found = True
        if tag == "form" and attributes.get("action", "").startswith(SIGN_IN_ACTIONS):
            self.found = True


def classify(page: str) -> str:
    text = visible_text(page)
    lowered = page.lower().replace("’", "'")
    if any(phrase in text for phrase in VERIFY_PHRASES):
        return VERIFY_ITS_YOU
    form = SignInFormReader()
    form.feed(page)
    form.close()
    if any(phrase in text for phrase in SIGN_IN_PHRASES) or form.found:
        return SIGN_IN_NEEDED
    if "takeout" in lowered and any(phrase in text for phrase in SIGNED_IN_PHRASES):
        return SIGNED_IN
    return UNKNOWN


CONFIRMATION_PHRASES = ("export created", "we've started creating a copy", "your export has been scheduled", "export scheduled", "google is creating a copy of data from google photos")
RECURRING = re.compile(r"(?:every (?:1 |one )?month|monthly) for (?:1 year|a year|12 months)")


@dataclass(frozen=True)
class ScheduleConfirmation:
    frequency: str


def parse_schedule_confirmation(page: str) -> ScheduleConfirmation | None:
    text = visible_text(page)
    if not any(phrase in text for phrase in CONFIRMATION_PHRASES):
        return None
    recurring = RECURRING.search(text)
    if recurring is not None:
        return ScheduleConfirmation(recurring.group(0))
    existing = parse_existing_schedule(page)
    if existing is not None and existing.is_monthly:
        return ScheduleConfirmation(f"{existing.total} scheduled exports")
    return None


SCHEDULE_BANNER = "more scheduled export"
EXISTING_SCHEDULE = re.compile(r"export (\d+) of (\d+) will start on ([a-z]+ \d{1,2}, \d{4})")
MONTHS_IN_SCHEDULE = 12


@dataclass(frozen=True)
class ExistingSchedule:
    next_number: int
    total: int
    next_start: datetime

    @property
    def is_monthly(self) -> bool:
        return self.total == MONTHS_IN_SCHEDULE


def has_schedule_banner(page: str) -> bool:
    return SCHEDULE_BANNER in visible_text(page)


def parse_existing_schedule(page: str) -> ExistingSchedule | None:
    text = visible_text(page)
    if SCHEDULE_BANNER not in text:
        return None
    found = EXISTING_SCHEDULE.search(text)
    if found is None:
        return None
    number, total = int(found.group(1)), int(found.group(2))
    if not 1 <= number <= total or MONTHS_IN_SCHEDULE % total != 0:
        return None
    try:
        start = datetime.strptime(found.group(3), "%B %d, %Y")
    except ValueError:
        return None
    return ExistingSchedule(number, total, start)


ACCOUNT_LABEL = re.compile(r"""aria-label\s*=\s*["']""" + re.escape(selectors.SIGNED_IN_ACCOUNT_LABEL_PREFIX) + r"""[^"']*?\(([^()"'\s]+@[^()"'\s]+)\)""", re.IGNORECASE)


def signed_in_email(page: str) -> str | None:
    found = ACCOUNT_LABEL.search(page)
    return found.group(1).lower() if found is not None else None




ExportState = Literal["in_progress", "ready", "expired"]
SIZE_UNITS = {"kb": 1024, "mb": 1024**2, "gb": 1024**3, "tb": 1024**4}
SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*(kb|mb|gb|tb)\b")
MAX_EXPORTS = 100
MAX_PARTS = 200
SUMMARY_PHRASE = "the status of the requests you've made"
EMPTY_SUMMARY_PHRASES = ("haven't made any requests", "have not made any requests", "no requests")
STATE_WORDS: dict[str, ExportState] = {"completed": "ready", "expired": "expired", "in progress": "in_progress"}
ARCHIVE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
ARCHIVE_HREF = re.compile(re.escape(selectors.TAKEOUT_ARCHIVE_URL) + r"([A-Za-z0-9_-]{1,128})")
MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december")
MONTH_DAY = r"([a-z]+) (\d{1,2})"
CLOCK = r"(\d{1,2}):(\d{2}) ?([ap])\.?m\.?"
RELATIVE = re.compile(r"created (\d{1,3}|an?|one) (second|minute|hour|day|week)s? ago")
UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 7 * 86400}
JUST_NOW = ("created just now", "created moments ago", "created a moment ago")
CREATED = re.compile(rf"created {MONTH_DAY}(?:, (\d{{4}}))?(?:,? (?:at )?{CLOCK})?")
AVAILABLE = re.compile(rf"available until {MONTH_DAY}, (\d{{4}})(?:,? (?:at )?{CLOCK})?")
CALENDAR_DATE = re.compile(rf"{MONTH_DAY}, (\d{{4}})")
FUTURE_SLACK = 3600.0
YEARS_BACK = 8
SUMMARY_FIELD = re.compile(r"(overall status|creation date|expiration date): (.+)")
PARTS_HEADING = "data backup to a download link"
PART_LABEL = re.compile(r"download part (\d{1,4}) of (\d{1,4})")
KNOWN_HEADINGS = ("google takeout", "summary", "status", "your exports", "your latest export", "create a new export", "manage your exports", "your google data",
                  "data backup to a download link", "google photos", "export summary", "sign in", "verify it's you", "choose an account")
MAX_LANDMARKS = 8


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
    archive_url: str


@dataclass(frozen=True)
class ArchiveDetail:
    state: ExportState
    created_on: date
    expires_on: date | None
    parts: tuple[ListedPart, ...]


def plain(text: str) -> str:
    return SPACES.sub(" ", text.replace(" ", " ").replace("\xa0", " ").replace("’", "'")).strip().lower()


def archive_url(google_id: str) -> str:
    if not ARCHIVE_ID.fullmatch(google_id):
        raise UnreadableExports("the export id is not one Takeout uses")
    return selectors.TAKEOUT_ARCHIVE_URL + google_id


@dataclass
class SummaryRow:
    google_id: str
    chunks: list[str]


class SkippingReader(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self.skip += 1
        elif not self.skip:
            self.opened(tag, {name: value or "" for name, value in attrs})

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif not self.skip:
            self.closed(tag)

    def handle_data(self, data: str) -> None:
        if not self.skip and data.strip():
            self.text(data)

    def opened(self, tag: str, attributes: dict[str, str]) -> None:
        return None

    def closed(self, tag: str) -> None:
        return None

    def text(self, data: str) -> None:
        return None


class SummaryReader(SkippingReader):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[SummaryRow] = []
        self.current: SummaryRow | None = None
        self.chunks: list[str] = []

    def opened(self, tag: str, attributes: dict[str, str]) -> None:
        if tag != "a" or self.current is not None:
            return
        found = ARCHIVE_HREF.fullmatch(attributes.get("href", "").strip())
        if found is not None:
            self.current = SummaryRow(found.group(1), [])

    def closed(self, tag: str) -> None:
        if tag == "a" and self.current is not None:
            self.rows.append(self.current)
            self.current = None

    def text(self, data: str) -> None:
        self.chunks.append(plain(data))
        if self.current is not None:
            self.current.chunks.append(plain(data))


def read_summary(page: str) -> SummaryReader:
    reader = SummaryReader()
    reader.feed(page)
    reader.close()
    return reader


def is_summary(reader: SummaryReader) -> bool:
    return any(chunk.startswith(SUMMARY_PHRASE) for chunk in reader.chunks)


def says_empty(reader: SummaryReader) -> bool:
    return any(phrase in chunk for chunk in reader.chunks for phrase in EMPTY_SUMMARY_PHRASES)


def wall_clock(month: str, day: str, year: int, clock: tuple[str | None, str | None, str | None], zone: tzinfo) -> datetime:
    if month not in MONTHS:
        raise ValueError(month)
    hour_text, minute_text, meridiem = clock
    hour, minute = 0, 0
    if hour_text is not None and minute_text is not None:
        hour, minute = int(hour_text), int(minute_text)
        if not 1 <= hour <= 12:
            raise ValueError(hour_text)
        hour = hour % 12 + (12 if meridiem == "p" else 0)
    return datetime(year, MONTHS.index(month) + 1, int(day), hour, minute, tzinfo=zone)


def nearest_past(month: str, day: str, clock: tuple[str | None, str | None, str | None], now: float, zone: tzinfo) -> float:
    this_year = datetime.fromtimestamp(now, zone).year
    for year in range(this_year, this_year - YEARS_BACK, -1):
        try:
            moment = wall_clock(month, day, year, clock, zone).timestamp()
        except ValueError:
            continue
        if moment <= now + FUTURE_SLACK:
            return moment
    raise ValueError(month)


def created_at(chunk: str, now: float, zone: tzinfo) -> float:
    if chunk in JUST_NOW:
        return now
    relative = RELATIVE.fullmatch(chunk)
    if relative is not None:
        count = int(relative.group(1)) if relative.group(1).isdigit() else 1
        return now - count * UNIT_SECONDS[relative.group(2)]
    found = CREATED.fullmatch(chunk)
    if found is None:
        raise ValueError(chunk)
    month, day, year = found.group(1), found.group(2), found.group(3)
    clock = (found.group(4), found.group(5), found.group(6))
    if year is None:
        return nearest_past(month, day, clock, now, zone)
    return wall_clock(month, day, int(year), clock, zone).timestamp()


def available_until(chunk: str, zone: tzinfo) -> float:
    found = AVAILABLE.fullmatch(chunk)
    if found is None:
        raise ValueError(chunk)
    return wall_clock(found.group(1), found.group(2), int(found.group(3)), (found.group(4), found.group(5), found.group(6)), zone).timestamp()


def listing_of(row: SummaryRow, now: float, zone: tzinfo) -> ExportListing:
    states = [STATE_WORDS[chunk] for chunk in row.chunks if chunk in STATE_WORDS]
    if len(states) != 1:
        raise UnreadableExports("an export on Takeout's summary is in a state that was not recognised")
    created = [chunk for chunk in row.chunks if chunk.startswith("created ")]
    available = [chunk for chunk in row.chunks if chunk.startswith("available until ")]
    if len(created) != 1 or len(available) > 1:
        raise UnreadableExports("an export on Takeout's summary has no readable creation date")
    try:
        created_moment = created_at(created[0], now, zone)
        expires = available_until(available[0], zone) if available else None
    except ValueError:
        raise UnreadableExports("an export on Takeout's summary has a date that could not be read") from None
    return ExportListing(row.google_id, created_moment, states[0], expires, (), archive_url(row.google_id))


def parse_exports(page: str, now: float, zone: tzinfo) -> list[ExportListing]:
    reader = read_summary(page)
    if not is_summary(reader):
        raise UnreadableExports("the page is not Takeout's summary of exports")
    listings = [listing_of(row, now, zone) for row in reader.rows]
    if len(listings) > MAX_EXPORTS or len({listing.google_id for listing in listings}) != len(listings):
        raise UnreadableExports("Takeout's summary lists exports that could not be told apart")
    return listings


def exports_page_ready(page: str) -> bool:
    reader = read_summary(page)
    return is_summary(reader) and (bool(reader.rows) or says_empty(reader))


def size_in_bytes(text: str) -> int | None:
    found = SIZE.search(text)
    return None if found is None else round(float(found.group(1)) * SIZE_UNITS[found.group(2)])


DOWNLOAD_HREF = re.compile(r"https://" + re.escape(selectors.DOWNLOAD_HOST) + r"/takeout/download(?:[?#].*)?")
EXPORT_PARAMETER = re.compile(r"[?&]j=([^&#\s]+)")


def download_link(href: str) -> str | None:
    cleaned = html.unescape(href).strip()
    return cleaned if DOWNLOAD_HREF.fullmatch(cleaned) else None


@dataclass
class PartItem:
    links: list[tuple[str, str]]
    sizes: list[str]
    chunks: list[str]


class ArchiveReader(SkippingReader):
    def __init__(self) -> None:
        super().__init__()
        self.chunks: list[str] = []
        self.items: list[PartItem] = []
        self.heading: list[str] | None = None
        self.in_parts = False
        self.current: PartItem | None = None
        self.depth = 0

    def opened(self, tag: str, attributes: dict[str, str]) -> None:
        if tag == "h2":
            self.heading, self.in_parts = [], False
        if self.current is not None:
            self.depth += tag == "li"
            if tag == "a" and attributes.get("href"):
                self.current.links.append((attributes["href"], plain(attributes.get("aria-label", ""))))
            if "data-size" in attributes:
                self.current.sizes.append(attributes["data-size"].strip())
        elif tag == "li" and self.in_parts:
            self.current, self.depth = PartItem([], [], []), 1

    def closed(self, tag: str) -> None:
        if tag == "h2" and self.heading is not None:
            self.in_parts = plain(" ".join(self.heading)).startswith(PARTS_HEADING)
            self.heading = None
        if tag == "li" and self.current is not None:
            self.depth -= 1
            if self.depth == 0:
                self.items.append(self.current)
                self.current = None

    def text(self, data: str) -> None:
        self.chunks.append(plain(data))
        if self.heading is not None:
            self.heading.append(data)
        if self.current is not None:
            self.current.chunks.append(plain(data))


def read_archive(page: str) -> ArchiveReader:
    reader = ArchiveReader()
    reader.feed(page)
    reader.close()
    return reader


def summary_fields(chunks: list[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for chunk in chunks:
        found = SUMMARY_FIELD.fullmatch(chunk)
        if found is None:
            continue
        if fields.setdefault(found.group(1), found.group(2)) != found.group(2):
            raise UnreadableExports("an export's page on Takeout shows two different summaries")
    return fields


def numbered_link(item: PartItem) -> list[tuple[str, re.Match[str]]]:
    return [(href, found) for href, label in item.links if (found := PART_LABEL.fullmatch(label)) is not None]


def part_of(item: PartItem, google_id: str) -> tuple[ListedPart, int]:
    numbered = numbered_link(item)
    if len(numbered) != 1:
        raise UnreadableExports("a download on an export's page on Takeout has no readable part number")
    raw, label = numbered[0]
    href = download_link(raw)
    owner = None if href is None else EXPORT_PARAMETER.search(href)
    if href is None or owner is None or owner.group(1) != google_id:
        raise UnreadableExports("a download link on an export's page on Takeout is not one Darkroom can use")
    exact = set(item.sizes)
    if len(exact) == 1 and (value := exact.pop()).isdigit():
        size: int | None = int(value)
    else:
        size = size_in_bytes(" ".join(item.chunks))
    if size is None:
        raise UnreadableExports("a download on an export's page on Takeout has no readable size")
    return ListedPart(int(label.group(1)), size, href), int(label.group(2))


def archive_parts(items: list[PartItem], google_id: str) -> tuple[ListedPart, ...]:
    read = [part_of(item, google_id) for item in items if item.links]
    parts = sorted(part for part, _ in read)
    totals = {total for _, total in read}
    if len(parts) > MAX_PARTS or totals != {len(parts)} or [part.number for part in parts] != list(range(1, len(parts) + 1)):
        raise UnreadableExports("an export's page on Takeout lists parts that could not be told apart")
    return tuple(parts)


def calendar_date(text: str) -> date:
    found = CALENDAR_DATE.fullmatch(text)
    if found is None:
        raise ValueError(text)
    return wall_clock(found.group(1), found.group(2), int(found.group(3)), (None, None, None), UTC).date()


def parse_archive(page: str, google_id: str) -> ArchiveDetail:
    reader = read_archive(page)
    fields = summary_fields(reader.chunks)
    state = STATE_WORDS.get(fields.get("overall status", ""))
    if state is None:
        raise UnreadableExports("an export's page on Takeout is in a state that was not recognised")
    try:
        created_on = calendar_date(fields.get("creation date", ""))
        expires = fields.get("expiration date")
        expires_on = None if expires is None else calendar_date(expires)
    except ValueError:
        raise UnreadableExports("an export's page on Takeout has a date that could not be read") from None
    parts = archive_parts(reader.items, google_id) if state == "ready" else ()
    if state == "ready" and not parts:
        raise UnreadableExports("a completed export's page on Takeout lists no parts")
    return ArchiveDetail(state, created_on, expires_on, parts)


def archive_page_ready(page: str) -> bool:
    reader = read_archive(page)
    status = next((found.group(2) for chunk in reader.chunks if (found := SUMMARY_FIELD.fullmatch(chunk)) and found.group(1) == "overall status"), None)
    if status is None:
        return False
    return STATE_WORDS.get(status) != "ready" or any(numbered_link(item) for item in reader.items)


def with_archive(listing: ExportListing, detail: ArchiveDetail, zone: tzinfo) -> ExportListing:
    if detail.state != listing.state:
        raise UnreadableExports("an export's page on Takeout and the summary disagree about its state")
    listed_on = datetime.fromtimestamp(listing.created_at, zone).date()
    if abs((listed_on - detail.created_on).days) > 1:
        raise UnreadableExports("an export's page on Takeout and the summary disagree about when it was created")
    expires = listing.expires_at
    if expires is None and detail.expires_on is not None:
        expires = datetime.combine(detail.expires_on, time(), zone).timestamp()
    return replace(listing, expires_at=expires, parts=detail.parts)


class LandmarkReader(SkippingReader):
    def __init__(self) -> None:
        super().__init__()
        self.title: str | None = None
        self.headings: list[str] = []
        self.open: str | None = None
        self.words: list[str] = []

    def opened(self, tag: str, attributes: dict[str, str]) -> None:
        if tag in ("title", "h1", "h2") and self.open is None:
            self.open, self.words = tag, []

    def closed(self, tag: str) -> None:
        if tag != self.open:
            return
        text = plain(" ".join(self.words))
        if tag == "title":
            self.title = text if self.title is None else self.title
        else:
            self.headings.append(text)
        self.open = None

    def text(self, data: str) -> None:
        if self.open is not None:
            self.words.append(data)


def known_heading(text: str) -> str | None:
    return next((known for known in KNOWN_HEADINGS if text == known or text.startswith(known + " ")), None)


def landmarks(page: str) -> str:
    reader = LandmarkReader()
    reader.feed(page)
    reader.close()
    said: list[str] = []
    if reader.title is not None:
        title = known_heading(reader.title)
        said.append("an unrecognised title" if title is None else f"title '{title}'")
    named = [known for known in map(known_heading, reader.headings) if known is not None][:MAX_LANDMARKS]
    others = len(reader.headings) - len(named)
    if named:
        heading = f"heading{'s' if len(named) > 1 else ''} " + ", ".join(f"'{name}'" for name in named)
        said.append(heading + (f" and {others} other{'s' if others > 1 else ''}" if others else ""))
    elif others:
        said.append(f"{others} other heading{'s' if others > 1 else ''}")
    return "; ".join(said) or "no title or headings"
