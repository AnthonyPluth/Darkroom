import json
import sys

from PIL import Image
from sqlalchemy import select

from darkroom import config
from darkroom.dates import utc_now
from darkroom.domain import accounts, index, people, pixel_folder, pixels, transport
from darkroom.storage import db
from darkroom.storage.models import Event, MediaItem, Person, PixelCopy
from darkroom.storage.paths import PIXELS, Photos

SLUG = "ci"
PHOTOS = 5
COLOURS = ((200, 40, 40), (40, 200, 40), (40, 40, 200), (200, 200, 40), (40, 200, 200))


def opened_session():
    settings = config.load()
    engine = db.make_engine(db.database_url(settings))
    return engine, db.session(engine), Photos(settings.photos_root)


def write_originals(photos, count):
    folder = photos.originals(SLUG) / "2026"
    folder.mkdir(parents=True, exist_ok=True)
    for number in range(1, count + 1):
        Image.new("RGB", (64 + number, 48), COLOURS[(number - 1) % len(COLOURS)]).save(folder / f"IMG_{number:04d}.jpg", "JPEG")


def seed(opened, photos, device, now, count=PHOTOS):
    person = people.create(opened, photos, "CI Person", SLUG, None, None, None, None, now)
    account = accounts.create(opened, "ci.photos@example.com", person.id, now)
    pixel = pixels.create(opened, "CI Pixel", device, account.id, [person.id], now)
    opened.commit()
    write_originals(photos, count)
    index.index_person(opened, photos, person, now)
    opened.commit()
    items = opened.execute(select(MediaItem).where(MediaItem.person_id == person.id, MediaItem.source == "phone").order_by(MediaItem.id)).scalars().all()
    queued = sum(pixel_folder.queue(opened, item, now) for item in items)
    opened.commit()
    if queued != count:
        raise SystemExit(f"queued {queued} copies, not {count}")
    return pixel.id


def set_device(opened, pixel_id, device, now):
    pixels.update(opened, pixel_id, {"syncthing_device_id": device}, now)
    opened.commit()


def copies(opened):
    slugs = dict(opened.execute(select(Person.id, Person.slug)).all())
    rows = opened.execute(select(PixelCopy).order_by(PixelCopy.id)).scalars().all()
    return [{"id": row.id, "state": row.state, "sha1": row.sha1, "placed_at": row.placed_at, "pruned_at": row.pruned_at,
             "path": None if row.name is None else f"{PIXELS}/{row.pixel_id}/{slugs[row.person_id]}/{row.name}"} for row in rows]


def folder_removed(opened, pixel_id):
    found = opened.execute(select(Event.id).where(Event.type == transport.FOLDER_REMOVED, Event.subject == f"pixel {pixel_id}").limit(1)).first()
    return found is not None


def run(opened, photos, command, args, now):
    if command == "seed":
        print(seed(opened, photos, args[0], now))
    elif command == "device":
        set_device(opened, int(args[0]), None if args[1] == "-" else args[1], now)
    elif command == "copies":
        print(json.dumps(copies(opened), sort_keys=True))
    elif command == "placed":
        print(sum(row["state"] == "placed" for row in copies(opened)))
    elif command == "removed":
        return 0 if folder_removed(opened, int(args[0])) else 1
    else:
        raise SystemExit(f"unknown command {command}")
    return 0


def main(command, *args):
    engine, session, photos = opened_session()
    try:
        with session as opened:
            return run(opened, photos, command, args, utc_now().timestamp())
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
