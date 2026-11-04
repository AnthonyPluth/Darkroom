import os
import shutil


def overwrite(photos, person):
    target = photos.originals(person) / "IMG_0001.HEIC"
    # ruleid: darkroom-originals-read-only
    with open(target, "wb") as out:
        out.write(b"")


def append_with_mode(photos, person):
    folder = photos.originals(person)
    # ruleid: darkroom-originals-read-only
    with open(folder / "notes.txt", mode="a") as out:
        out.write("x")


def low_level(photos, person):
    # ruleid: darkroom-originals-read-only
    return os.open(photos.originals(person) / "a.jpg", os.O_WRONLY | os.O_CREAT)


def remove(photos, person, relative):
    original = photos.original(person, relative)
    # ruleid: darkroom-originals-read-only
    os.remove(original)


def rename_away(photos, person, elsewhere):
    original = photos.originals(person) / "a.jpg"
    # ruleid: darkroom-originals-read-only
    os.rename(original, elsewhere)


def replace_onto(photos, person, staged):
    # ruleid: darkroom-originals-read-only
    os.replace(staged, photos.originals(person) / "a.jpg")


def move_onto(photos, person, staged):
    folder = photos.originals(person)
    # ruleid: darkroom-originals-read-only
    shutil.move(staged, folder)


def copy_onto(photos, person, staged):
    # ruleid: darkroom-originals-read-only
    shutil.copy2(staged, photos.originals(person) / "a.jpg")


def from_the_row(person_row, staged):
    # ruleid: darkroom-originals-read-only
    shutil.copyfile(staged, person_row.originals_path)


def path_methods(photos, person):
    original = photos.original(person, "jo/originals/a.jpg")
    # ruleid: darkroom-originals-read-only
    original.write_bytes(b"")


def unlink_temp(photos, person):
    # ruleid: darkroom-originals-read-only
    (photos.uploads_tmp(person) / "part").unlink()


def open_for_writing(photos, person):
    # ruleid: darkroom-originals-read-only
    return photos.original(person, "jo/originals/a.jpg").open("r+b")


def read_it(photos, person):
    original = photos.original(person, "jo/originals/a.jpg")
    # ok: darkroom-originals-read-only
    with open(original, "rb") as handle:
        return handle.read()


def read_with_path(photos, person):
    # ok: darkroom-originals-read-only
    return photos.original(person, "jo/originals/a.jpg").open("rb").read()


def copy_out_of_originals(photos, person, staging):
    # ok: darkroom-originals-read-only
    shutil.copy2(photos.originals(person) / "a.jpg", staging / "a.jpg")


def write_google_only(photos, person):
    # ok: darkroom-originals-read-only
    (photos.google_only(person, 2020, 1) / "a.jpg").write_bytes(b"")


def delete_the_tree(photos, person):
    # ruleid: darkroom-originals-read-only
    shutil.rmtree(photos.originals(person))
