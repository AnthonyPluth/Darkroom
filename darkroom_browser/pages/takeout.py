from __future__ import annotations

import html
import re
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from html.parser import HTMLParser
from typing import Literal, NamedTuple

from darkroom_browser import selectors
from darkroom_browser.pages import takeout_data

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
MONTH_NUMBERS = {name: number for number, name in enumerate(MONTHS, start=1)} | {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12}
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
WEEKDAY_NUMBERS = {name: number for number, name in enumerate(WEEKDAYS)} | {
    "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3, "fri": 4, "sat": 5, "sun": 6}
MONTH_NAME = "(?:" + "|".join(sorted(MONTH_NUMBERS, key=len, reverse=True)) + r")\.?"
WEEKDAY_NAME = "(?:" + "|".join(sorted(WEEKDAY_NUMBERS, key=len, reverse=True)) + r")\.?"
CLOCK = r"(?:,? (?:at )?(?P<hour>\d{1,2}):(?P<minute>\d{2})(?: ?(?P<meridiem>[ap])\.?m\.?)?)?"
CALENDAR = re.compile(rf"(?:(?P<weekday>{WEEKDAY_NAME}),? )?(?:(?P<month>{MONTH_NAME}) (?P<day>\d{{1,2}})|(?P<day_first>\d{{1,2}}) (?P<month_after>{MONTH_NAME}))(?:,? (?P<year>\d{{4}}))?{CLOCK}")
RELATIVE = re.compile(r"(?P<count>\d{1,3}|an?|one) (?P<unit>second|minute|hour|day|week|month|year)s? ago")
DAY_WORD = re.compile(rf"(?P<word>today|yesterday){CLOCK}")
WEEKDAY_ONLY = re.compile(rf"(?P<weekday>{WEEKDAY_NAME}){CLOCK}")
UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 7 * 86400, "month": 30 * 86400, "year": 365 * 86400}
JUST_NOW = ("just now", "moments ago", "a moment ago")
CREATED = re.compile(r"created (?:on )?(.+)")
AVAILABLE = re.compile(r"available until (?:on )?(.+)")
MERIDIEMS = ("am", "pm", "a.m.", "p.m.", "a.m", "p.m")
SHAPE_WORDS = frozenset(("created", "available", "until", "at", "on", "in", "ago", "yesterday", "today", "just", "now", "moment", "moments", "a", "an", "one",
                         "second", "seconds", "minute", "minutes", "hour", "hours", "day", "days", "week", "weeks", "month", "months", "year", "years",
                         "am", "pm", "p", "m", "completed", "expired", "progress", *MONTH_NUMBERS, *WEEKDAY_NUMBERS))
SHAPE_PIECE = re.compile(r"(\d+)|([^\W\d_]+)|([ ,.:/-]+)|(.)", re.DOTALL)
SHAPE_LIMIT = 60
LONGEST_DIGITS = 4
MAX_SHAPED_ROWS = 2
MAX_SHAPED_CHUNKS = 6
DATE_UNREADABLE = "an export on Takeout's summary has a date that could not be read"
FUTURE_SLACK = 3600.0
YEARS_BACK = 8
SUMMARY_FIELD = re.compile(r"(overall status|creation date|expiration date): (.+)")
PARTS_HEADING = "data backup to a download link"
PART_LABEL = re.compile(r"(?:(?P<verb>download|downloaded|redownload|re-download)(?P<again> again)? )?part (?P<number>\d{1,4}) of (?P<total>\d{1,4})(?P<after> again)?")
LABEL_PUNCTUATION = re.compile(r"[()\[\],.;:!]+")
LABEL_WORDS = frozenset(("download", "downloaded", "redownload", "re", "again", "part", "of", "see", "report"))
LABEL_SHAPE_LIMIT = 40
MAX_SHAPED_ITEMS = 2
MAX_SHAPED_LABELS = 2
INDEX = re.compile(r"\d{1,4}")
NO_NUMBER = "a download on an export's page on Takeout has no readable part number"
NO_SIZE = "a download on an export's page on Takeout has no readable size"
NO_LINK = "a download on an export's page on Takeout has no download link"
NOT_USABLE = "a download link on an export's page on Takeout is not one Darkroom can use"
TWO_ADDRESSES = "a download on an export's page on Takeout has links that lead to different addresses"
NUMBERS_DISAGREE = "a download on an export's page on Takeout shows part numbers that disagree"
DOWNLOADED_DISAGREE = "a download on an export's page on Takeout says both that it was and was not downloaded already"
TOLD_APART = "an export's page on Takeout lists parts that could not be told apart"
KNOWN_HEADINGS = ("google takeout", "summary", "status", "your exports", "your latest export", "create a new export", "manage your exports", "your google data",
                  "data backup to a download link", "google photos", "export summary", "sign in", "verify it's you", "choose an account", "confirm it's you",
                  "welcome", "verify your identity", "enter your password", "google accounts")
