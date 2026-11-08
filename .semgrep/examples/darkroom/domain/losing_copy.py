from darkroom.domain import takeout_copies
from darkroom.storage import paths
from darkroom.storage.paths import checked_keeper, drop_claimed, drop_losing_copy, file_sha1, holding, remove_losing_copy, restore_losing_copy, unlink_claimed


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


def deleting_internals_by_bare_name(folder, private, name, sha1, keeper, found):
    # ruleid: darkroom-losing-copy-one-caller
    drop_losing_copy(folder, name, sha1, keeper, None)
    # ruleid: darkroom-losing-copy-one-caller
    drop_claimed(folder, name, private, 3, sha1, keeper, None)
    # ruleid: darkroom-losing-copy-one-caller
    unlink_claimed(private, "copy", found)
    # ruleid: darkroom-losing-copy-one-caller
    with holding(keeper):
        pass


def hashing_a_file_is_fine(path):
    # ok: darkroom-losing-copy-one-caller
    return file_sha1(path)
