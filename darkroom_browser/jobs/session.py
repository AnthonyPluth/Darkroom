from __future__ import annotations

from darkroom_browser import selectors
from darkroom_browser.context import Browser, JobContext
from darkroom_browser.pages import takeout

POLL_SECONDS = 30.0
UNKNOWN_LIMIT_SECONDS = 5 * 60.0
WAIT_LIMIT_SECONDS = 48 * 3600.0


class UnexpectedPage(Exception):
    pass


class SignInTimedOut(Exception):
    pass


def ensure_signed_in(ctx: JobContext, browser: Browser) -> None:
    browser.goto(selectors.TAKEOUT_URL)
    started = ctx.clock()
    unknown_since: float | None = None
    reported = False
    state = takeout.classify(browser.content())
    while state != takeout.SIGNED_IN:
        now = ctx.clock()
        if state == takeout.UNKNOWN:
            unknown_since = now if unknown_since is None else unknown_since
            if now - unknown_since > UNKNOWN_LIMIT_SECONDS:
                raise UnexpectedPage("the page stayed unrecognised for more than 5 minutes")
        else:
            unknown_since = None
            if not reported:
                ctx.wait_for_user("verify" if state == takeout.VERIFY_ITS_YOU else "sign_in")
                reported = True
        if now - started > WAIT_LIMIT_SECONDS:
            raise SignInTimedOut("nobody signed in within 48 hours")
        ctx.sleep(POLL_SECONDS)
        ctx.check()
        state = takeout.classify(browser.content())
    if reported:
        ctx.progress("signed in")