MAX_LANDMARKS = 8
EMBEDDED_DATA = "Takeout's embedded data"
ABOUT_ID = "export id"
ABOUT_COUNT = "part count"
ABOUT_NAMES = "file names"
ABOUT_INDEXES = "part numbers"
ABOUT_SIZES = "sizes"
ABOUT_DOWNLOADED = "downloaded flags"
ABOUT_CREATED = "creation date"
ABOUT_EXPIRY = "expiry date"
DAY_SECONDS = 86400.0
PART_SIZE_SLACK = 0.06
PART_MIN_SLACK = 1024 * 1024


class UnreadableExports(ValueError):
    pass


class Disagreement(Exception):
    def __init__(self, about: str) -> None:
        super().__init__(about)
        self.about = about

    @property
    def refusal(self) -> str:
        return f"{EMBEDDED_DATA} and the page disagree about the {self.about}"

    @property
    def unused(self) -> str:
        return f"it and the page disagree about the {self.about}"


def unused_note(where: str, reason: str) -> str:
    return f"{EMBEDDED_DATA} was not used on {where} ({reason})"


class UnreadableRowDates(ValueError):
    def __init__(self, shapes: list[str]) -> None:
        super().__init__("an export's dates could not be read")
        self.shapes = shapes


class PartUnreadable(UnreadableExports):
    pass


class ListedPart(NamedTuple):
    number: int
    bytes: int
    href: str
    link: str
    already_downloaded: bool = False
    google_downloads: int | None = None


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
    unreadable: int = 0
    unused: str | None = None
    expires_at: float | None = None


ASCII_SPACE = " \t\n\r\f"
LINK_TRICKS = re.compile(r"[\x00-\x20\x7f\\]")
SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
TAKEOUT_ROOT = "https://" + selectors.TAKEOUT_HOST + "/"
SINGLE_DOT = re.compile(r"\.|%2[eE]")
DOUBLE_DOT = re.compile(r"(?:\.|%2[eE]){2}")
ENCODED_TRICKS = re.compile(r"%(?:2[fF]|5[cC]|25)")
INVISIBLE = re.compile("[\u200b-\u200f\u2060\ufeff]")


def without_dot_segments(path: str) -> str:
    segments = path.split("/")[1:]
    kept: list[str] = []
    for index, segment in enumerate(segments):
        double, single = DOUBLE_DOT.fullmatch(segment), SINGLE_DOT.fullmatch(segment)
        if double:
            kept = kept[:-1]
        elif not single:
            kept.append(segment)
        if (double or single) and index == len(segments) - 1:
            kept.append("")
    return "/" + "/".join(kept)


def path_and_tail(rest: str) -> tuple[str, str]:
    cut = min((rest.find(mark) for mark in "?#" if mark in rest), default=len(rest))
    return rest[:cut], rest[cut:]


def joined(link: str, against: str | None) -> str | None:
    if SCHEME.match(link):
        absolute = link
    elif link.startswith("//"):
        absolute = "https:" + link
    elif against is None or link.startswith(("?", "#")):
        return None
    elif link.startswith("/"):
        absolute = TAKEOUT_ROOT[:-1] + link
    else:
        folder, _ = path_and_tail(against[len(TAKEOUT_ROOT) - 1:])
        absolute = TAKEOUT_ROOT[:-1] + folder[:folder.rfind("/") + 1] + link
    if not absolute.startswith(TAKEOUT_ROOT):
        return None
    path, tail = path_and_tail(absolute[len(TAKEOUT_ROOT) - 1:])
    if ENCODED_TRICKS.search(path):
        return None
    return TAKEOUT_ROOT[:-1] + without_dot_segments(path) + tail


def resolved(href: str, address: str, base: str | None) -> str | None:
    link = href.strip(ASCII_SPACE)
    if not link or LINK_TRICKS.search(link) or LINK_TRICKS.search(address):
        return None
    against = address if address.startswith(TAKEOUT_ROOT) else None
    if base is not None and (base_link := base.strip(ASCII_SPACE)):
        if LINK_TRICKS.search(base_link):
            return None
        against = joined(base_link, against)
        if against is None:
            return None
    return joined(link, against)


def is_relative(href: str) -> bool:
    link = href.strip(ASCII_SPACE)
    return bool(link) and not link.startswith("#") and SCHEME.match(link) is None


def on_takeout(address: str) -> str:
    return "page address on Takeout" if address.startswith(TAKEOUT_ROOT) and not LINK_TRICKS.search(address) else "page address not on Takeout"


def plain(text: str) -> str:
    return SPACES.sub(" ", INVISIBLE.sub("", text).replace(" ", " ").replace("\xa0", " ").replace("’", "'")).strip().lower()


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
        self.base: str | None = None
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self.skip += 1
        elif not self.skip:
            attributes = {name: value or "" for name, value in attrs}
            if tag == "base" and self.base is None and "href" in attributes:
                self.base = attributes["href"]
            if tag == "a" and attributes.get("href"):
                self.links.append(attributes["href"])
            self.opened(tag, attributes)

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


@dataclass
class AnchorText:
    href: str
    chunks: list[str]


