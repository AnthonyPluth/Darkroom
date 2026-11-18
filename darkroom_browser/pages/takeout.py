from __future__ import annotations

import html
import re
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta, tzinfo
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
PART_LABEL = re.compile(r"download part (\d{1,4}) of (\d{1,4})")
KNOWN_HEADINGS = ("google takeout", "summary", "status", "your exports", "your latest export", "create a new export", "manage your exports", "your google data",
                  "data backup to a download link", "google photos", "export summary", "sign in", "verify it's you", "choose an account", "confirm it's you",
                  "welcome", "verify your identity", "enter your password", "google accounts")
MAX_LANDMARKS = 8


class UnreadableExports(ValueError):
    pass


class UnreadableRowDates(ValueError):
    def __init__(self, shapes: list[str]) -> None:
        super().__init__("an export's dates could not be read")
        self.shapes = shapes


class ListedPart(NamedTuple):
    number: int
    bytes: int
    href: str
    link: str


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


def shape(text: str) -> str:
    pieces: list[str] = []
    for digits, word, kept, _other in SHAPE_PIECE.findall(text):
        if digits:
            piece = "9" * len(digits) if len(digits) <= LONGEST_DIGITS else "9+"
        elif word:
            piece = word.lower() if word.lower() in SHAPE_WORDS else "?"
        else:
            piece = kept or "?"
        if not (piece == "?" and pieces and pieces[-1] == "?"):
            pieces.append(piece)
    said = "".join(pieces)
    return said if len(said) <= SHAPE_LIMIT else said[:SHAPE_LIMIT - 3] + "..."


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


def listing_of(row: SummaryRow, now: float, zone: tzinfo) -> ExportListing:
    states = [STATE_WORDS[chunk] for chunk in row.chunks if chunk in STATE_WORDS]
    if len(states) != 1:
        raise UnreadableExports("an export on Takeout's summary is in a state that was not recognised")
    created = [chunk for chunk in row.chunks if chunk.startswith("created ")]
    available = [chunk for chunk in row.chunks if chunk.startswith("available until ")]
    if len(created) != 1 or len(available) > 1:
        raise UnreadableExports(f"an export on Takeout's summary has no readable creation date: {quoted(row.chunks[:MAX_SHAPED_CHUNKS])}")
    created_moment, expires = row_dates(row, created[0], available[0] if available else None, now, zone)
    return ExportListing(row.google_id, created_moment, states[0], expires, (), archive_url(row.google_id))


def parse_exports(page: str, address: str, now: float, zone: tzinfo) -> list[ExportListing]:
    reader = read_summary(page, address)
    if not is_summary(reader):
        raise UnreadableExports("the page is not Takeout's summary of exports")
    listings: list[ExportListing] = []
    unreadable: list[list[str]] = []
    for row in reader.rows:
        try:
            listings.append(listing_of(row, now, zone))
        except UnreadableRowDates as error:
            unreadable.append(error.shapes)
    if unreadable:
        shown = "; ".join(quoted(chunks[:MAX_SHAPED_CHUNKS]) for chunks in unreadable[:MAX_SHAPED_ROWS])
        raise UnreadableExports(f"{DATE_UNREADABLE} ({len(unreadable)} of {len(reader.rows)} exports): {shown}")
    if len(listings) > MAX_EXPORTS or len({listing.google_id for listing in listings}) != len(listings):
        raise UnreadableExports("Takeout's summary lists exports that could not be told apart")
    return listings


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
PART_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,190}-([0-9]{3,4})\.zip")


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


def numbered_as_part(name: object, number: int) -> bool:
    if not isinstance(name, str) or "/" in name or "\\" in name:
        return False
    found = PART_NAME.fullmatch(name)
    return found is not None and int(found.group(1)) == number


def download_of_part(url: object, name: object, href: str, number: int) -> bool:
    if not isinstance(url, str) or LINK_TRICKS.search(url):
        return False
    authority = AUTHORITY.fullmatch(url)
    if authority is None or "@" in authority.group(1):
        return False
    host = host_category(url)
    if host == "accounts" or (host == "takeout" and DOWNLOAD_HREF.fullmatch(url) is None):
        return False
    got, wanted = single_parameters(url), single_parameters(href)
    if got is None or wanted is None or wanted.get("j") is None or wanted.get("i") is None:
        return False
    if "j" in got or "i" in got:
        return got.get("j") == wanted["j"] and got.get("i") == wanted["i"]
    return numbered_as_part(name, number)


def download_link(href: str, address: str, base: str | None) -> str | None:
    target = resolved(href, address, base)
    return target if target is not None and DOWNLOAD_HREF.fullmatch(target) else None


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


def part_of(item: PartItem, google_id: str, address: str, base: str | None) -> tuple[ListedPart, int]:
    numbered = numbered_link(item)
    if len(numbered) != 1:
        raise UnreadableExports("a download on an export's page on Takeout has no readable part number")
    raw, label = numbered[0]
    href = download_link(raw, address, base)
    if href is None or export_id_of(href) != google_id:
        raise UnreadableExports("a download link on an export's page on Takeout is not one Darkroom can use")
    exact = set(item.sizes)
    if len(exact) == 1 and (value := exact.pop()).isdigit():
        size: int | None = int(value)
    else:
        size = size_in_bytes(" ".join(item.chunks))
    if size is None:
        raise UnreadableExports("a download on an export's page on Takeout has no readable size")
    return ListedPart(int(label.group(1)), size, href, raw), int(label.group(2))


def archive_parts(reader: ArchiveReader, google_id: str, address: str) -> tuple[ListedPart, ...]:
    read = [part_of(item, google_id, address, reader.base) for item in reader.items if item.links]
    parts = sorted(part for part, _ in read)
    totals = {total for _, total in read}
    if len(parts) > MAX_PARTS or totals != {len(parts)} or [part.number for part in parts] != list(range(1, len(parts) + 1)):
        raise UnreadableExports("an export's page on Takeout lists parts that could not be told apart")
    return tuple(parts)


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


def parse_archive(page: str, address: str, google_id: str) -> ArchiveDetail:
    reader = read_archive(page)
    fields = summary_fields(reader.chunks)
    state = STATE_WORDS.get(fields.get("overall status", ""))
    if state is None:
        raise UnreadableExports("an export's page on Takeout is in a state that was not recognised")
    unreadable: list[str] = []
    created_on = field_date(fields, "creation date", True, unreadable)
    expires_on = field_date(fields, "expiration date", False, unreadable)
    if unreadable or created_on is None:
        raise UnreadableExports("an export's page on Takeout has a date that could not be read: " + ", ".join(unreadable))
    parts = archive_parts(reader, google_id, address) if state == "ready" else ()
    if state == "ready" and not parts:
        raise UnreadableExports("a completed export's page on Takeout lists no parts")
    return ArchiveDetail(state, created_on, expires_on, parts)


def overall_status(reader: ArchiveReader) -> str | None:
    return next((found.group(2) for chunk in reader.chunks if (found := SUMMARY_FIELD.fullmatch(chunk)) and found.group(1) == "overall status"), None)


def archive_page_ready(page: str) -> bool:
    reader = read_archive(page)
    status = overall_status(reader)
    if status is None:
        return False
    return STATE_WORDS.get(status) != "ready" or any(numbered_link(item) for item in reader.items)


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
