from darkroom.domain import share_bind, share_check
from darkroom.storage import share, stored_settings
from darkroom.storage.share import SHARE_SENTINEL_FILE, write_sentinel


def repair_a_missing_sentinel(root, token):
    # ruleid: darkroom-share-sentinel-one-writer
    share.write_sentinel(root, share.sentinel_content(token))


def repair_by_bare_name(root, token):
    # ruleid: darkroom-share-sentinel-one-writer
    write_sentinel(root, token.encode())


def record_a_new_identity(opened, token, now):
    # ruleid: darkroom-share-sentinel-one-writer
    stored_settings.record_share(opened, token, now)


def bind_from_the_web(opened, found, now):
    # ruleid: darkroom-share-sentinel-one-writer
    share_bind.bind_share(opened, found, now)


def write_the_file_by_hand(root, token):
    # ruleid: darkroom-share-sentinel-one-writer
    (root / ".darkroom-share").write_text(token)
    # ruleid: darkroom-share-sentinel-one-writer
    (root / share.SHARE_SENTINEL_FILE).write_text(token)
    # ruleid: darkroom-share-sentinel-one-writer
    return root / SHARE_SENTINEL_FILE


def check_the_share(check, opened, now):
    # ok: darkroom-share-sentinel-one-writer
    return check.refusal(opened, now)


def read_what_is_there(root, token):
    # ok: darkroom-share-sentinel-one-writer
    return share_check.sentinel_problem(root, token)
