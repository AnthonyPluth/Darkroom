from __future__ import annotations

from datetime import UTC, datetime

from darkroom_browser import selectors
from darkroom_browser.context import Browser, Cancelled, JobContext
from darkroom_browser.jobs.session import UnexpectedPage, ensure_signed_in
from darkroom_browser.pages import takeout

CLICK_SEQUENCE: tuple[tuple[str, str], ...] = (
    ("deselect everything", selectors.EXPORT_SELECT_NONE),
    ("select Google Photos only", selectors.EXPORT_PHOTOS_CHECKBOX),
    ("go to the delivery step", selectors.EXPORT_NEXT_STEP),
    ("open the delivery menu", selectors.EXPORT_DELIVERY_MENU),
    ("send the download link by email", selectors.EXPORT_DELIVERY_EMAIL),
    ("export every month for 1 year", selectors.EXPORT_FREQUENCY_MONTHLY),
    ("open the file type menu", selectors.EXPORT_TYPE_MENU),
    ("choose .zip", selectors.EXPORT_TYPE_ZIP),
    ("open the file size menu", selectors.EXPORT_SIZE_MENU),
    ("choose 50 GB", selectors.EXPORT_SIZE_50GB),
    ("create the export", selectors.EXPORT_CREATE),
)


CONFIRM_POLLS = 12
CONFIRM_POLL_SECONDS = 5.0


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
        if selector != selectors.EXPORT_CREATE:
            browser.click(selector)
            continue
        try:
            browser.click(selector)
        except Exception as error:
            raise ScheduleNotConfirmed(f"the click on Create export did not finish ({type(error).__name__}); check Takeout before trying again") from error


def await_confirmation(ctx: JobContext, browser: Browser) -> None:
    for attempt in range(CONFIRM_POLLS + 1):
        page = browser.content()
        if takeout.parse_schedule_confirmation(page) is not None:
            return
        if takeout.classify(page) in (takeout.SIGN_IN_NEEDED, takeout.VERIFY_ITS_YOU):
            raise ScheduleNotConfirmed("Takeout showed a sign-in page after Create export; check Takeout before trying again")
        if attempt < CONFIRM_POLLS:
            ctx.sleep(CONFIRM_POLL_SECONDS)
            ctx.check()
    raise ScheduleNotConfirmed("Takeout did not confirm a recurring export; check Takeout before trying again")


def create_schedule(ctx: JobContext) -> dict[str, object]:
    browser = ctx.browser()
    ensure_signed_in(ctx, browser)
    drive(ctx, browser)
    try:
        await_confirmation(ctx, browser)
    except (Cancelled, ScheduleNotConfirmed):
        raise
    except Exception as error:
        raise ScheduleNotConfirmed(f"the page failed after Create export ({type(error).__name__}); check Takeout before trying again") from error
    created = ctx.wall()
    return {"schedule_created": created, "schedule_end": one_year_after(created)}
