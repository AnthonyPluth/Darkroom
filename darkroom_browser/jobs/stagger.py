from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from darkroom_browser.context import Browser, Cancelled, DownloadInterrupted, JobContext, LeaseLost, PartClick, Refused, Unreachable
from darkroom_browser.jobs import download
from darkroom_browser.jobs.session import SIGN_IN_STATES, WAIT_LIMIT_SECONDS, SignInTimedOut, ensure_signed_in, waiting_reason
from darkroom_browser.jobs.status import SignedOut
from darkroom_browser.jobs.telemetry import Clicks
from darkroom_browser.stall import StallWatch, Verdict

POLL_SECONDS = 5.0
PROGRESS_SECONDS = 30.0
SPACE_RETRY_SECONDS = 5 * 60.0
PAUSED_WORDS = "paused for space: the share has too little free room, so no new part starts until it has more; Darkroom asks again every 5 minutes"
RESUMED_WORDS = "free space is back on the share; resuming downloads"


@dataclass
class Flight:
    part: dict[str, Any]
    handle: str
    watch: StallWatch
    reported: float


@dataclass
class Waiting:
    part: dict[str, Any]
    click: PartClick
    since: float
    asking: bool
    reported: bool
    seen: str = ""


class OnePartAtATime:
    def __init__(self, ctx: JobContext, browser: Browser, folder: Path, plan: dict[str, Any], parts: list[dict[str, Any]], taken: set[str], clicks: Clicks) -> None:
        self.ctx = ctx
        self.browser = browser
        self.folder = folder
        self.plan = plan
        self.queue = [part for part in parts if part.get("status") != "done"]
        self.taken = taken
        self.clicks = clicks
        self.flight: Flight | None = None
        self.waiting: Waiting | None = None
        self.signing_in: float | None = None
        self.counted: dict[int, int] = {}
        self.fetched = 0
        self.paused_until: float | None = None
        self.paused = False

    def say(self, message: str) -> None:
        if self.signing_in is None and (self.waiting is None or not self.waiting.asking):
            self.tell(message)

    def tell(self, message: str) -> None:
        self.ctx.progress(f"{message}; {PAUSED_WORDS}" if self.paused and message != PAUSED_WORDS else message)

    def run(self) -> int:
        self.tell("downloading one part at a time; the next part starts when the one before it has been kept or has failed")
        while self.busy():
            self.ctx.check()
            self.advance_flight()
            self.advance_waiting()
            self.advance_signing_in()
            if self.waiting is None and self.signing_in is None:
                self.browser.drop_strays()
                if self.may_start():
                    self.start(self.queue.pop(0))
                    continue
            if self.busy():
                self.ctx.sleep(POLL_SECONDS)
        return self.fetched

    def busy(self) -> bool:
        return bool(self.queue) or self.flight is not None or self.waiting is not None or self.signing_in is not None

    def may_start(self) -> bool:
        if not self.queue or self.flight is not None:
            return False
        return self.paused_until is None or self.ctx.clock() >= self.paused_until

    def signed_in(self) -> bool:
        waited = ensure_signed_in(self.ctx, self.browser)
        if waited and self.paused:
            self.tell("signed in")
        return waited

    def requeue(self, part: dict[str, Any]) -> None:
        self.queue.insert(0, part)

    def held_for_space(self, part: dict[str, Any]) -> bool:
        reply = self.ctx.reporter.part(self.ctx.job_id, part["number"], {"event": "space"})
        if reply.get("gave_up"):
            raise download.DownloadFailed(download.gave_up_message(part["number"], reply["part"], ""))
        if not reply.get("paused_for_space"):
            return False
        self.requeue(part)
        self.paused_until = self.ctx.clock() + SPACE_RETRY_SECONDS
        if not self.paused:
            self.paused = True
            self.say(PAUSED_WORDS)
        return True

    def start(self, part: dict[str, Any]) -> None:
        number = part["number"]
        if self.held_for_space(part):
            return
        try:
            listed = download.part_links(self.ctx, self.browser, str(self.plan.get("google_id", "")), self.plan["part_count"], number, self.clicks, wait=False)[number]
            selector = download.part_link(listed.link)
            if not download.leads_where_checked(self.browser, selector, listed.href):
                raise download.DownloadFailed(f"the browser could not confirm that the link to part {number} on Takeout leads to the address Darkroom checked, so Darkroom downloaded nothing")
        except SignedOut as signed_out:
            self.requeue(part)
            self.signing_in = self.ctx.clock()
            self.ctx.wait_for_user(waiting_reason(signed_out.state))
            return
        google = listed.google_downloads
        send_count = google is not None and (number not in self.counted or google > self.counted[number])
        reply = self.ctx.reporter.part(self.ctx.job_id, number, {"event": "attempt", "google_downloads": google} if send_count else {"event": "attempt"})
        if reply.get("paused_for_space"):
            self.requeue(part)
            self.paused_until = self.ctx.clock() + SPACE_RETRY_SECONDS
            if not self.paused:
                self.paused = True
                self.say(PAUSED_WORDS)
            return
        if self.paused:
            self.paused = False
            self.say(RESUMED_WORDS)
        self.paused_until = None
        if not reply.get("allowed"):
            raise download.DownloadFailed(download.gave_up_message(number, reply["part"], ""))
        self.counted[number] = reply["part"]["downloads_used"]
        clicking = PartClick(selector, listed.href, number)
        if self.kept_late(part, clicking):
            return
        self.say(f"downloading part {number} of {self.plan['part_count']}; {self.clicks.words()}")
        self.clicks.clicked(number, 0)
        try:
            handle = self.browser.start_download(selector, clicking)
        except Cancelled:
            raise
        except Exception as error:
            self.no_download(part, clicking, isinstance(error, DownloadInterrupted))
            return
        self.fly(part, handle)

    def kept_late(self, part: dict[str, Any], clicking: PartClick) -> bool:
        try:
            handle = self.browser.late_download(clicking)
        except Cancelled:
            raise
        except Exception:
            download.report_failure(self.ctx, part["number"], "interrupted")
            self.requeue(part)
            return True
        if handle is None:
            return False
        self.say(f"part {part['number']} started downloading by itself; Darkroom is keeping it rather than clicking again")
        self.fly(part, handle)
        return True

    def fly(self, part: dict[str, Any], handle: str) -> None:
        now = self.ctx.clock()
        self.flight = Flight(part, handle, StallWatch(now), now)
        self.clicks.saw(1)

    def no_download(self, part: dict[str, Any], clicking: PartClick, interrupted: bool) -> None:
        number = part["number"]
        state, seen = download.what_the_click_led_to(self.browser)
        if interrupted:
            download.report_failure(self.ctx, number, "interrupted")
            self.requeue(part)
            if state in SIGN_IN_STATES:
                self.prompt(Waiting(part, clicking, self.ctx.clock(), True, True))
            return
        if state in SIGN_IN_STATES:
            self.prompt(Waiting(part, clicking, self.ctx.clock(), True, False))
            return
        self.say(f"part {number}: clicking its link started no download ({seen}); "
                 f"waiting {int(download.BACKOFF_SECONDS // 60)} minutes in case it starts late before Darkroom tries again")
        self.waiting = Waiting(part, clicking, self.ctx.clock(), False, False, seen)

    def prompt(self, waiting: Waiting) -> None:
        number = waiting.part["number"]
        self.clicks.prompted(number, 0)
        self.waiting = waiting
        self.ctx.wait_for_user("verify")

    def adopt_late(self, waiting: Waiting) -> bool:
        if waiting.reported:
            return False
        number = waiting.part["number"]
        try:
            handle = self.browser.late_download(waiting.click)
        except Cancelled:
            raise
        except Exception:
            self.waiting = None
            download.report_failure(self.ctx, number, "interrupted")
            self.requeue(waiting.part)
            return True
        if handle is None:
            return False
        if waiting.asking:
            self.answered(waiting)
        self.waiting = None
        self.say(f"part {number} started downloading by itself; Darkroom is keeping it rather than clicking again")
        self.fly(waiting.part, handle)
        return True

    def answered(self, waiting: Waiting) -> None:
        self.clicks.answered()
        self.waiting = None
        if self.signed_in():
            self.clicks.answered()
        self.tell(f"verified; resuming part {waiting.part['number']} ({self.clicks.answer_words()})")

    def advance_signing_in(self) -> None:
        if self.signing_in is None:
            return
        state, _ = download.what_the_click_led_to(self.browser)
        if state in SIGN_IN_STATES:
            if self.ctx.clock() - self.signing_in > WAIT_LIMIT_SECONDS:
                raise SignInTimedOut("nobody signed in to the right Google account within 48 hours")
            return
        self.signing_in = None
        self.signed_in()
        self.clicks.answered()
        self.tell("signed in")

    def advance_waiting(self) -> None:
        waiting = self.waiting
        if waiting is None or self.adopt_late(waiting):
            return
        number = waiting.part["number"]
        state, _ = download.what_the_click_led_to(self.browser)
        now = self.ctx.clock()
        if not waiting.asking:
            if state in SIGN_IN_STATES:
                self.prompt(Waiting(waiting.part, waiting.click, now, True, False))
            elif now - waiting.since >= download.BACKOFF_SECONDS and not self.adopt_late(waiting):
                self.waiting = None
                download.report_failure(self.ctx, number, "not_started", waiting.seen)
                self.requeue(waiting.part)
            return
        if state in SIGN_IN_STATES:
            if now - waiting.since > WAIT_LIMIT_SECONDS:
                raise SignInTimedOut("nobody confirmed it was them within 48 hours")
            return
        if self.adopt_late(Waiting(waiting.part, waiting.click, waiting.since, False, waiting.reported)):
            self.clicks.answered()
            return
        self.answered(waiting)
        if not waiting.reported:
            download.report_failure(self.ctx, number, download.ASKED_TO_VERIFY)
            self.requeue(waiting.part)

    def report_bytes(self, number: int, size: int) -> None:
        try:
            self.ctx.reporter.part(self.ctx.job_id, number, {"event": "progress", "bytes": size})
        except (Refused, Unreachable, ValueError) as error:
            if isinstance(error, LeaseLost):
                raise
            return

    def advance_flight(self) -> None:
        flight = self.flight
        if flight is None:
            return
        number = flight.part["number"]
        state = self.browser.download_state(flight.handle)
        now = self.ctx.clock()
        verdict = flight.watch.judge(now, state)
        if verdict is Verdict.WAIT:
            if now - flight.reported >= PROGRESS_SECONDS:
                flight.reported = now
                self.report_bytes(number, state.size)
            return
        self.flight = None
        if verdict is Verdict.STALLED:
            self.browser.cancel_download(flight.handle)
            download.report_failure(self.ctx, number, "interrupted")
            self.requeue(flight.part)
            return
        self.land(flight)

    def land(self, flight: Flight) -> None:
        number = flight.part["number"]
        partial = download.fresh_partial(self.folder)
        try:
            path = self.browser.keep_download(flight.handle, partial)
        except (Cancelled, download.DownloadsFolderRefused):
            raise
        except Exception:
            download.report_failure(self.ctx, number, "interrupted")
            self.requeue(flight.part)
            return
        self.ctx.check()
        problem = download.problem_with(path, flight.part.get("bytes"), self.taken)
        if problem is not None:
            download.report_failure(self.ctx, number, problem)
            self.requeue(flight.part)
            return
        size = path.stat().st_size
        os.replace(path, self.folder / path.name)
        os.chmod(self.folder / path.name, download.PART_MODE)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- Darkroom reads parts through the shared group, others get nothing
        self.ctx.reporter.part(self.ctx.job_id, number, {"event": "done", "filename": path.name, "bytes": size})
        self.taken.add(path.name)
        self.fetched += 1
        self.say(f"part {number} downloaded")


def one_at_a_time(ctx: JobContext, browser: Browser, folder: Path, plan: dict[str, Any], parts: list[dict[str, Any]], taken: set[str], clicks: Clicks) -> int:
    return OnePartAtATime(ctx, browser, folder, plan, parts, taken, clicks).run()


def download_export(ctx: JobContext) -> dict[str, object]:
    return download.download_export(ctx, one_at_a_time)
