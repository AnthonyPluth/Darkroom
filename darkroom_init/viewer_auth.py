from __future__ import annotations

import os
from typing import Any

VIEWER_USER = "darkroom"


def read_to_end(fd: int) -> str:
    with os.fdopen(fd, "rb") as pipe:
        return pipe.read().decode("ascii").strip()


class SecretOnPipe:
    def __init__(self, src: str) -> None:
        from websockify.auth_plugins import BasicHTTPAuth
        secret = read_to_end(int(src))
        if len(secret) < 32:
            raise ValueError("the viewer secret did not arrive")
        self.basic = BasicHTTPAuth(f"{VIEWER_USER}:{secret}")

    def authenticate(self, headers: Any, target_host: Any, target_port: Any) -> None:
        self.basic.authenticate(headers, target_host, target_port)