class SummaryReader(SkippingReader):
    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[AnchorText] = []
        self.rows: list[SummaryRow] = []
        self.current: AnchorText | None = None
        self.chunks: list[str] = []

    def opened(self, tag: str, attributes: dict[str, str]) -> None:
        if tag == "a" and self.current is None and attributes.get("href"):
            self.current = AnchorText(attributes["href"], [])

    def closed(self, tag: str) -> None:
        if tag == "a" and self.current is not None:
            self.anchors.append(self.current)
            self.current = None

    def text(self, data: str) -> None:
        self.chunks.append(plain(data))
        if self.current is not None:
            self.current.chunks.append(plain(data))


def archive_id(href: str, address: str, base: str | None) -> str | None:
    target = resolved(href, address, base)
    found = None if target is None else ARCHIVE_HREF.fullmatch(target)
    return None if found is None else found.group(1)


def read_summary(page: str, address: str) -> SummaryReader:
    reader = SummaryReader()
    reader.feed(page)
    reader.close()
    for anchor in reader.anchors:
        google_id = archive_id(anchor.href, address, reader.base)
        if google_id is not None:
            reader.rows.append(SummaryRow(google_id, anchor.chunks))
    return reader


def is_summary(reader: SummaryReader) -> bool:
    return any(chunk.startswith(SUMMARY_PHRASE) for chunk in reader.chunks)


def says_empty(reader: SummaryReader) -> bool:
    return any(phrase in chunk for chunk in reader.chunks for phrase in EMPTY_SUMMARY_PHRASES)


def shape(text: str, words: frozenset[str] = SHAPE_WORDS, limit: int = SHAPE_LIMIT) -> str:
    pieces: list[str] = []
    for digits, word, kept, _other in SHAPE_PIECE.findall(text):
        if digits:
            piece = "9" * len(digits) if len(digits) <= LONGEST_DIGITS else "9+"
        elif word:
            piece = word.lower() if word.lower() in words else "?"
        else:
            piece = kept or "?"
        if not (piece == "?" and pieces and pieces[-1] == "?"):
            pieces.append(piece)
    said = "".join(pieces)
    return said if len(said) <= limit else said[:limit - 3] + "..."


def quoted(texts: list[str]) -> str:
    return ", ".join(f"'{shape(text)}'" for text in texts)


def clock_of(found: re.Match[str]) -> time:
    hour_text, minute_text, meridiem = found["hour"], found["minute"], found["meridiem"]
    if hour_text is None or minute_text is None:
        return time()
    hour, minute = int(hour_text), int(minute_text)
    if meridiem is None:
        if len(hour_text) != 2:
            raise ValueError(hour_text)
        return time(hour, minute)
    if not 1 <= hour <= 12:
        raise ValueError(hour_text)
    return time(hour % 12 + (12 if meridiem == "p" else 0), minute)


def named(table: dict[str, int], name: str) -> int:
    number = table.get(name.removesuffix("."))
    if number is None:
        raise ValueError(name)
    return number


def at_day(day: date, found: re.Match[str], zone: tzinfo) -> float:
    return datetime.combine(day, clock_of(found), zone).timestamp()


def on_calendar(found: re.Match[str], year: int, zone: tzinfo) -> datetime:
    month = named(MONTH_NUMBERS, found["month"] or found["month_after"])
    return datetime.combine(date(year, month, int(found["day"] or found["day_first"])), clock_of(found), zone)


def on_its_weekday(found: re.Match[str], moment: datetime) -> float:
    if found["weekday"] is not None and named(WEEKDAY_NUMBERS, found["weekday"]) != moment.weekday():
        raise ValueError(found["weekday"])
    return moment.timestamp()


def nearest_past(found: re.Match[str], now: float, zone: tzinfo) -> datetime:
    this_year = datetime.fromtimestamp(now, zone).year
    for year in range(this_year, this_year - YEARS_BACK, -1):
        try:
            moment = on_calendar(found, year, zone)
        except ValueError:
            continue
        if moment.timestamp() <= now + FUTURE_SLACK:
            return moment
    raise ValueError(found.string)


def latest_weekday(found: re.Match[str], now: float, zone: tzinfo) -> float:
    weekday, today = named(WEEKDAY_NUMBERS, found["weekday"]), datetime.fromtimestamp(now, zone).date()
    for back in range(8):
        day = today - timedelta(days=back)
        if day.weekday() == weekday and (moment := at_day(day, found, zone)) <= now + FUTURE_SLACK:
            return moment
    raise ValueError(found.string)


def created_when(when: str, now: float, zone: tzinfo) -> float:
    if when in JUST_NOW:
        return now
    if (relative := RELATIVE.fullmatch(when)) is not None:
        count = int(relative["count"]) if relative["count"].isdigit() else 1
        return now - count * UNIT_SECONDS[relative["unit"]]
    if (word := DAY_WORD.fullmatch(when)) is not None:
        today = datetime.fromtimestamp(now, zone).date()
        return at_day(today - timedelta(days=word["word"] == "yesterday"), word, zone)
    if (dated := CALENDAR.fullmatch(when)) is not None:
        return on_its_weekday(dated, nearest_past(dated, now, zone) if dated["year"] is None else on_calendar(dated, int(dated["year"]), zone))
    if (weekday := WEEKDAY_ONLY.fullmatch(when)) is not None:
        return latest_weekday(weekday, now, zone)
    raise ValueError(when)


