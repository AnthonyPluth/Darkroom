from sqlalchemy import select

from darkroom.storage.models import Device


def find_device(opened, digest):
    # ruleid: darkroom-device-token-single-path
    return opened.execute(select(Device).where(Device.token_hash == digest)).scalar_one_or_none()


def find_device_by_filter(opened, digest):
    # ruleid: darkroom-device-token-single-path
    return opened.query(Device).filter_by(token_hash=digest).one_or_none()


def made_device(person_id, digest):
    # ok: darkroom-device-token-single-path
    return Device(person_id=person_id, name="iPhone", token_hash=digest, created=1.0)


def devices_of(opened, person_id):
    # ok: darkroom-device-token-single-path
    return opened.execute(select(Device).where(Device.person_id == person_id)).scalars().all()
