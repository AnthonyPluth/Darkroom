from sqlalchemy import select

from darkroom.storage.models import Device


def find_inside_the_devices_module(opened, digest):
    # ruleid: darkroom-device-token-single-path
    return opened.execute(select(Device).where(Device.token_hash == digest)).scalar_one_or_none()
