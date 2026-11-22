import os
import shutil

from darkroom.storage import paths
from darkroom.storage.paths import sweep_placements


def unlink_a_placement_copy(photos, person, export, name):
    copy = photos.placements(person, export) / name
    # ruleid: darkroom-placement-copy-one-remover
    copy.unlink()


def remove_the_placement_folder(photos, person, export):
    # ruleid: darkroom-placement-copy-one-remover
    shutil.rmtree(photos.placements(person, export))


def move_a_copy_into_place_by_hand(folder, destination):
    # ruleid: darkroom-placement-copy-one-remover
    os.rename(folder / paths.PLACEMENTS, destination)


def read_a_placement_copy(photos, person, export, name):
    copy = photos.placements(person, export) / name
    # ok: darkroom-placement-copy-one-remover
    return copy.stat().st_size


def copy_ahead_outside_the_ingest(photos, person, export, chunks, sha1):
    # ruleid: darkroom-placement-copy-one-caller
    return paths.staged_placement(photos, person, export, chunks, sha1)


def sweep_outside_the_ingest(photos, person, export):
    # ruleid: darkroom-placement-copy-one-caller
    sweep_placements(photos, person, export)


def publish_a_copy_handed_on(ahead, destination):
    # ok: darkroom-placement-copy-one-caller
    return ahead.publish(destination)
