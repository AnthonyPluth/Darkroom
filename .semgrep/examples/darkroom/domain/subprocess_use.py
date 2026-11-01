import os
import subprocess


def run_it():
    # ruleid: darkroom-subprocess-only-exiftool
    return subprocess.run(["ls"], check=True)


def shell_it():
    # ruleid: darkroom-subprocess-only-exiftool
    os.system("ls")


def quote_it(name):
    # ok: darkroom-subprocess-only-exiftool
    return os.path.basename(name)