def created_at(chunk: str, now: float, zone: tzinfo) -> float:
    found = CREATED.fullmatch(chunk)
    if found is None:
        raise ValueError(chunk)
    moment = created_when(found.group(1), now, zone)
    if moment > now + FUTURE_SLACK:
        raise ValueError(chunk)
    return moment


def dated(text: str, zone: tzinfo) -> float:
    found = CALENDAR.fullmatch(text)
    if found is None or found["year"] is None:
        raise ValueError(text)
    return on_its_weekday(found, on_calendar(found, int(found["year"]), zone))


def available_until(chunk: str, zone: tzinfo) -> float:
    found = AVAILABLE.fullmatch(chunk)
    if found is None:
        raise ValueError(chunk)
    return dated(found.group(1), zone)


def row_dates(row: SummaryRow, created: str, available: str | None, now: float, zone: tzinfo) -> tuple[float, float | None]:
    unreadable: list[str] = []
    try:
        created_moment = created_at(created, now, zone)
    except ValueError:
        unreadable.append(created)
    try:
        expires = None if available is None else available_until(available, zone)
    except ValueError:
        unreadable.append(available or "")
    unreadable += [chunk for chunk in row.chunks if chunk in MERIDIEMS]
    if unreadable:
        raise UnreadableRowDates(unreadable)
    return created_moment, expires


class RowRead(NamedTuple):
    google_id: str
    state: ExportState
    dates: tuple[float, float | None] | None
    shapes: list[str]


class ReadExports(NamedTuple):
    listings: list[ExportListing]
    unused: str | None


def row_read(row: SummaryRow, now: float, zone: tzinfo) -> RowRead:
    states = [STATE_WORDS[chunk] for chunk in row.chunks if chunk in STATE_WORDS]
    if len(states) != 1:
        raise UnreadableExports("an export on Takeout's summary is in a state that was not recognised")
    created = [chunk for chunk in row.chunks if chunk.startswith("created ")]
    available = [chunk for chunk in row.chunks if chunk.startswith("available until ")]
    if len(created) != 1 or len(available) > 1:
        raise UnreadableExports(f"an export on Takeout's summary has no readable creation date: {quoted(row.chunks[:MAX_SHAPED_CHUNKS])}")
    try:
        return RowRead(row.google_id, states[0], row_dates(row, created[0], available[0] if available else None, now, zone), [])
    except UnreadableRowDates as error:
        return RowRead(row.google_id, states[0], None, error.shapes)


def told_apart(listings: list[ExportListing]) -> list[ExportListing]:
    if len(listings) > MAX_EXPORTS or len({listing.google_id for listing in listings}) != len(listings):
        raise UnreadableExports("Takeout's summary lists exports that could not be told apart")
    return listings


def page_listings(reads: list[RowRead]) -> list[ExportListing]:
    unreadable = [read.shapes for read in reads if read.dates is None]
    if unreadable:
        shown = "; ".join(quoted(chunks[:MAX_SHAPED_CHUNKS]) for chunks in unreadable[:MAX_SHAPED_ROWS])
        raise UnreadableExports(f"{DATE_UNREADABLE} ({len(unreadable)} of {len(reads)} exports): {shown}")
    return told_apart([ExportListing(read.google_id, read.dates[0], read.state, read.dates[1], (), archive_url(read.google_id)) for read in reads if read.dates is not None])


def parts_fit_state(state: ExportState, export: takeout_data.EmbeddedExport) -> bool:
    return not ((state == "ready" and export.parts is None) or (state == "expired" and export.parts is not None))


def apart(first: float | None, second: float | None) -> bool:
    return first is not None and second is not None and abs(first - second) > DAY_SECONDS


def embedded_listing(read: RowRead, found: dict[str, takeout_data.EmbeddedExport]) -> ExportListing:
    export = found.get(read.google_id)
    if export is None:
        raise Disagreement(ABOUT_ID)
    if not parts_fit_state(read.state, export):
        raise Disagreement(ABOUT_COUNT)
    created, expires = read.dates if read.dates is not None else (None, None)
    if apart(created, export.created_at):
        raise Disagreement(ABOUT_CREATED)
    if apart(expires, export.expires_at) or (expires is not None and export.expires_at is None):
        raise Disagreement(ABOUT_EXPIRY)
    return ExportListing(read.google_id, export.created_at, read.state, export.expires_at, (), archive_url(read.google_id))


def read_exports(page: str, address: str, now: float, zone: tzinfo) -> ReadExports:
    reader = read_summary(page, address)
    if not is_summary(reader):
        raise UnreadableExports("the page is not Takeout's summary of exports")
    reads = [row_read(row, now, zone) for row in reader.rows]
    if not reads:
        return ReadExports([], None)
    embedded = takeout_data.read_embedded(page, now)
    if embedded.unused is not None:
        return ReadExports(page_listings(reads), embedded.unused)
    try:
        listings = [embedded_listing(read, embedded.exports) for read in reads]
    except Disagreement as error:
        if any(read.dates is None for read in reads):
            raise UnreadableExports(error.refusal) from error
        return ReadExports(page_listings(reads), error.unused)
    return ReadExports(told_apart(listings), None)


