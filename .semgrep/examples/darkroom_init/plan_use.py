import subprocess


def start_it():
    # ruleid: darkroom-subprocess-only-exiftool
    return subprocess.Popen(["Xvfb", ":99"])


def name_it(spec):
    # ok: darkroom-subprocess-only-exiftool
    return spec.name
