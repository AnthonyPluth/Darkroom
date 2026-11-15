import subprocess


def start_child(argv):
    # ok: darkroom-subprocess-only-exiftool
    return subprocess.Popen(argv, start_new_session=True)
