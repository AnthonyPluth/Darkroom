import os


def write_once(photos, person, temp):
    # ok: darkroom-originals-read-only
    os.link(temp, photos.originals(person) / "a.jpg")
