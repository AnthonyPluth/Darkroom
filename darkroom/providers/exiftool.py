from __future__ import annotations

import shutil
import subprocess


class Missing(RuntimeError):
    pass


def version() -> str:
    path = shutil.which("exiftool")
    if path is None:
        raise Missing("exiftool is not installed; install it (libimage-exiftool-perl) and try again")
    done = subprocess.run([path, "-ver"], capture_output=True, text=True, check=True, timeout=30)
    return done.stdout.strip()
