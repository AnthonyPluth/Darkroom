from darkroom.storage import paths


def prune_copy(photos, copy, slug):
    # ok: darkroom-pixel-prune-one-caller
    return paths.remove_pixel_copy(photos, copy.pixel_id, slug, copy.name, copy.sha1)
