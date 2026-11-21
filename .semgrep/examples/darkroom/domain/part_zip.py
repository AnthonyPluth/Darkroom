import os
import shutil

from darkroom.domain.ingest import unpack
from darkroom.storage import paths
from darkroom.storage.paths import remove_held_copy


def unlink_a_part(export, part):
    zipped = unpack.part_path(export, part)
    # ruleid: darkroom-part-zip-one-remover
    zipped.unlink()


def remove_the_export_folder(photos, person, export):
    folder = photos.export_tree(person, export)
    # ruleid: darkroom-part-zip-one-remover
    shutil.rmtree(folder)


def remove_a_held_copy_by_name(photos, person, export, name):
    held = photos.held(person, export) / name
    # ruleid: darkroom-part-zip-one-remover
    os.remove(held)


def move_a_part_away(photos, person, export, name, elsewhere):
    # ruleid: darkroom-part-zip-one-remover
    os.rename(paths.part_zip_path(photos, person, export, name), elsewhere)


def read_a_part(export, part):
    zipped = unpack.part_path(export, part)
    # ok: darkroom-part-zip-one-remover
    return zipped.stat().st_size


def remove_a_part_after_the_14_days(photos, export, part):
    # ok: darkroom-part-zip-one-remover
    return paths.remove_inbox_file(photos, unpack.part_path(export, part))


def free_without_the_proof(photos, person, export, name, identity):
    # ruleid: darkroom-part-zip-one-caller
    paths.remove_part_zip(photos, person, export, name, identity)


def free_a_held_copy_without_the_proof(photos, person, export, name, identity):
    # ruleid: darkroom-part-zip-one-caller
    remove_held_copy(photos, person, export, name, identity)


def name_a_held_copy(photos, person, export, name):
    # ok: darkroom-part-zip-one-caller
    return paths.held_copy_path(photos, person, export, name)


def zip_gone_by_identity(photos, path):
    # ruleid: darkroom-absence-through-entry-absent
    return paths.inbox_identity(photos, path) is None


def held_copy_still_there(photos, path):
    # ruleid: darkroom-absence-through-entry-absent
    return paths.inbox_identity(photos, path) is not None


def zip_gone_by_entry(photos, path):
    # ok: darkroom-absence-through-entry-absent
    return paths.inbox_entry_absent(photos, path)