def parse_exports(page: str, address: str, now: float, zone: tzinfo) -> list[ExportListing]:
    return read_exports(page, address, now, zone).listings


def exports_page_ready(page: str, address: str) -> bool:
    reader = read_summary(page, address)
    return is_summary(reader) and (bool(reader.rows) or says_empty(reader))


def base_finding(base: str | None) -> str:
    return "no base address" if base is None else "a base address"


def summary_findings(page: str, address: str) -> str:
    reader = read_summary(page, address)
    summary = "summary text found" if is_summary(reader) else "summary text not found"
    relative = sum(map(is_relative, reader.links))
    naming = sum("archive/" in link for link in reader.links)
    links = f"export links {len(reader.rows)} of {len(reader.links)} links ({relative} relative, {naming} naming an export's page)"
    empty = "an empty-list sentence" if says_empty(reader) else "no empty-list sentence"
    return "; ".join((summary, links, empty, base_finding(reader.base), on_takeout(address)))


def size_in_bytes(text: str) -> int | None:
    found = SIZE.search(text)
    return None if found is None else round(float(found.group(1)) * SIZE_UNITS[found.group(2)])


DOWNLOAD_HREF = re.compile(r"https://" + re.escape(selectors.DOWNLOAD_HOST) + r"/takeout/download(?:[?#].*)?")
SINGLE_PARAMETERS = ("j", "i", "user")
PERCENT_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")
GOOGLE_DOWNLOAD_DOMAINS = ("google.com", "googleusercontent.com")


def single_parameters(href: str) -> dict[str, str] | None:
    query = href.partition("#")[0].partition("?")[2]
    found: dict[str, str] = {}
    for pair in query.split("&"):
        name, _, value = pair.partition("=")
        decoded = PERCENT_ESCAPE.sub(lambda escape: chr(int(escape.group(1), 16)), name)
        if decoded not in SINGLE_PARAMETERS:
            continue
        if name != decoded or decoded in found:
            return None
        found[decoded] = value
    return found


def export_id_of(href: str) -> str | None:
    found = single_parameters(href)
    return None if found is None else found.get("j")


def googles_download_host(url: str) -> bool:
    authority = AUTHORITY.fullmatch(url)
    if authority is None or "@" in authority.group(1):
        return False
    host, _, port = authority.group(1).lower().partition(":")
    if port not in ("", "443") or host == selectors.ACCOUNTS_HOST:
        return False
    return host in GOOGLE_DOWNLOAD_DOMAINS or host.endswith(tuple("." + domain for domain in GOOGLE_DOWNLOAD_DOMAINS))


def download_of_part(url: object, href: str) -> bool:
    if not isinstance(url, str) or LINK_TRICKS.search(url) or not googles_download_host(url):
        return False
    if host_category(url) == "takeout" and DOWNLOAD_HREF.fullmatch(url) is None:
        return False
    got, wanted = single_parameters(url), single_parameters(href)
    if got is None or wanted is None or wanted.get("j") is None or wanted.get("i") is None:
        return False
    return got.get("j") == wanted["j"] and got.get("i") == wanted["i"]


def download_link(href: str, address: str, base: str | None) -> str | None:
    target = resolved(href, address, base)
    return target if target is not None and DOWNLOAD_HREF.fullmatch(target) else None


@dataclass
class PartItem:
    links: list[tuple[str, str]]
    sizes: list[str]
    chunks: list[str]
    marks: list[dict[str, str]]


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
            if "data-download-uri" in attributes:
                self.current.marks.append(attributes)
        elif tag == "li" and self.in_parts:
            self.current, self.depth = PartItem([], [], [], []), 1

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


def part_label(label: str) -> re.Match[str] | None:
    return PART_LABEL.fullmatch(SPACES.sub(" ", LABEL_PUNCTUATION.sub(" ", label)).strip())


def numbered_link(item: PartItem) -> list[tuple[str, re.Match[str]]]:
    return [(href, found) for href, label in item.links if (found := part_label(label)) is not None]


def counts_as_part(item: PartItem) -> bool:
    return bool(item.links or item.marks)


def label_says_downloaded(found: re.Match[str]) -> bool | None:
    if found["verb"] is None:
        return True if found["after"] else None
    return bool(found["again"] or found["after"]) or found["verb"] != "download"


def mark_says_downloaded(item: PartItem) -> bool | None:
    said = {mark["data-is-downloaded"].strip().lower() for mark in item.marks if "data-is-downloaded" in mark}
    if not said:
        return None
    if not said <= {"true", "false"} or len(said) > 1:
        raise PartUnreadable(DOWNLOADED_DISAGREE)
    return said == {"true"}


class Candidate(NamedTuple):
    raw: str
    href: str
    label: re.Match[str] | None


