from __future__ import annotations

from darkroom_browser.context import JobContext
from darkroom_browser.jobs.schedule import create_schedule


def renew_schedule(ctx: JobContext) -> dict[str, object]:
    ctx.progress("creating next year's export schedule")
    return create_schedule(ctx)
