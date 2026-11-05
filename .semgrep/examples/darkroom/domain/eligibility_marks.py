from sqlalchemy import select, update

from darkroom.storage.models import Device, PhoneAsset


def mark(asset, now):
    # ruleid: darkroom-eligibility-single-path
    asset.eligible_at = now


def mark_confirmed(asset, now):
    # ruleid: darkroom-eligibility-single-path
    asset.eligible_confirmed_at = now


def mark_deleted(asset, now):
    # ruleid: darkroom-eligibility-single-path
    asset.deleted_at = now


def mark_checked(device, now):
    # ruleid: darkroom-eligibility-single-path
    device.eligibility_checked_at = now


def clear_both(asset):
    # ruleid: darkroom-eligibility-single-path
    asset.eligible_at, asset.eligible_confirmed_at = None, None


def clear_second(asset, row):
    # ruleid: darkroom-eligibility-single-path
    row.sha1, asset.eligible_confirmed_at = "", None


def mark_by_setattr(asset, now):
    # ruleid: darkroom-eligibility-single-path
    setattr(asset, "eligible_at", now)


def mark_in_bulk(opened, device_id, now):
    # ruleid: darkroom-eligibility-single-path
    opened.execute(update(PhoneAsset).where(PhoneAsset.device_id == device_id).values(eligible_at=now))


def mark_by_dict(opened, now):
    # ruleid: darkroom-eligibility-single-path
    opened.query(PhoneAsset).update({"deleted_at": now})


def mark_in_row(row, now):
    # ruleid: darkroom-eligibility-single-path
    row["eligible_confirmed_at"] = now


def made_eligible(device_id, now):
    # ruleid: darkroom-eligibility-single-path
    return PhoneAsset(device_id=device_id, photokit_id="x", created=now, eligible_at=now)


def turn_cleanup_on(device):
    # ruleid: darkroom-eligibility-single-path
    device.cleanup_enabled = True


def turn_cleanup_on_from_a_body(device, body):
    # ruleid: darkroom-eligibility-single-path
    device.cleanup_enabled = body["cleanup_enabled"]


def made_with_cleanup(person_id):
    # ruleid: darkroom-eligibility-single-path
    return Device(person_id=person_id, name="iPhone", token_hash="h", cleanup_enabled=True, created=1.0)


def cleanup_on_in_bulk(opened):
    # ruleid: darkroom-eligibility-single-path
    opened.execute(update(Device).values(cleanup_enabled=True))


def turn_cleanup_off(device):
    # ok: darkroom-eligibility-single-path
    device.cleanup_enabled = False


def made_without_cleanup(person_id):
    # ok: darkroom-eligibility-single-path
    return Device(person_id=person_id, name="iPhone", token_hash="h", cleanup_enabled=False, created=1.0)


def shown(device):
    # ok: darkroom-eligibility-single-path
    return {"id": device.id, "cleanup_enabled": device.cleanup_enabled}


def waiting(opened, device_id):
    # ok: darkroom-eligibility-single-path
    return opened.execute(select(PhoneAsset).where(PhoneAsset.device_id == device_id, PhoneAsset.eligible_at.is_not(None))).scalars().all()


def not_deleted(opened):
    # ok: darkroom-eligibility-single-path
    return opened.query(PhoneAsset).filter_by(deleted_at=None).all()


def is_eligible(asset):
    # ok: darkroom-eligibility-single-path
    return asset.eligible_at is not None
