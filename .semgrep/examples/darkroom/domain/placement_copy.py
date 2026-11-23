import os
import shutil

from darkroom.storage import paths
from darkroom.storage import paths as storage
# ruleid: darkroom-placement-copy-one-caller
from darkroom.storage.paths import staged_placement as copy_ahead
# ruleid: darkroom-placement-copy-one-caller
from darkroom.storage.paths import sweep_placements


def unlink_a_placement_copy(photos, person, export, name):
    # ruleid: darkroom-placement-folder-only-in-paths
    copy = photos.placements(person, export) / name
    # ruleid: darkroom-placement-copy-one-remover
    copy.unlink()


def remove_the_placement_folder(photos, person, export):
    # ruleid: darkroom-placement-copy-one-remover,darkroom-placement-folder-only-in-paths
    shutil.rmtree(photos.placements(person, export))


def move_a_copy_into_place_by_hand(folder, destination):
    # ruleid: darkroom-placement-copy-one-remover,darkroom-placement-folder-only-in-paths
    os.rename(folder / paths.PLACEMENTS, destination)


def remove_through_a_string_built_path(photos, person, export, name):
    # ruleid: darkroom-placement-folder-only-in-paths
    os.unlink(os.path.join(str(photos.root), "_inbox", person, export, ".placements", name))


def reach_the_folder_through_getattr(photos, person, export):
    # ruleid: darkroom-placement-folder-only-in-paths
    return getattr(photos, "placements")(person, export)


def read_the_export_folder(photos, person, export):
    # ok: darkroom-placement-folder-only-in-paths
    return photos.export_tree(person, export).stat().st_size


def copy_ahead_outside_the_ingest(photos, person, export, chunks, sha1):
    # ruleid: darkroom-placement-copy-one-caller
    return paths.staged_placement(photos, person, export, chunks, sha1)


def sweep_outside_the_ingest(photos, person, export):
    # ruleid: darkroom-placement-copy-one-caller
    sweep_placements(photos, person, export)


def sweep_through_getattr(photos, person, export):
    # ruleid: darkroom-placement-copy-one-caller
    return getattr(storage, "sweep_placements")(photos, person, export)


def hand_the_sweep_on(run_later):
    # ruleid: darkroom-placement-copy-one-caller
    run_later(storage.sweep_placements)


def publish_an_unchecked_entry(folder, name, fd, sha1, destination):
    # ruleid: darkroom-placement-copy-one-caller
    return paths.StagedPlacement(folder, name, fd, sha1).publish(destination)


def publish_a_copy_handed_on(ahead, destination):
    # ok: darkroom-placement-copy-one-caller,darkroom-placement-folder-only-in-paths,darkroom-placement-copy-one-remover
    return ahead.publish(destination)
