from darkroom.domain import managed_upload
from darkroom.providers import immich


def upload_without_the_receipt(client, key, body):
    # ruleid: darkroom-immich-upload-one-caller
    return client.upload(key, body)


def check_without_the_receipt(reader, sha1):
    # ruleid: darkroom-immich-upload-one-caller
    return reader.bulk_check(sha1)


def send_an_import_route_by_hand(client, key):
    # ruleid: darkroom-immich-upload-one-caller
    return client.call("POST", immich.BULK_CHECK_PATH, key=key, importing=True, body={})


def upload_through_the_receipt(opened, reader, receipt_id, body, now):
    # ok: darkroom-immich-upload-one-caller
    return managed_upload.upload_receipt(opened, reader, receipt_id, body, now)


def read_back_a_managed_original(reader, asset_id, sha1):
    # ok: darkroom-immich-upload-one-caller
    return reader.get_asset(asset_id)
