from __future__ import annotations

from darkroom_browser import selectors
from darkroom_browser.context import Browser, JobContext
from darkroom_browser.jobs.session import SIGN_IN_STATES, UnexpectedPage, ensure_signed_in
from darkroom_browser.pages import takeout

LIST_POLLS = 6
LIST_POLL_SECONDS = 5.0
MAX_ROUNDS = 2


class ExportsUnreadable(UnexpectedPage):
    pass


def open_exports(ctx: JobContext, browser: Browser) -> str:
    for _ in range(MAX_ROUNDS):
        browser.goto(selectors.TAKEOUT_MANAGE_URL)
        signed_out = False
        for attempt in range(LIST_POLLS + 1):
            page = browser.content()
            if takeout.exports_page_ready(page):
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
    raise ExportsUnreadable("Takeout's list of exports did not load")


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
    page = open_exports(ctx, browser)
    try:
        listings = takeout.parse_exports(page)
    except takeout.UnreadableExports as error:
        raise ExportsUnreadable(str(error)) from error
    ctx.progress(f"read {len(listings)} exports")
    return report(listings)
