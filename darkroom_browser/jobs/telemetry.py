from __future__ import annotations

from darkroom_browser import monitoring
from darkroom_browser.context import JobContext, LeaseLost, Refused, Unreachable


def counted(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def lasted(seconds: float) -> str:
    whole = int(max(0.0, seconds) // 60)
    hours, minutes = divmod(whole, 60)
    if not hours:
        return counted(minutes, "minute")
    return f"{counted(hours, 'hour')} {counted(minutes, 'minute')}" if minutes else counted(hours, "hour")


def seconds_since(now: float, then: float | None) -> int | None:
    return None if then is None else int(max(0.0, now - then))


def gap_words(since_click: int | None, since_answer: int | None) -> str:
    click = "the first click of this job" if since_click is None else f"{counted(since_click // 60, 'minute')} after the previous click"
    answer = "no prompt answered yet in this job" if since_answer is None else f"{counted(since_answer // 60, 'minute')} after the last answered prompt"
    return f"{click}, {answer}"


class Clicks:
    def __init__(self, ctx: JobContext) -> None:
        self.ctx = ctx
        self.first_click: float | None = None
        self.last_click: float | None = None
        self.last_answer: float | None = None
        self.since_answer = 0
        self.in_a_row = 0
        self.clicks = 0
        self.prompts = 0
        self.most_in_a_row = 0
        self.most_downloading = 0
        self.asked_after = 0
        self.last_gaps: dict[str, object] = {}

    def gaps(self) -> dict[str, object]:
        now = self.ctx.clock()
        found: dict[str, object] = {}
        since_click, since_answer = seconds_since(now, self.last_click), seconds_since(now, self.last_answer)
        if since_click is not None:
            found["since_click"] = since_click
        if since_answer is not None:
            found["since_answer"] = since_answer
        return found

    def words(self) -> str:
        now = self.ctx.clock()
        return gap_words(seconds_since(now, self.last_click), seconds_since(now, self.last_answer))

    def answer_words(self) -> str:
        return f"prompt {self.prompts} of this job, after {counted(self.asked_after, 'part click')} since the last answered prompt"

    def send(self, number: int, body: dict[str, object]) -> None:
        try:
            self.ctx.reporter.part(self.ctx.job_id, number, body)
        except LeaseLost:
            raise
        except (Refused, Unreachable, ValueError) as error:
            monitoring.log("download telemetry not recorded", job=self.ctx.job_id, error=type(error).__name__)

    def clicked(self, number: int, downloading: int) -> None:
        self.last_gaps = self.gaps()
        self.send(number, {"event": "clicked", "downloading": downloading} | self.last_gaps)
        now = self.ctx.clock()
        self.first_click = now if self.first_click is None else self.first_click
        self.last_click = now
        self.clicks += 1
        self.since_answer += 1
        self.in_a_row += 1
        self.most_downloading = max(self.most_downloading, downloading + 1)

    def prompted(self, number: int, downloading: int) -> None:
        self.send(number, {"event": "prompted", "downloading": downloading, "clicks": self.since_answer} | self.last_gaps)
        self.prompts += 1
        self.asked_after = self.since_answer
        self.most_in_a_row = max(self.most_in_a_row, self.in_a_row - 1)
        self.in_a_row = 0

    def answered(self) -> None:
        self.last_answer = self.ctx.clock()
        self.since_answer = 0

    def longest(self) -> int:
        return max(self.most_in_a_row, self.in_a_row)

    def summary(self) -> str:
        took = lasted(self.ctx.clock() - self.first_click) if self.first_click is not None else lasted(0.0)
        return (f"{counted(self.clicks, 'part click')} and {counted(self.prompts, 'verification prompt')} in {took}; "
                f"at most {counted(self.longest(), 'part click')} in a row without a prompt; at most {counted(self.most_downloading, 'part')} downloading at once")

    def result(self) -> dict[str, object]:
        return {"clicks": self.clicks, "prompts": self.prompts, "most_clicks_in_a_row_without_a_prompt": self.longest(), "most_downloading": self.most_downloading}
