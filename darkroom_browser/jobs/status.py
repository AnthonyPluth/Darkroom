from __future__ import annotations

from collections.abc import Callable
from datetime import tzinfo

from darkroom_browser import selectors
from darkroom_browser.config import local_zone
from darkroom_browser.context import Browser, JobContext
from darkroom_browser.jobs.session import SIGN_IN_STATES, UnexpectedPage, ensure_signed_in
from darkroom_browser.pages import takeout

LIST_POLLS = 6
LIST_POLL_SECONDS = 5.0
MAX_ROUNDS = 2


class ExportsUnreadable(UnexpectedPage):
    pass


def open_page(ctx: JobContext, browser: Browser, url: str, ready: Callable[[str], bool], failure: str) -> str:
    page = ""
    for _ in range(MAX_ROUNDS):
        browser.goto(url)
        signed_out = False
        for attempt in range(LIST_POLLS + 1):
            page = browser.content()
            if ready(page):
                return page
            if takeout.classify(page) in SIGN_IN_STATES:
                signed_out = True
                break
            if attempt < LIST_POLLS:
                ctx.sleep(LIST_POLL_SECONDS)
                ctx.check()
        if not signed_out:
            break
        ensure_signed_in(ctx, browser)
    raise ExportsUnreadable(f"{failure}; the page showed {takeout.landmarks(page)}")


def open_exports(ctx: JobContext, browser: Browser) -> str:
    return open_page(ctx, browser, selectors.TAKEOUT_MANAGE_URL, takeout.exports_page_ready, "Takeout's list of exports did not load")


def open_archive(ctx: JobContext, browser: Browser, url: str) -> str:
    return open_page(ctx, browser, url, takeout.archive_page_ready, "an export's own page on Takeout did not load")


def with_parts(ctx: JobContext, browser: Browser, listing: takeout.ExportListing, zone: tzinfo) -> takeout.ExportListing:
    if listing.state != "ready":
        return listing
    detail = takeout.parse_archive(open_archive(ctx, browser, listing.archive_url), listing.google_id)
    return takeout.with_archive(listing, detail, zone)


def report(listings: list[takeout.ExportListing]) -> dict[str, object]:
    return {
        "exports": [
            {
                "google_id": listing.google_id,
                "created_at": listing.created_at,
                "state": listing.state,
                "expires_at": listing.expires_at,
                "parts": [{"number": part.number, "bytes": part.bytes} for part in listing.parts],
            }
            for listing in listings
        ]
    }


def check_exports(ctx: JobContext) -> dict[str, object]:
    browser = ctx.browser()
    ensure_signed_in(ctx, browser)
    zone = local_zone()
    page = open_exports(ctx, browser)
    try:
        listings = [with_parts(ctx, browser, listing, zone) for listing in takeout.parse_exports(page, ctx.wall(), zone)]
    except takeout.UnreadableExports as error:
        raise ExportsUnreadable(str(error)) from error
    ctx.progress(f"read {len(listings)} exports")
    return report(listings)
