from __future__ import annotations

import functools
import os
import sys

from darkroom_browser import config, live, monitoring
from darkroom_browser.client import Client
from darkroom_browser.runner import Runner

WORKER_UMASK = 0o007


def main() -> int:
    settings = config.load()
    found = config.problems(settings)
    for problem in found:
        monitoring.log("worker refuses to start", problem=problem)
    if found:
        return 1
    os.umask(WORKER_UMASK)
    runner = Runner(settings, Client(config.darkroom_url(settings), settings.token), functools.partial(live.open_context, working_root=settings.photos_root))
    try:
        runner.run_forever()
    except KeyboardInterrupt:
        runner.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
