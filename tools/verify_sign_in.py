from __future__ import annotations

import signal
import sys
import threading

from tests.fakes.immich import FakeImmich
from tests.fakes.oidc_provider import CLIENT_ID, FakeProvider

OWNER = {"sub": "verify-owner", "email": "owner@example.com", "name": "Sam Example", "groups": ["parents"]}


def main() -> int:
    provider = FakeProvider()
    provider.signs_in = dict(OWNER)
    provider.start()
    immich = FakeImmich([]).start()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda _signum, _frame: stop.set())
    signal.signal(signal.SIGINT, lambda _signum, _frame: stop.set())
    print(f"{provider.base} {CLIENT_ID} {immich.url}", flush=True)
    stop.wait()
    immich.stop()
    provider.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