def candidates_of(item: PartItem, google_id: str, address: str, base: str | None) -> list[Candidate]:
    found: list[Candidate] = []
    for raw, label in item.links:
        numbered = part_label(label)
        target = download_link(raw, address, base)
        if target is None:
            if numbered is not None:
                raise UnreadableExports(NOT_USABLE)
            continue
        if export_id_of(target) != google_id:
            raise UnreadableExports(NOT_USABLE)
        found.append(Candidate(raw, target, numbered))
    return found


def index_number(item: PartItem) -> int | None:
    indices = {mark["data-index"].strip() for mark in item.marks if "data-index" in mark}
    if len(indices) > 1:
        raise UnreadableExports(NUMBERS_DISAGREE)
    if not indices:
        return None
    index = indices.pop()
    if not INDEX.fullmatch(index):
        raise PartUnreadable(NO_NUMBER)
    return int(index) + 1


def link_number(href: str) -> int | None:
    found = single_parameters(href)
    index = None if found is None else found.get("i")
    return int(index) + 1 if index is not None and INDEX.fullmatch(index) else None


class ReadPart(NamedTuple):
    part: ListedPart
    total: int | None
    file_name: str | None = None
    marked: bool | None = None
    exact_size: bool = False


def file_name_of(item: PartItem) -> str | None:
    names = {mark["data-filename"].strip() for mark in item.marks if "data-filename" in mark}
    return names.pop() if len(names) == 1 else None


def part_of(item: PartItem, google_id: str, address: str, base: str | None) -> ReadPart:
    candidates = candidates_of(item, google_id, address, base)
    if not candidates:
        raise PartUnreadable(NO_LINK)
    if len({candidate.href for candidate in candidates}) > 1:
        raise UnreadableExports(TWO_ADDRESSES)
    labels = {(int(found["number"]), int(found["total"])) for candidate in candidates if (found := candidate.label) is not None}
    if len(labels) > 1:
        raise UnreadableExports(NUMBERS_DISAGREE)
    chosen = next((candidate for candidate in candidates if candidate.label is not None), candidates[0])
    from_label = next(iter(labels), None)
    from_index = index_number(item)
    number = from_label[0] if from_label is not None else from_index
    if number is None:
        raise PartUnreadable(NO_NUMBER)
    if {seen for seen in (from_index, link_number(chosen.href)) if seen is not None} - {number}:
        raise UnreadableExports(NUMBERS_DISAGREE)
    by_label = {said for candidate in candidates if candidate.label is not None and (said := label_says_downloaded(candidate.label)) is not None}
    by_mark = mark_says_downloaded(item)
    if len(by_label) > 1 or (by_mark is not None and by_label - {by_mark}):
        raise PartUnreadable(DOWNLOADED_DISAGREE)
    exact = set(item.sizes)
    value = exact.pop() if len(exact) == 1 else ""
    is_exact = value.isdigit()
    size = int(value) if is_exact else size_in_bytes(" ".join(item.chunks))
    if size is None:
        raise PartUnreadable(NO_SIZE)
    part = ListedPart(number, size, chosen.href, chosen.raw, bool(by_mark) or True in by_label)
    marked = by_mark if by_mark is not None else next(iter(by_label), None)
    return ReadPart(part, from_label[1] if from_label is not None else None, file_name_of(item), marked, is_exact)


def has_attribute(item: PartItem, name: str) -> bool:
    return any(name in mark for mark in item.marks) or (name == "data-size" and bool(item.sizes))


def item_findings(item: PartItem, address: str, base: str | None) -> str:
    usable = sum(download_link(raw, address, base) is not None for raw, _ in item.links)
    labels = [label for _, label in item.links if label][:MAX_SHAPED_LABELS]
    shown = ", ".join(f"'{shape(label, LABEL_WORDS, LABEL_SHAPE_LIMIT)}'" for label in labels) or "none"
    return f"{len(item.links)} links ({usable} usable), labels {shown}"


def part_findings(reader: ArchiveReader, unread: list[PartItem], address: str) -> str:
    items = [item for item in reader.items if counts_as_part(item)]
    labelled = sum(bool(numbered_link(item)) for item in items)
    present = ", ".join(f"{name} on {sum(has_attribute(item, name) for item in items)}" for name in ("data-index", "data-size", "data-is-downloaded"))
    downloaded = sum(any(mark.get("data-is-downloaded", "").strip().lower() == "true" for mark in item.marks) for item in items)
    shown = "; ".join(item_findings(item, address, reader.base) for item in unread[:MAX_SHAPED_ITEMS])
    return (f"{len(items)} part items, at most {max((len(item.links) for item in items), default=0)} links in one, {labelled} with a part label, {present} "
            f"({downloaded} downloaded); {len(unread)} unread: {shown}")


