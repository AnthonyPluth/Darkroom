from __future__ import annotations

from collections.abc import Callable
from datetime import tzinfo
from typing import NamedTuple

from darkroom_browser import selectors
from darkroom_browser.config import local_zone
from darkroom_browser.context import Browser, JobContext
from darkroom_browser.jobs.session import SIGN_IN_STATES, UnexpectedPage, ensure_signed_in
from darkroom_browser.pages import takeout

LIST_POLLS = 6
LIST_POLL_SECONDS = 5.0
MAX_ROUNDS = 2
MAX_MESSAGE_CHARS = 480


class ExportsUnreadable(UnexpectedPage):
    pass


class SignedOut(Exception):
    def __init__(self, state: str) -> None:
        super().__init__("Takeout asked for a sign-in")
        self.state = state


class Loaded(NamedTuple):
    page: str
    address: str


def open_page(ctx: JobContext, browser: Browser, url: str, ready: Callable[[str, str], bool], findings: Callable[[str, str], str], failure: str,
              signed_in: Callable[[], None] | None = None, wait: bool = True) -> Loaded:
    page, address = "", url
    for _ in range(MAX_ROUNDS):
        browser.goto(url)
        signed_out = False
        for attempt in range(LIST_POLLS + 1):
            page, address = browser.content(), browser.current_url()
            if ready(page, address):
                return Loaded(page, address)
            state = takeout.classify(page)
            if state in SIGN_IN_STATES:
                if not wait:
                    raise SignedOut(state)
                signed_out = True
                break
            if attempt < LIST_POLLS:
                ctx.sleep(LIST_POLL_SECONDS)
                ctx.check()
        if not signed_out:
            break
        if ensure_signed_in(ctx, browser) and signed_in is not None:
            signed_in()
    raise ExportsUnreadable(f"{failure}; {findings(page, address)}; the page showed {takeout.landmarks(page)}")


def open_exports(ctx: JobContext, browser: Browser) -> Loaded:
    return open_page(ctx, browser, selectors.TAKEOUT_MANAGE_URL, takeout.exports_page_ready, takeout.summary_findings, "Takeout's list of exports did not load")


def archive_ready(page: str, _address: str) -> bool:
    return takeout.archive_page_ready(page)


def open_archive(ctx: JobContext, browser: Browser, url: str, signed_in: Callable[[], None] | None = None, wait: bool = True) -> Loaded:
    return open_page(ctx, browser, url, archive_ready, takeout.archive_findings, "an export's own page on Takeout did not load", signed_in, wait)


def with_parts(ctx: JobContext, browser: Browser, listing: takeout.ExportListing, zone: tzinfo, unused: list[str]) -> takeout.ExportListing:
    if listing.state != "ready":
        return listing
    detail = takeout.parse_archive(*open_archive(ctx, browser, listing.archive_url), listing.google_id, now=ctx.wall(), zone=zone)
    if detail.unused is not None:
        unused.append(takeout.unused_note("an export's page", detail.unused))
    unused.extend(takeout.unchecked_note("an export's page", about) for about in detail.unchecked)
    return takeout.with_archive(listing, detail, zone)


def part_report(part: takeout.ListedPart) -> dict[str, object]:
    found: dict[str, object] = {"number": part.number, "bytes": part.bytes}
    if part.google_downloads is not None:
        found["google_downloads"] = part.google_downloads
    return found


def report(listings: list[takeout.ExportListing]) -> dict[str, object]:
    return {
        "exports": [
            {
                "google_id": listing.google_id,
                "created_at": listing.created_at,
                "state": listing.state,
                "expires_at": listing.expires_at,
                "parts": [part_report(part) for part in listing.parts],
            }
            for listing in listings
        ]
    }


def read_message(count: int, unused: list[str]) -> str:
    said = f"read {count} exports"
    notes = list(dict.fromkeys(unused))
    for index, note in enumerate(notes):
        left = len(notes) - index - 1
        room = MAX_MESSAGE_CHARS - (len(f"; and {left} more notes about Takeout's embedded data") if left else 0)
        if len(said) + 2 + len(note) > room:
            return f"{said}; and {left + 1} more notes about Takeout's embedded data"
        said += "; " + note
    return said


def check_exports(ctx: JobContext) -> dict[str, object]:
    browser = ctx.browser()
    ensure_signed_in(ctx, browser)
    zone = local_zone()
    page, address = open_exports(ctx, browser)
    unused: list[str] = []
    try:
        found = takeout.read_exports(page, address, ctx.wall(), zone)
        if found.unused is not None:
            unused.append(takeout.unused_note("the list of exports", found.unused))
        unused.extend(takeout.unchecked_note("the list of exports", about) for about in found.unchecked)
        listings = [with_parts(ctx, browser, listing, zone, unused) for listing in found.listings]
    except takeout.UnreadableExports as error:
        raise ExportsUnreadable(str(error)) from error
    ctx.progress(read_message(len(listings), unused))
    return report(listings)
