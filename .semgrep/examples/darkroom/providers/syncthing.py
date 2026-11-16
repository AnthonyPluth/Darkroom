def add_folder(client, identity, path, devices):
    # ok: darkroom-syncthing-folder-path-from-photos
    client.call("POST", "/rest/config/folders", body={"id": identity, "path": path, "type": "sendonly", "paused": True})


def a_default_path(client, identity):
    # ruleid: darkroom-syncthing-folder-path-from-photos
    client.call("POST", "/rest/config/folders", body={"id": identity, "path": "/photos/_pixels/1", "type": "sendonly", "paused": True})