def archive_reads(reader: ArchiveReader, google_id: str, address: str, needed: int | None = None) -> tuple[list[ReadPart], int]:
    read: list[ReadPart] = []
    unread: list[tuple[PartItem, PartUnreadable]] = []
    for item in reader.items:
        if not counts_as_part(item):
            continue
        try:
            read.append(part_of(item, google_id, address, reader.base))
        except PartUnreadable as error:
            unread.append((item, error))
    read.sort(key=lambda each: each.part)
    count = len(read) + len(unread)
    numbers = [each.part.number for each in read]
    totals = {each.total for each in read if each.total is not None}
    if count > MAX_PARTS or not totals <= {count} or len(set(numbers)) != len(numbers) or not all(1 <= number <= count for number in numbers):
        raise UnreadableExports(TOLD_APART)
    if unread and (needed is None or needed not in numbers):
        raise UnreadableExports(f"{unread[0][1]}: {part_findings(reader, [item for item, _ in unread], address)}")
    return read, len(unread)


def archive_parts(reader: ArchiveReader, google_id: str, address: str, needed: int | None = None) -> tuple[tuple[ListedPart, ...], int]:
    reads, unread = archive_reads(reader, google_id, address, needed)
    return tuple(each.part for each in reads), unread


def calendar_date(text: str) -> date:
    return datetime.fromtimestamp(dated(text, UTC), UTC).date()


def field_date(fields: dict[str, str], name: str, required: bool, unreadable: list[str]) -> date | None:
    text = fields.get(name)
    if text is None:
        if required:
            unreadable.append(f"no {name}")
        return None
    try:
        return calendar_date(text)
    except ValueError:
        unreadable.append(f"{name} '{shape(text)}'")
        return None


def sizes_agree(read: ReadPart, embedded: takeout_data.EmbeddedPart) -> bool:
    if read.exact_size:
        return read.part.bytes == embedded.bytes
    return abs(read.part.bytes - embedded.bytes) <= max(embedded.bytes * PART_SIZE_SLACK, PART_MIN_SLACK)


def matched_parts(reads: list[ReadPart], unread: int, parts: tuple[takeout_data.EmbeddedPart, ...]) -> tuple[ListedPart, ...]:
    if len(reads) + unread != len(parts):
        raise Disagreement(ABOUT_COUNT)
    by_name = {part.file_name: part for part in parts}
    matched: list[tuple[ReadPart, takeout_data.EmbeddedPart]] = []
    for read in reads:
        found = parts[read.part.number - 1] if read.file_name is None else by_name.get(read.file_name)
        if found is None:
            raise Disagreement(ABOUT_NAMES)
        matched.append((read, found))
    checks = ((ABOUT_INDEXES, lambda read, found: found.position + 1 == read.part.number), (ABOUT_SIZES, sizes_agree),
              (ABOUT_DOWNLOADED, lambda read, found: read.marked is None or read.marked == (found.google_downloads > 0)))
    for about, agrees in checks:
        if not all(agrees(read, found) for read, found in matched):
            raise Disagreement(about)
    return tuple(read.part._replace(bytes=found.bytes, already_downloaded=read.part.already_downloaded or found.google_downloads > 0, google_downloads=found.google_downloads)
                 for read, found in matched)


def local_date(moment: float, zone: tzinfo) -> date:
    return datetime.fromtimestamp(moment, zone).date()


def days_apart(first: date | None, second: date | None) -> bool:
    return first is not None and second is not None and abs((first - second).days) > 1


def embedded_archive(state: ExportState, created_on: date | None, expires_on: date | None, reads: list[ReadPart], unread: int,
                     export: takeout_data.EmbeddedExport | None, zone: tzinfo) -> ArchiveDetail:
    if export is None:
        raise Disagreement(ABOUT_ID)
    if not parts_fit_state(state, export):
        raise Disagreement(ABOUT_COUNT)
    parts = matched_parts(reads, unread, export.parts) if state == "ready" and export.parts is not None else ()
    embedded_created = local_date(export.created_at, zone)
    embedded_expiry = None if export.expires_at is None else local_date(export.expires_at, zone)
    if days_apart(created_on, embedded_created):
        raise Disagreement(ABOUT_CREATED)
    if days_apart(expires_on, embedded_expiry):
        raise Disagreement(ABOUT_EXPIRY)
    return ArchiveDetail(state, embedded_created, embedded_expiry or expires_on, parts, unread, expires_at=export.expires_at)


def parse_archive(page: str, address: str, google_id: str, needed: int | None = None, now: float | None = None, zone: tzinfo = UTC) -> ArchiveDetail:
    reader = read_archive(page)
    fields = summary_fields(reader.chunks)
    state = STATE_WORDS.get(fields.get("overall status", ""))
    if state is None:
        raise UnreadableExports("an export's page on Takeout is in a state that was not recognised")
    unreadable: list[str] = []
    created_on = field_date(fields, "creation date", True, unreadable)
    expires_on = field_date(fields, "expiration date", False, unreadable)
    embedded = takeout_data.read_embedded(page, now)
    if (unreadable or created_on is None) and embedded.unused is not None:
        raise UnreadableExports("an export's page on Takeout has a date that could not be read: " + ", ".join(unreadable))
    reads, unread = archive_reads(reader, google_id, address, needed) if state == "ready" else ([], 0)
    if state == "ready" and not reads:
        raise UnreadableExports("a completed export's page on Takeout lists no parts")
    page_detail = ArchiveDetail(state, created_on or date.min, expires_on, tuple(read.part for read in reads), unread)
    if embedded.unused is not None:
        return replace(page_detail, unused=embedded.unused)
    try:
        return embedded_archive(state, created_on, expires_on, reads, unread, embedded.exports.get(google_id), zone)
    except Disagreement as error:
        if unreadable or created_on is None or unread:
            raise UnreadableExports(error.refusal) from error
        return replace(page_detail, unused=error.unused)


