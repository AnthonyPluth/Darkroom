from __future__ import annotations

from darkroom_browser.context import JobContext
from darkroom_browser.jobs.session import ensure_signed_in


def connect(ctx: JobContext) -> dict[str, object]:
    ensure_signed_in(ctx, ctx.browser())
    return {"session_state": "ok"}
