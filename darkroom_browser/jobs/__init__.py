from __future__ import annotations

from collections.abc import Callable
from importlib import metadata

from darkroom_browser.context import JobContext
from darkroom_browser.jobs.connect import connect
from darkroom_browser.jobs.schedule import create_schedule
from darkroom_browser.jobs.status import check_exports

Job = Callable[[JobContext], dict[str, object]]


class UnknownJob(LookupError):
    pass


def playwright_version() -> str | None:
    try:
        return metadata.version("playwright")
    except metadata.PackageNotFoundError:
        return None


def noop(ctx: JobContext) -> dict[str, object]:
    ctx.progress("noop: nothing to do")
    return {"ok": True, "kind": "noop", "playwright": playwright_version()}



REGISTRY: dict[str, Job] = {"noop": noop, "connect": connect, "schedule": create_schedule, "status": check_exports}


def find(kind: str) -> Job:
    try:
        return REGISTRY[kind]
    except KeyError:
        raise UnknownJob(f"no job called {kind!r}") from None
