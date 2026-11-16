import os
from pathlib import Path

from darkroom.storage import paths


def add_by_hand(client, photos, pixel_id):
    # ruleid: darkroom-syncthing-folder-path-from-photos
    client.add_folder(f"darkroom-pixel-{pixel_id}", f"/photos/_pixels/{pixel_id}", [], paused=True)


def add_beside_the_root(client, photos, pixel_id):
    # ruleid: darkroom-syncthing-folder-path-from-photos
    folder = photos.root
    # ruleid: darkroom-syncthing-folder-path-from-photos
    client.add_folder("darkroom-pixel-1", str(folder), [], paused=True)


def add_with_a_joined_path(client, photos, pixel_id):
    # ruleid: darkroom-syncthing-folder-path-from-photos
    joined = os.path.join("/photos", paths.PIXELS, str(pixel_id))
    # ruleid: darkroom-syncthing-folder-path-from-photos
    built = Path("/photos")
    # ruleid: darkroom-syncthing-folder-path-from-photos
    client.add_folder("darkroom-pixel-1", path=joined, devices=[])
    return built


def add_from_the_helper(client, photos, pixel_id):
    # ok: darkroom-syncthing-folder-path-from-photos
    client.add_folder(f"darkroom-pixel-{pixel_id}", str(photos.pixel_root(pixel_id)), [], paused=True)


def add_what_was_planned(client, want):
    # ok: darkroom-syncthing-folder-path-from-photos
    client.add_folder(want.folder, want.path, [], paused=True)
    # ok: darkroom-syncthing-folder-path-from-photos
    client.add_folder(want.folder, path=want.path, devices=[], paused=True)


def add_again_where_syncthing_had_it(client):
    for found in client.folders():
        # ruleid: darkroom-syncthing-folder-path-from-photos
        client.add_folder(found.id, found.path, [], paused=True)
        # ruleid: darkroom-syncthing-folder-path-from-photos
        client.add_folder(found.id, path=found.path, devices=[], paused=True)


def add_a_planned_path_under_another_folder(client, want, found):
    # ruleid: darkroom-syncthing-folder-path-from-photos
    client.add_folder(found.id, want.path, [], paused=True)