def overall_status(reader: ArchiveReader) -> str | None:
    return next((found.group(2) for chunk in reader.chunks if (found := SUMMARY_FIELD.fullmatch(chunk)) and found.group(1) == "overall status"), None)


def archive_page_ready(page: str) -> bool:
    reader = read_archive(page)
    status = overall_status(reader)
    if status is None:
        return False
    return STATE_WORDS.get(status) != "ready" or any(numbered_link(item) or has_attribute(item, "data-index") for item in reader.items)


def archive_findings(page: str, address: str) -> str:
    reader = read_archive(page)
    status = "overall status not found" if overall_status(reader) is None else "overall status found"
    numbered = [href for item in reader.items for href, _ in numbered_link(item)]
    usable = sum(download_link(href, address, reader.base) is not None for href in numbered)
    links = f"numbered download links {len(numbered)} ({usable} usable, {sum(map(is_relative, numbered))} relative)"
    return "; ".join((status, links, base_finding(reader.base), on_takeout(address)))


def with_archive(listing: ExportListing, detail: ArchiveDetail, zone: tzinfo) -> ExportListing:
    if detail.state != listing.state:
        raise UnreadableExports("an export's page on Takeout and the summary disagree about its state")
    listed_on = datetime.fromtimestamp(listing.created_at, zone).date()
    if abs((listed_on - detail.created_on).days) > 1:
        raise UnreadableExports("an export's page on Takeout and the summary disagree about when it was created")
    expires = listing.expires_at if listing.expires_at is not None else detail.expires_at
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


def read_landmarks(page: str) -> LandmarkReader:
    reader = LandmarkReader()
    reader.feed(page)
    reader.close()
    return reader


def landmarks(page: str) -> str:
    reader = read_landmarks(page)
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


HostCategory = Literal["takeout", "accounts", "other", "none"]
REAUTH_PHRASES = ("enter your password", "confirm your password", "re-enter your password", "type your password", "to continue, first verify it's you")
GREETINGS = ("welcome", "hi ")
IDENTITY_ATTRIBUTES = ("data-email", "data-identifier", "data-profile-identifier")
IDENTITY_IDS = ("profileidentifier", "identifier-chip")
AUTHORITY = re.compile(r"https://([^/?#]*)(?:[/?#].*)?", re.IGNORECASE | re.DOTALL)


def host_category(address: str) -> HostCategory:
    link = address.strip(ASCII_SPACE)
    if not link or link.lower().startswith("about:"):
        return "none"
    found = AUTHORITY.fullmatch(link)
    if found is None or LINK_TRICKS.search(link):
        return "other"
    host, _, port = found.group(1).lower().partition(":")
    if "@" in found.group(1) or (port and not port.isdigit()):
        return "other"
    if host == selectors.TAKEOUT_HOST:
        return "takeout"
    return "accounts" if host == selectors.ACCOUNTS_HOST else "other"


class OwnerFieldReader(SkippingReader):
    def __init__(self) -> None:
        super().__init__()
        self.secret_field = False
        self.identity = False

    def opened(self, tag: str, attributes: dict[str, str]) -> None:
        lowered = {name.lower(): value.lower() for name, value in attributes.items()}
        if tag == "input" and (lowered.get("type", "").strip() == "password" or lowered.get("name", "").strip() == "passwd"):
            self.secret_field = True
        if any(name in lowered for name in IDENTITY_ATTRIBUTES) or lowered.get("id", "") in IDENTITY_IDS:
            self.identity = True


def owner_fields(page: str) -> OwnerFieldReader:
    reader = OwnerFieldReader()
    reader.feed(page)
    reader.close()
    return reader


def greets_someone(page: str) -> bool:
    reader = read_landmarks(page)
    return any(heading == "welcome" or heading.startswith(GREETINGS) for heading in reader.headings)


def classify_at(page: str, address: str) -> str:
    state = classify(page)
    if state in (SIGN_IN_NEEDED, VERIFY_ITS_YOU):
        return state
    host = host_category(address)
    fields = owner_fields(page)
    if host == "accounts" or fields.secret_field:
        return VERIFY_ITS_YOU
    if state == SIGNED_IN:
        return state
    if any(phrase in visible_text(page) for phrase in REAUTH_PHRASES):
        return VERIFY_ITS_YOU
    if host != "takeout" and (fields.identity or greets_someone(page)):
        return VERIFY_ITS_YOU
    return state


def click_findings(page: str, address: str, tabs: int, own_tab: bool) -> str:
    field = "a password field" if owner_fields(page).secret_field else "no password field"
    which = "the job's own tab" if own_tab else "another tab"
    return f"{which}: page host {host_category(address)}; {field}; {landmarks(page)}; {tabs} tab{'' if tabs == 1 else 's'} open"
