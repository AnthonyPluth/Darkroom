from __future__ import annotations

from darkroom_browser import selectors
from darkroom_browser.context import Browser, JobContext, JobFailure
from darkroom_browser.pages import takeout

POLL_SECONDS = 30.0
UNKNOWN_LIMIT_SECONDS = 5 * 60.0
WAIT_LIMIT_SECONDS = 48 * 3600.0


class UnexpectedPage(JobFailure):
    pass


class SignInTimedOut(JobFailure):
    pass


class WrongAccount(JobFailure):
    pass


WRONG_ACCOUNT = "WRONG_ACCOUNT"
UNREADABLE = "UNREADABLE"
SIGN_IN_STATES = (takeout.SIGN_IN_NEEDED, takeout.VERIFY_ITS_YOU)


def read_state(ctx: JobContext, page: str) -> str:
    state = takeout.classify(page)
    if state != takeout.SIGNED_IN:
        return state
    expected = str(ctx.payload.get("email", "")).strip().lower()
    if not expected:
        raise WrongAccount("the job does not say which Google account it is for")
    actual = takeout.signed_in_email(page)
    if actual is None:
        return UNREADABLE
    return takeout.SIGNED_IN if actual == expected else WRONG_ACCOUNT


def waiting_reason(state: str) -> str:
    if state == WRONG_ACCOUNT:
        return "wrong_account"
    return "verify" if state == takeout.VERIFY_ITS_YOU else "sign_in"


def ensure_signed_in(ctx: JobContext, browser: Browser) -> None:
    browser.goto(selectors.TAKEOUT_URL)
    started = ctx.clock()
    unknown_since: float | None = None
    reported: str | None = None
    state = read_state(ctx, browser.content())
    while state != takeout.SIGNED_IN:
        now = ctx.clock()
        if state in (takeout.UNKNOWN, UNREADABLE):
            unknown_since = now if unknown_since is None else unknown_since
            if now - unknown_since > UNKNOWN_LIMIT_SECONDS:
                if state == UNREADABLE:
                    raise WrongAccount("could not read which Google account Takeout is signed in to")
                raise UnexpectedPage("the page stayed unrecognised for more than 5 minutes")
        else:
            unknown_since = None
            group = "wrong_account" if state == WRONG_ACCOUNT else "sign_in"
            if reported != group:
                ctx.wait_for_user(waiting_reason(state))
                reported = group
        if now - started > WAIT_LIMIT_SECONDS:
            raise SignInTimedOut("nobody signed in to the right Google account within 48 hours")
        ctx.sleep(POLL_SECONDS)
        ctx.check()
        state = read_state(ctx, browser.content())
    if reported is not None:
        ctx.progress("signed in")
