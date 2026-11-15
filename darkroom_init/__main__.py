from __future__ import annotations

import os
import secrets
import signal
import sys
from collections.abc import Callable, Mapping

from darkroom_init import plan, supervisor

TOKEN_BYTES = 32


def app_ready(given: Mapping[str, str]) -> Callable[[], bool]:
    port = given.get("DARKROOM_PORT", "8000")
    return lambda: supervisor.listening(int(port) if port.isascii() and port.isdigit() else 8000)


def main(given: Mapping[str, str] | None = None) -> int:
    given = dict(os.environ if given is None else given)
    supervisor.prctl(supervisor.PR_SET_DUMPABLE, 0)
    adopt = supervisor.prctl(supervisor.PR_SET_CHILD_SUBREAPER, 1)
    held: dict[plan.Secret, str] = {"token": secrets.token_urlsafe(TOKEN_BYTES), "viewer": secrets.token_urlsafe(TOKEN_BYTES)}
    running = supervisor.Supervisor(plan.specs(given, app_ready(given), supervisor.display_ready(given)), held, adopt_orphans=adopt)
    signal.signal(signal.SIGTERM, running.stop)
    signal.signal(signal.SIGINT, running.stop)
    supervisor.log("init started", orphans_adopted=int(adopt))
    return running.run()


if __name__ == "__main__":
    sys.exit(main())
