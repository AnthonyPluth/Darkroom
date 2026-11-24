from sqlalchemy import delete, insert, select, update

from darkroom.storage.models import MediaItem, RecoveryPoint, RecoveryPointAsset, RetentionAttestation


def open_a_point(opened, now):
    # ruleid: darkroom-recovery-point-one-writer
    opened.add(RecoveryPoint(opened_at=now, state="open", manifest_count=0, manifest_sha256="0" * 64))


def list_an_asset(opened, point_id):
    # ruleid: darkroom-recovery-point-one-writer
    opened.add(RecoveryPointAsset(recovery_point_id=point_id, owner_id="o", asset_id="a", sha1="a" * 40, source="receipt"))


def accept_without_asking(opened, now):
    # ruleid: darkroom-recovery-point-one-writer
    opened.add(RetentionAttestation(kind="remote_deletion_boundary", recorded_at=now, evidence_url="e", boundary_text_sha256="0" * 64))


def succeed_a_point(opened, point_id):
    # ruleid: darkroom-recovery-point-one-writer
    opened.execute(update(RecoveryPoint).where(RecoveryPoint.id == point_id).values(state="succeeded"))


def add_in_bulk(opened, rows):
    # ruleid: darkroom-recovery-point-one-writer
    opened.execute(insert(RecoveryPointAsset).values(rows))


def forget_the_manifest(opened, point_id):
    # ruleid: darkroom-recovery-point-one-writer
    opened.execute(delete(RecoveryPointAsset).where(RecoveryPointAsset.recovery_point_id == point_id))


def withdraw_by_hand(opened, now):
    # ruleid: darkroom-recovery-point-one-writer
    opened.get(RetentionAttestation, 1).withdrawn_at = now


def change_a_point(opened):
    # ruleid: darkroom-recovery-point-one-writer
    opened.get(RecoveryPoint, 1).state = "succeeded"


def read_the_points(opened):
    # ok: darkroom-recovery-point-one-writer
    return opened.execute(select(RecoveryPoint).where(RecoveryPoint.state == "succeeded")).scalars().all()


def read_a_manifest(opened, point_id):
    # ok: darkroom-recovery-point-one-writer
    return opened.execute(select(RecoveryPointAsset).where(RecoveryPointAsset.recovery_point_id == point_id)).scalars().all()


def read_the_active_records(opened):
    # ok: darkroom-recovery-point-one-writer
    return opened.get(RetentionAttestation, 1)


def write_something_else(opened, item_id):
    # ok: darkroom-recovery-point-one-writer
    opened.execute(update(MediaItem).where(MediaItem.id == item_id).values(width=1))
