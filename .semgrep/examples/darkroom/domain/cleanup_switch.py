from sqlalchemy import update

from darkroom.storage.models import Device


def turn_cleanup_on(device):
    # ruleid: darkroom-cleanup-never-on
    device.cleanup_enabled = True


def turn_cleanup_on_from_a_body(device, body):
    # ruleid: darkroom-cleanup-never-on
    device.cleanup_enabled = body["cleanup_enabled"]


def made_with_cleanup(person_id):
    # ruleid: darkroom-cleanup-never-on
    return Device(person_id=person_id, name="iPhone", token_hash="h", cleanup_enabled=True, created=1.0)


def cleanup_on_in_bulk(opened):
    # ruleid: darkroom-cleanup-never-on
    opened.execute(update(Device).values(cleanup_enabled=True))


def turn_cleanup_off(device):
    # ok: darkroom-cleanup-never-on
    device.cleanup_enabled = False


def made_without_cleanup(person_id):
    # ok: darkroom-cleanup-never-on
    return Device(person_id=person_id, name="iPhone", token_hash="h", cleanup_enabled=False, created=1.0)


def shown(device):
    # ok: darkroom-cleanup-never-on
    return {"id": device.id, "cleanup_enabled": device.cleanup_enabled}
