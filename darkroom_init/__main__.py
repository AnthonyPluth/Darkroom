from __future__ import annotations

import os
import secrets
import signal
import sys
from collections.abc import Mapping
from pathlib import Path

from darkroom_init import plan, sockets, supervisor, syncthing

TOKEN_BYTES = 32


def app_ready() -> bool:
    return sockets.accepting(plan.WORKER_SOCKET, plan.SOCKET_FOLDER)


def main(given: Mapping[str, str] | None = None) -> int:
    given = dict(os.environ if given is None else given)
    supervisor.prctl(supervisor.PR_SET_DUMPABLE, 0)
    adopt = supervisor.prctl(supervisor.PR_SET_CHILD_SUBREAPER, 1)
    held: dict[plan.Secret, str] = {"token": secrets.token_urlsafe(TOKEN_BYTES), "viewer": secrets.token_urlsafe(TOKEN_BYTES)}
    if plan.syncthing_on(given):
        held["syncthing"] = secrets.token_urlsafe(TOKEN_BYTES)
    ready = syncthing.key_rotated(Path(plan.syncthing_home(given)), held)
    running = supervisor.Supervisor(plan.specs(given, app_ready, supervisor.display_ready(given), ready), held, adopt_orphans=adopt)
    signal.signal(signal.SIGTERM, running.stop)
    signal.signal(signal.SIGINT, running.stop)
    supervisor.log("init started", orphans_adopted=int(adopt))
    return running.run()


if __name__ == "__main__":
    sys.exit(main())
