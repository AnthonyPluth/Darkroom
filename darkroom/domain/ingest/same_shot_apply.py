from __future__ import annotations

from contextlib import ExitStack

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from darkroom import config, monitoring
from darkroom.domain import alerts, managed_ingest
from darkroom.domain.ingest import hold_apply, run
from darkroom.providers import immich
from darkroom.storage import db
from darkroom.storage.models import Export, ManagedCompound, ManagedCompoundMember, ManagedImportIntent, ManagedImportReceipt, ManagedSameShotHold, Person
from darkroom.storage.paths import Photos

IMPORTED = "imported"
WAITING_DAY = 24 * 3600.0


def due(opened: Session) -> list[int]:
    query = (select(ManagedImportReceipt.id).join(ManagedSameShotHold, ManagedSameShotHold.receipt_id == ManagedImportReceipt.id)
             .join(ManagedImportIntent, ManagedImportIntent.id == ManagedImportReceipt.intent_id).join(Export, Export.id == ManagedImportIntent.export_id)
             .where(ManagedSameShotHold.status == "upload", ManagedImportIntent.kind == "import", Export.status == IMPORTED,
                    ManagedImportReceipt.resource_key.startswith("takeout-entry:"), ManagedImportReceipt.resource_key == ManagedImportReceipt.compound_key,
                    ManagedImportReceipt.state != "committed",
                    or_(ManagedImportReceipt.metadata_hold.is_(None), ManagedImportReceipt.metadata_hold.not_in(managed_ingest.TERMINAL)))
             .order_by(ManagedImportReceipt.id))
    return list(opened.scalars(query))


def waiting(opened: Session, settings: config.Settings, hold: ManagedSameShotHold, receipt: ManagedImportReceipt, now: float, tools: run.Tools) -> None:
    if hold.resolved_at is None or now - hold.resolved_at < WAITING_DAY:
        return
    db.write_lock(opened)
    managed_ingest.once(opened, settings, upload_key(hold), f"receipt {receipt.id}",
                        "Darkroom could not complete the upload the owner chose for a same-shot photo. It keeps the copy and tries again.", now, tools.notifier,
                        alerts.person(hold.person_id))
    opened.commit()


def finished(opened: Session, hold: ManagedSameShotHold, receipt: ManagedImportReceipt, intent: ManagedImportIntent, person: Person, now: float) -> None:
    hold_apply.resolved(opened, receipt, intent, person, now)
    alerts.close(opened, upload_key(hold), now)


def upload_key(hold: ManagedSameShotHold) -> str:
    return f"managed:same-shot-upload:{hold.id}"


def send_one(opened: Session, settings: config.Settings, photos: Photos, receipt_id: int, now: float, tools: run.Tools, import_source: hold_apply.ImportSource) -> None:
    receipt = opened.get(ManagedImportReceipt, receipt_id, populate_existing=True)
    hold = None if receipt is None else opened.execute(select(ManagedSameShotHold).where(ManagedSameShotHold.receipt_id == receipt_id)
                                                       .execution_options(populate_existing=True)).scalar_one_or_none()
    intent = None if receipt is None else opened.get(ManagedImportIntent, receipt.intent_id, populate_existing=True)
    person = None if intent is None else opened.get(Person, intent.person_id, populate_existing=True)
    if receipt is None or hold is None or intent is None or person is None or hold.status != "upload" or hold.owner_id != intent.owner_id or person.immich_user_id != intent.owner_id:
        opened.rollback()
        return
    try:
        with ExitStack() as stack:
            source = hold_apply.source_of(opened, photos, stack, receipt, intent, person, import_source)
            hold_apply.tags_of(source, receipt, tools.read)
            reader = managed_ingest.reader_of(settings, person, intent, tools.immich)
            if not hold_apply.send(opened, reader, source, receipt, intent, now, lambda: finished(opened, hold, receipt, intent, person, now)):
                waiting(opened, settings, hold, receipt, now, tools)
    except (hold_apply.UnreadableSource, *hold_apply.SOURCE_ERRORS):
        opened.rollback()
        monitoring.log("photo copy for a same-shot upload could not be read", receipt=receipt_id)
        waiting(opened, settings, hold, receipt, now, tools)
    except (immich.ImmichFailed, run.Busy) as error:
        opened.rollback()
        monitoring.log("same-shot upload not sent to Immich", receipt=receipt_id, error=type(error).__name__)
        waiting(opened, settings, hold, receipt, now, tools)


def send_released(opened: Session, settings: config.Settings, photos: Photos, now: float, tools: run.Tools, import_source: hold_apply.ImportSource) -> int:
    sent = 0
    for receipt_id in due(opened):
        send_one(opened, settings, photos, receipt_id, now, tools, import_source)
        sent += 1
    for compound_id, hold_id in due_compounds(opened):
        send_compound(opened, settings, photos, compound_id, hold_id, now, tools)
        sent += 1
    return sent


def due_compounds(opened: Session) -> list[tuple[int, int]]:
    member = ManagedCompoundMember
    query = (select(ManagedCompound.id, ManagedSameShotHold.id).join(member, member.compound_id == ManagedCompound.id)
             .join(ManagedImportReceipt, (ManagedImportReceipt.intent_id == member.intent_id) & (ManagedImportReceipt.resource_key == member.resource_key))
             .join(ManagedSameShotHold, ManagedSameShotHold.receipt_id == ManagedImportReceipt.id)
             .join(ManagedImportIntent, ManagedImportIntent.id == ManagedCompound.intent_id).join(Export, Export.id == ManagedImportIntent.export_id)
             .where(ManagedSameShotHold.status == "upload", ManagedImportIntent.kind == "import", Export.status == IMPORTED, ManagedCompound.committed_at.is_(None),
                    ManagedCompound.hold.is_(None))
             .order_by(ManagedCompound.id))
    return [(compound_id, hold_id) for compound_id, hold_id in opened.execute(query).all()]


def send_compound(opened: Session, settings: config.Settings, photos: Photos, compound_id: int, hold_id: int, now: float, tools: run.Tools) -> None:
    compound = opened.get(ManagedCompound, compound_id, populate_existing=True)
    hold = opened.get(ManagedSameShotHold, hold_id, populate_existing=True)
    if compound is None or hold is None or hold.status != "upload":
        opened.rollback()
        return
    managed_ingest.send_kept_compound(opened, settings, photos, compound, now, tools.immich)
    opened.expire_all()
    compound = opened.get(ManagedCompound, compound_id)
    hold = opened.get(ManagedSameShotHold, hold_id)
    receipt = None if hold is None else opened.get(ManagedImportReceipt, hold.receipt_id)
    if compound is None or hold is None or receipt is None:
        return
    if compound.committed_at is not None:
        db.write_lock(opened)
        alerts.close(opened, upload_key(hold), now)
        opened.commit()
    else:
        waiting(opened, settings, hold, receipt, now, tools)
