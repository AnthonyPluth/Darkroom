from __future__ import annotations

from datetime import UTC, datetime

from darkroom_browser import selectors
from darkroom_browser.context import Browser, JobContext
from darkroom_browser.jobs.session import UnexpectedPage, ensure_signed_in
from darkroom_browser.pages import takeout

CLICK_SEQUENCE: tuple[tuple[str, str], ...] = (
    ("deselect everything", selectors.EXPORT_SELECT_NONE),
    ("select Google Photos only", selectors.EXPORT_PHOTOS_CHECKBOX),
    ("go to the delivery step", selectors.EXPORT_NEXT_STEP),
    ("open the delivery menu", selectors.EXPORT_DELIVERY_MENU),
    ("send the download link by email", selectors.EXPORT_DELIVERY_EMAIL),
    ("export every 2 months for 1 year", selectors.EXPORT_FREQUENCY_RECURRING),
    ("open the file type menu", selectors.EXPORT_TYPE_MENU),
    ("choose .zip", selectors.EXPORT_TYPE_ZIP),
    ("open the file size menu", selectors.EXPORT_SIZE_MENU),
    ("choose 50 GB", selectors.EXPORT_SIZE_50GB),
    ("create the export", selectors.EXPORT_CREATE),
)


class ScheduleNotConfirmed(UnexpectedPage):
    pass


def one_year_after(created: float) -> float:
    moment = datetime.fromtimestamp(created, UTC)
    try:
        later = moment.replace(year=moment.year + 1)
    except ValueError:
        later = moment.replace(year=moment.year + 1, day=28)
    return later.timestamp()


def drive(ctx: JobContext, browser: Browser) -> None:
    for step, selector in CLICK_SEQUENCE:
        ctx.progress(step)
        browser.wait_for(selector)
        browser.click(selector)


def create_schedule(ctx: JobContext) -> dict[str, object]:
    browser = ctx.browser()
    ensure_signed_in(ctx, browser)
    drive(ctx, browser)
    if takeout.parse_schedule_confirmation(browser.content()) is None:
        raise ScheduleNotConfirmed("Takeout did not confirm a recurring export")
    created = ctx.wall()
    return {"schedule_created": created, "schedule_end": one_year_after(created)}
