from darkroom.domain import takeout_copies
from darkroom.storage import paths
from darkroom.storage.paths import checked_keeper, remove_losing_copy, restore_losing_copy


def drop_a_google_only_copy(photos, person, row):
    # ruleid: darkroom-losing-copy-one-caller
    paths.remove_losing_copy(photos, person.slug, row.removed_path, row.removed_sha1, row.kept_path, row.kept_sha1)


def drop_by_bare_name(photos, slug, takeout, phone):
    # ruleid: darkroom-losing-copy-one-caller
    remove_losing_copy(photos, slug, takeout.path, takeout.sha1, phone.path, phone.sha1)


def resolve_through_the_module(opened, photos, person, now):
    # ok: darkroom-losing-copy-one-caller
    return takeout_copies.resolve_person(opened, photos, person, now)


def put_a_claim_back_by_hand(photos, slug, row):
    # ruleid: darkroom-losing-copy-one-caller
    paths.restore_losing_copy(photos, slug, row.removed_path, row.removed_sha1)


def pin_a_keeper_elsewhere(photos, slug, phone):
    # ruleid: darkroom-losing-copy-one-caller
    with paths.checked_keeper(photos, slug, phone.path, phone.sha1) as keeper:
        return keeper.sidecar()


def internals_by_bare_name(photos, slug, row, phone):
    # ruleid: darkroom-losing-copy-one-caller
    restore_losing_copy(photos, slug, row.removed_path, row.removed_sha1)
    # ruleid: darkroom-losing-copy-one-caller
    return checked_keeper(photos, slug, phone.path, phone.sha1)
