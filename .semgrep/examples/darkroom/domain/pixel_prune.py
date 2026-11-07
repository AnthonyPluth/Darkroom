import os
import shutil

from darkroom.storage import paths


def unlink_a_copy(photos, pixel_id, person, name):
    copy = photos.pixel_copy(pixel_id, person, name)
    # ruleid: darkroom-pixel-prune-one-remover
    copy.unlink()


def remove_the_folder(photos, pixel_id, person):
    folder = photos.pixel_folder(pixel_id, person)
    # ruleid: darkroom-pixel-prune-one-remover
    shutil.rmtree(folder)


def remove_by_joined_name(photos, pixel_id, name):
    target = os.path.join(photos.root, paths.PIXELS, str(pixel_id), name)
    # ruleid: darkroom-pixel-prune-one-remover
    os.remove(target)


def move_out_of_the_folder(photos, pixel_id, person, name, elsewhere):
    # ruleid: darkroom-pixel-prune-one-remover
    os.rename(photos.pixel_copy(pixel_id, person, name), elsewhere)


def read_a_copy(photos, pixel_id, person, name):
    copy = photos.pixel_copy(pixel_id, person, name)
    # ok: darkroom-pixel-prune-one-remover
    return copy.stat().st_size


def remove_a_staged_file(photos, person, export):
    staged = photos.staging(person, export) / "a.jpg"
    # ok: darkroom-pixel-prune-one-remover
    staged.unlink()


def prune_without_the_check(photos, copy):
    # ruleid: darkroom-pixel-prune-one-caller
    paths.remove_pixel_copy(photos, copy.pixel_id, "jo", copy.name, copy.sha1)

