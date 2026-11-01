from __future__ import annotations

from collections.abc import Callable

from darkroom_browser.context import JobContext

Job = Callable[[JobContext], dict[str, object]]


class UnknownJob(LookupError):
    pass


def noop(ctx: JobContext) -> dict[str, object]:
    ctx.progress("noop: nothing to do")
    return {"ok": True, "kind": "noop"}


REGISTRY: dict[str, Job] = {"noop": noop}


def find(kind: str) -> Job:
    try:
        return REGISTRY[kind]
    except KeyError:
        raise UnknownJob(f"no job called {kind!r}") from None
