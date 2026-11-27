import os
import shutil

from darkroom.domain.ingest import part_free
from darkroom.domain import retention_gate
from darkroom.storage import paths
from darkroom.storage.paths import remove_review_hold


def unlink_a_hold_by_folder(photos, person, export, name):
    folder = photos.review_hold(person, export)
    # ruleid: darkroom-review-hold-one-remover
    (folder / name).unlink()


def remove_the_review_folder(photos, person, export):
    folder = photos.review_hold(person, export)
    # ruleid: darkroom-review-hold-one-remover
    shutil.rmtree(folder)


def remove_a_hold_by_name(photos, person, export, name):
    hold = paths.review_hold_path(photos, person, export, name)
    # ruleid: darkroom-review-hold-one-remover
    os.remove(hold)


def move_a_hold_away(photos, relative, export, elsewhere):
    # ruleid: darkroom-review-hold-one-remover
    os.rename(paths.review_hold_at(photos, relative, export), elsewhere)


def read_a_hold(photos, person, export, name):
    hold = paths.review_hold_path(photos, person, export, name)
    # ok: darkroom-review-hold-one-remover
    return hold.stat().st_size


def remove_an_old_folder_hold_after_the_14_days(photos, path):
    # ok: darkroom-review-hold-one-remover
    return paths.remove_inbox_file(photos, path)


def release_without_the_proof(photos, person, export, name, identity):
    # ruleid: darkroom-review-hold-one-caller
    paths.remove_review_hold(photos, person, export, name, identity)


def release_by_a_bare_name(photos, person, export, name, identity):
    # ruleid: darkroom-review-hold-one-caller
    remove_review_hold(photos, person, export, name, identity)


def release_after_only_a_database_check(opened, photos, person, export, review, now, name, identity):
    if retention_gate.refusal(opened, review, now, release_enabled=True) is None:
        # ruleid: darkroom-review-hold-one-caller
        paths.remove_review_hold(photos, person, export, name, identity)


def name_a_hold(photos, person, export, name):
    # ok: darkroom-review-hold-one-caller
    return paths.review_hold_path(photos, person, export, name)


def release_ready_holds(opened, settings, now):
    # ok: darkroom-review-hold-one-caller
    return part_free.free_ready(opened, settings, now)
