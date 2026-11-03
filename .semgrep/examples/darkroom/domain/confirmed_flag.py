from sqlalchemy import update

from darkroom.storage.models import MediaItem


def mark(item):
    # ruleid: darkroom-confirmed-flag-single-writer
    item.confirmed_in_google = True


def mark_by_setattr(item):
    # ruleid: darkroom-confirmed-flag-single-writer
    setattr(item, "confirmed_in_google", True)


def mark_in_bulk(opened, ids):
    # ruleid: darkroom-confirmed-flag-single-writer
    opened.execute(update(MediaItem).where(MediaItem.id.in_(ids)).values(confirmed_in_google=True))


def mark_by_dict(opened, ids):
    # ruleid: darkroom-confirmed-flag-single-writer
    opened.query(MediaItem).filter(MediaItem.id.in_(ids)).update({"confirmed_in_google": True})


def made_confirmed(person_id):
    # ruleid: darkroom-confirmed-flag-single-writer
    return MediaItem(person_id=person_id, source="phone", confirmed_in_google=True)


def mark_in_row(row):
    # ruleid: darkroom-confirmed-flag-single-writer
    row["confirmed_in_google"] = True


def unconfirmed(opened):
    # ok: darkroom-confirmed-flag-single-writer
    return opened.query(MediaItem).filter_by(confirmed_in_google=False).all()


def is_confirmed(item):
    # ok: darkroom-confirmed-flag-single-writer
    return item.confirmed_in_google
