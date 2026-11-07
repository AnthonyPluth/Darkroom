from darkroom.domain import takeout_copies
from darkroom.storage import paths


def drop_a_google_only_copy(photos, person, row):
    # ruleid: darkroom-losing-copy-one-caller
    paths.remove_losing_copy(photos, person.slug, row.removed_path, row.removed_sha1, row.kept_path, row.kept_sha1)


def resolve_through_the_module(opened, photos, person, now):
    # ok: darkroom-losing-copy-one-caller
    return takeout_copies.resolve_person(opened, photos, person, now)
