from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from fractions import Fraction
from io import BytesIO
from pathlib import Path

from PIL import ExifTags, Image
from pillow_heif import register_heif_opener
from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom import oidc
from darkroom.config import Settings, viewer_link
from darkroom.domain import alerts, browser_jobs, events, index, media, pixel_check, renewal, share_check, transport, transport_status
from darkroom.providers import syncthing
from darkroom.storage import db
from darkroom.storage.models import (Alert, BrowserJob, DedupeDecision, Export, ExportPart, GoogleAccount, ImportRun, MediaItem, Person, Pixel, PixelPerson, PixelTransport,
                                     ReviewItem, TakeoutEntry)
from darkroom.storage.paths import Photos, write_once

register_heif_opener()

HOUR = 3600.0
DAY = 86_400.0
MAKE = "Example"
MODEL = "DemoPhone"
MEMBER = oidc.Identity("demo:avery", "avery@example.com", "Avery Example", ("parents",), True)
SOUTH_PACIFIC = (-33.5, -150.25)
NORTH_ATLANTIC = (41.25, -40.5)
INDIAN_OCEAN = (-12.75, 88.5)
OCEANS = frozenset({SOUTH_PACIFIC, NORTH_ATLANTIC, INDIAN_OCEAN})
QUICKTIME_EPOCH = 2_082_844_800
TIMESCALE = 600
MATRIX = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
SIZE = (128, 64)


@dataclass(frozen=True)
class DemoPerson:
    slug: str
    name: str
    email: str


@dataclass(frozen=True)
class DemoAccount:
    email: str
    default: str
    pixel: str | None
    syncthing_device_id: str | None
    serves: tuple[str, ...]
    session_state: str
    schedule_days_left: int
    renewal: str | None = None


@dataclass(frozen=True)
class Shot:
    stem: str
    kind: str
    days_ago: int
    clock: str
    offset: str | None
    gps: tuple[float, float] | None
    colour: tuple[int, int, int]
    seconds: float = 0.0


PEOPLE = (
    DemoPerson("avery", "Avery Example", "avery@example.com"),
    DemoPerson("blair", "Blair Example", "blair@example.com"),
    DemoPerson("casey", "Casey Example", "casey@example.com"),
)

RENEWAL_UNSURE = "ScheduleNotConfirmed: Takeout did not confirm a recurring export; check Takeout before trying again"
RENEWAL_FAILED = "UnexpectedPage: the page stayed unrecognised for more than 5 minutes"

ACCOUNTS = (
    DemoAccount("avery.photos@example.com", "avery", "Family Pixel", "DEMO-FAMILY-PIXEL", ("avery", "blair"), "needs_sign_in", 20, RENEWAL_UNSURE),
    DemoAccount("casey.photos@example.com", "casey", "Casey's Pixel", "DEMO-CASEY-PIXEL", ("casey",), "ok", 300),
    DemoAccount("blair.photos@example.com", "blair", "Blair's Pixel", "DEMO-BLAIR-PIXEL", ("blair",), "ok", -10, RENEWAL_FAILED),
)


@dataclass(frozen=True)
class DemoPixel:
    name: str
    account: str
    syncthing_device_id: str | None
    serves: tuple[str, ...]


def demo_device(label: str) -> str:
    data = f"DEMO{label}".ljust(52, "A")
    return "DEMO-" + "".join(data[start:start + 13] + syncthing.check_character(data[start:start + 13]) for start in range(0, 52, 13))[4:]


SHARED_DEVICE = demo_device("SHARED")

PIXELS = (
    DemoPixel("Avery's old Pixel", "avery.photos@example.com", "DEMO-AVERY-OLD-PIXEL", ("avery",)),
    DemoPixel("Casey's work Pixel", "casey.photos@example.com", SHARED_DEVICE, ("casey",)),
    DemoPixel("Casey's spare Pixel", "casey.photos@example.com", SHARED_DEVICE, ("casey",)),
    DemoPixel("Blair's second Pixel", "blair.photos@example.com", "DEMO-BLAIR-SECOND-PIXEL", ("blair",)),
    DemoPixel("Avery's travel Pixel", "avery.photos@example.com", "DEMO-AVERY-TRAVEL-PIXEL", ("avery",)),
)


@dataclass(frozen=True)
class DemoTransport:
    pixel: str
    connected: bool
    seen_ago: float | None
    error: str | None = None
    unaccounted: int = 0


TRANSPORT = (
    DemoTransport("Family Pixel", True, 6 * 60.0),
    DemoTransport("Casey's Pixel", False, 4 * DAY + 2 * HOUR),
    DemoTransport("Blair's Pixel", False, 2 * DAY, transport.HELD, 2),
    DemoTransport("Avery's old Pixel", False, None, transport.INVALID_DEVICE),
    DemoTransport("Casey's work Pixel", False, 3 * DAY, transport.SHARED_DEVICE),
    DemoTransport("Casey's spare Pixel", False, None, transport.SHARED_DEVICE),
    DemoTransport("Blair's second Pixel", False, 2 * DAY + 3 * HOUR, transport.REMOVED),
    DemoTransport("Avery's travel Pixel", False, None, transport.PATH_REFUSED),
)
SYNCTHING_STATES = ("running", "down", "share_check_failed")
UNPAIRED = (DemoPixel("Casey's new Pixel", "casey.photos@example.com", None, ("casey",)),)

SHOTS: dict[str, tuple[Shot, ...]] = {
    "avery": (
        Shot("IMG_0001", "jpeg", 2, "09:15:30", "-10:00", SOUTH_PACIFIC, (200, 40, 40)),
        Shot("IMG_0002", "heic", 9, "18:40:05", None, None, (40, 160, 90)),
        Shot("IMG_0003", "live", 45, "12:05:50", "+06:00", INDIAN_OCEAN, (30, 60, 200), 2.5),
        Shot("IMG_0004", "video", 120, "07:30:15", "-03:00", None, (0, 0, 0), 12.0),
    ),
    "blair": (
        Shot("IMG_0101", "jpeg", 3, "16:20:00", None, NORTH_ATLANTIC, (220, 180, 40)),
        Shot("IMG_0102", "jpeg", 30, "11:11:11", "-10:00", SOUTH_PACIFIC, (120, 40, 200)),
        Shot("IMG_0103", "heic", 400, "20:00:00", None, None, (40, 40, 40)),
    ),
    "casey": (
        Shot("IMG_0201", "heic", 1, "08:00:00", "+06:00", INDIAN_OCEAN, (240, 120, 40)),
        Shot("IMG_0202", "jpeg", 75, "13:45:00", None, None, (90, 200, 220)),
        Shot("IMG_0203", "video", 200, "19:05:00", "-03:00", None, (0, 0, 0), 4.0),
    ),
}


@dataclass(frozen=True)
class Summary:
    people: int
    accounts: int
    pixels: int
    originals: int
    indexed: int
    failed: int
    alerts: int


def noon(today: date) -> float:
    return datetime.combine(today, time(12, 0), UTC).timestamp()


def taken_at(shot: Shot, today: date) -> tuple[datetime, float]:
    local = datetime.combine(today - timedelta(days=shot.days_ago), time.fromisoformat(shot.clock))
    offset = media.parse_offset(shot.offset) if shot.offset else None
    return local, media.local_to_utc(local, offset or 0)


def degrees(value: float) -> tuple[Fraction, Fraction, Fraction]:
    whole = abs(value)
    full = int(whole)
    minutes = int((whole - full) * 60)
    seconds = round(((whole - full) * 60 - minutes) * 60 * 10_000)
    return Fraction(full), Fraction(minutes), Fraction(seconds, 10_000)


def exif_for(shot: Shot, local: datetime) -> bytes:
    exif = Image.Exif()
    exif[ExifTags.Base.Make] = MAKE
    exif[ExifTags.Base.Model] = MODEL
    taken = exif.get_ifd(ExifTags.IFD.Exif)
    taken[ExifTags.Base.DateTimeOriginal] = local.strftime("%Y:%m:%d %H:%M:%S")
    if shot.offset:
        taken[ExifTags.Base.OffsetTimeOriginal] = shot.offset
    if shot.gps:
        gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
        gps[ExifTags.GPS.GPSLatitudeRef] = "N" if shot.gps[0] >= 0 else "S"
        gps[ExifTags.GPS.GPSLatitude] = degrees(shot.gps[0])
        gps[ExifTags.GPS.GPSLongitudeRef] = "E" if shot.gps[1] >= 0 else "W"
        gps[ExifTags.GPS.GPSLongitude] = degrees(shot.gps[1])
    return exif.tobytes()


def picture(colour: tuple[int, int, int]) -> Image.Image:
    image = Image.new("RGB", SIZE, colour)
    for x in range(0, SIZE[0], 4):
        image.putpixel((x, x % SIZE[1]), (255 - colour[0], 255 - colour[1], 255 - colour[2]))
    return image


def atom(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def creation_keys(created_local: str) -> bytes:
    key = b"mdtacom.apple.quicktime.creationdate"
    handler = atom(b"hdlr", bytes(8) + b"mdta" + bytes(13))
    keys = atom(b"keys", struct.pack(">BxxxI", 0, 1) + struct.pack(">I", 4 + len(key)) + key)
    value = atom(b"data", struct.pack(">II", 1, 0) + created_local.encode())
    return atom(b"meta", handler + keys + atom(b"ilst", atom(struct.pack(">I", 1), value)))


def quicktime(created_utc: float, created_local: str, seconds: float, width: int, height: int) -> bytes:
    stamp = int(created_utc) + QUICKTIME_EPOCH
    duration = int(seconds * TIMESCALE)
    mvhd = atom(b"mvhd", struct.pack(">BxxxIIII", 0, stamp, stamp, TIMESCALE, duration) + struct.pack(">IH", 0x10000, 0x100) + bytes(10)
                + MATRIX + bytes(24) + struct.pack(">I", 2))
    tkhd = atom(b"tkhd", struct.pack(">BBBBIIIII", 0, 0, 0, 7, stamp, stamp, 1, 0, duration) + bytes(16) + MATRIX
                + struct.pack(">II", width << 16, height << 16))
    mdhd = atom(b"mdhd", struct.pack(">BxxxIIIIHH", 0, stamp, stamp, TIMESCALE, duration, 0x55C4, 0))
    handler = atom(b"hdlr", bytes(4) + b"mhlr" + b"vide" + bytes(13))
    vmhd = atom(b"vmhd", struct.pack(">BBBBHHHH", 0, 0, 0, 1, 0, 0, 0, 0))
    dinf = atom(b"dinf", atom(b"dref", struct.pack(">BxxxI", 0, 1) + atom(b"url ", struct.pack(">BBBB", 0, 0, 0, 1))))
    avc1 = atom(b"avc1", bytes(6) + struct.pack(">H", 1) + bytes(16) + struct.pack(">HHIIII", width, height, 0x480000, 0x480000, 0, 1)
                + bytes(32) + struct.pack(">Hh", 24, -1))
    stbl = atom(b"stbl", atom(b"stsd", struct.pack(">BxxxI", 0, 1) + avc1) + atom(b"stts", struct.pack(">BxxxIII", 0, 1, 1, duration))
                + atom(b"stsc", struct.pack(">BxxxIIII", 0, 1, 1, 1, 1)) + atom(b"stsz", struct.pack(">BxxxIII", 0, 0, 1, 4))
                + atom(b"stco", struct.pack(">BxxxII", 0, 1, 0)))
    track = atom(b"trak", tkhd + atom(b"mdia", mdhd + handler + atom(b"minf", vmhd + dinf + stbl)))
    ftyp = atom(b"ftyp", b"qt  " + struct.pack(">I", 0x20050300) + b"qt  ")
    return ftyp + atom(b"moov", mvhd + track + creation_keys(created_local)) + atom(b"mdat", bytes(4))


def encoded(shot: Shot, local: datetime, fmt: str) -> bytes:
    out = BytesIO()
    picture(shot.colour).save(out, format=fmt, exif=exif_for(shot, local))
    return out.getvalue()


def create_only(path: Path, content: bytes, utc: float) -> Path:
    write_once(path, [content])
    os.utime(path, (utc, utc))
    return path


def write_shot(folder: Path, shot: Shot, today: date) -> list[Path]:
    local, utc = taken_at(shot, today)
    written: list[Path] = []
    if shot.kind in ("jpeg", "heic", "live"):
        suffix, fmt = (".JPG", "JPEG") if shot.kind == "jpeg" else (".HEIC", "HEIF")
        written.append(create_only(folder / f"{shot.stem}{suffix}", encoded(shot, local, fmt), utc))
    if shot.kind in ("live", "video"):
        created_local = local.strftime("%Y-%m-%dT%H:%M:%S") + (shot.offset or "+00:00").replace(":", "")
        written.append(create_only(folder / f"{shot.stem}.MOV", quicktime(utc, created_local, shot.seconds, *SIZE), utc))
    return written


def seed_person(opened: Session, photos: Photos, who: DemoPerson, today: date, now: float) -> tuple[Person, int, index.IndexReport]:
    person = Person(name=who.name, slug=who.slug, email=who.email, originals_path=f"{who.slug}/originals", google_only_path=f"{who.slug}/google-only",
                    created=now - 400 * DAY)
    opened.add(person)
    opened.flush()
    originals = photos.ensure_person_tree(who.slug)[0]
    files = [path for shot in SHOTS[who.slug] for path in write_shot(originals, shot, today)]
    report = index.index_person(opened, photos, person, now)
    events.record(opened, now, "originals_indexed", who.slug, f"new={report.new} failed={report.failed}")
    return person, len(files), report


def seed_account(opened: Session, settings: Settings, people: dict[str, Person], spec: DemoAccount, now: float) -> int:
    account = GoogleAccount(email=spec.email, default_person_id=people[spec.default].id, session_state=spec.session_state, session_checked=now - 2 * HOUR,
                            schedule_created=now - (365 - spec.schedule_days_left) * DAY, schedule_end=now + spec.schedule_days_left * DAY, created=now - 300 * DAY)
    opened.add(account)
    opened.flush()
    if spec.pixel is not None:
        pixel = Pixel(name=spec.pixel, syncthing_device_id=spec.syncthing_device_id, google_account_id=account.id, created=now - 300 * DAY)
        opened.add(pixel)
        opened.flush()
        for slug in spec.serves:
            opened.add(PixelPerson(pixel_id=pixel.id, person_id=people[slug].id))
        opened.flush()
    renewing = seed_renewal(opened, settings, account, spec.renewal) if spec.renewal is not None else 0
    if spec.session_state != "needs_sign_in":
        return renewing
    alerts.open_alert(opened, settings, browser_jobs.sign_in_key(account.id), "sign_in_needed", account.email, alerts.sign_in_needed(account.email),
                      viewer_link(settings), now - 2 * HOUR, notifier=alerts.recorded, about=alerts.account(account.id))
    return renewing + 1


def seed_pixel(opened: Session, people: dict[str, Person], spec: DemoPixel, now: float) -> None:
    account = opened.execute(select(GoogleAccount).where(GoogleAccount.email == spec.account)).scalar_one()
    pixel = Pixel(name=spec.name, syncthing_device_id=spec.syncthing_device_id, google_account_id=account.id, created=now - 200 * DAY)
    opened.add(pixel)
    opened.flush()
    for slug in spec.serves:
        opened.add(PixelPerson(pixel_id=pixel.id, person_id=people[slug].id))
    opened.flush()


def seed_renewal(opened: Session, settings: Settings, account: GoogleAccount, message: str) -> int:
    assert account.schedule_end is not None
    asked = account.schedule_end - renewal.NOTICE + 5 * HOUR
    job = BrowserJob(account_id=account.id, kind=browser_jobs.RENEW_KIND, status="failed", payload="{}", created=asked, started=asked + 60, heartbeat=asked + 300,
                     finished=asked + 300, message=message)
    opened.add(job)
    opened.flush()
    renewal.job_failed(opened, settings, job, asked + 300)
    return 1


DUPLICATE_OF = "IMG_0101"
DUPLICATE_REASON = "Looks like IMG_0101.JPG, but the two differ slightly (recompressed): worth a look"


def seed_import(opened: Session, photos: Photos, people: dict[str, Person], today: date, now: float) -> None:
    owner = people["avery"]
    account = opened.execute(select(GoogleAccount).where(GoogleAccount.default_person_id == owner.id)).scalar_one()
    export = Export(account_id=account.id, kind="incremental", google_id="demo-export-1", created_at=now - 4 * DAY, ready_at=now - 3 * DAY,
                    expires_at=now + 4 * DAY, part_count=1, status="imported", stage="done", owner_person_id=owner.id)
    opened.add(export)
    opened.flush()
    opened.add(ExportPart(export_id=export.id, number=1, filename="takeout-demo-1.zip", bytes=2_400_000, downloads_used=1, attempts=1, status="done"))
    opened.add(ImportRun(export_id=export.id, started=now - 3 * DAY, finished=now - 3 * DAY + 600, stage="done", added=2, skipped_t1=1, skipped_t2=1,
                         skipped_t3=0, review=1, missing_gps=3, failed=0, status="done"))
    shot = next(candidate for candidate in SHOTS["blair"] if candidate.stem == DUPLICATE_OF)
    matched = opened.execute(select(MediaItem).where(MediaItem.person_id == people["blair"].id, MediaItem.path.like(f"%{DUPLICATE_OF}.%"))).scalar_one()
    source = f"Takeout/Google Photos/{DUPLICATE_OF}.JPG"
    decision = DedupeDecision(export_id=export.id, account_id=account.id, source_path=source, person_id=people["blair"].id, matched_item_id=matched.id,
                              tier=4, reason=DUPLICATE_REASON, decided_by="auto", decided_at=now - 3 * DAY + 300)
    opened.add(decision)
    opened.flush()
    blair = people["blair"].slug
    hold = photos.review_hold(blair, f"export-{export.id}") / f"{DUPLICATE_OF}.JPG"
    local, utc = taken_at(shot, today)
    held = Shot(shot.stem, shot.kind, shot.days_ago, shot.clock, shot.offset, shot.gps, (shot.colour[0], shot.colour[1], shot.colour[2] + 20))
    create_only(hold, encoded(held, local, "JPEG"), utc)
    opened.add(ReviewItem(decision_id=decision.id, status="pending", hold_path=f"_inbox/{blair}/review/export-{export.id}/{hold.name}",
                          hold_sha1=media.sha1_of(hold)[0]))


def seed_unpaired(opened: Session, people: dict[str, Person], spec: DemoPixel, now: float) -> None:
    account = opened.execute(select(GoogleAccount).where(GoogleAccount.email == spec.account)).scalar_one()
    pixel = Pixel(name=spec.name, syncthing_device_id=None, google_account_id=account.id, created=now - DAY)
    opened.add(pixel)
    opened.flush()
    for slug in spec.serves:
        opened.add(PixelPerson(pixel_id=pixel.id, person_id=people[slug].id))
    opened.flush()


def seed_download(opened: Session, now: float) -> None:
    owner = opened.execute(select(Person).where(Person.slug == "casey")).scalar_one()
    account = opened.execute(select(GoogleAccount).where(GoogleAccount.default_person_id == owner.id)).scalar_one()
    export = Export(account_id=account.id, kind="incremental", google_id="demo-export-2", created_at=now - DAY, ready_at=now - 2 * HOUR,
                    expires_at=now + 7 * DAY - 2 * HOUR, part_count=3, status="downloading", download_tries=0, owner_person_id=owner.id)
    opened.add(export)
    opened.flush()
    opened.add(ExportPart(export_id=export.id, number=1, filename="takeout-demo-2-001.zip", bytes=3_200_000, bytes_done=3_200_000, downloads_used=1, attempts=1,
                          status="done"))
    opened.add(ExportPart(export_id=export.id, number=2, bytes=3_100_000, bytes_done=1_240_000, downloads_used=2, attempts=2, status="downloading"))
    opened.add(ExportPart(export_id=export.id, number=3, bytes=1_800_000, status="pending"))


def seed_failed_download(opened: Session, now: float, slug: str = "blair", number: int = 3) -> None:
    owner = opened.execute(select(Person).where(Person.slug == slug)).scalar_one()
    account = opened.execute(select(GoogleAccount).where(GoogleAccount.default_person_id == owner.id)).scalar_one()
    export = Export(account_id=account.id, kind="incremental", google_id=f"demo-export-{number}", created_at=now - 2 * DAY, ready_at=now - 2 * DAY + HOUR,
                    expires_at=now + 5 * DAY + HOUR, part_count=2, status="failed", download_tries=0, owner_person_id=owner.id)
    opened.add(export)
    opened.flush()
    opened.add(ExportPart(export_id=export.id, number=1, filename=f"takeout-demo-{number}-001.zip", bytes=2_000_000, bytes_done=2_000_000, downloads_used=1,
                          attempts=1, status="done"))
    opened.add(ExportPart(export_id=export.id, number=2, bytes=1_500_000, downloads_used=3, attempts=3, status="failed"))


PART_IMPORT = (
    ("ingested", 1517, 1517, 0, 0, 0),
    ("ingested", 1402, 1402, 21, 2, 0),
    ("ingesting", 1488, 812, 0, 0, 0),
    ("ingested", 1402, 1402, 0, 0, 2),
    ("refused", None, 0, 0, 0, 0),
    ("failed", 900, 300, 0, 0, 0),
    ("queued", None, 0, 0, 0, 0),
)


FREED_PART: dict[str, object] = {"free_state": "freed", "bytes_freed": 2_000_000}


def seed_part_import(opened: Session, now: float) -> None:
    owner = opened.execute(select(Person).where(Person.slug == "avery")).scalar_one()
    account = opened.execute(select(GoogleAccount).where(GoogleAccount.default_person_id == owner.id)).scalar_one()
    export = Export(account_id=account.id, kind="incremental", google_id="demo-export-5", created_at=now - DAY, ready_at=now - 6 * HOUR,
                    expires_at=now + 7 * DAY - 6 * HOUR, part_count=len(PART_IMPORT) + 1, status="downloading", download_tries=0, owner_person_id=owner.id)
    opened.add(export)
    opened.flush()
    for number, (state, entries, done, sidecar, whole, failed) in enumerate(PART_IMPORT, start=1):
        opened.add(ExportPart(export_id=export.id, number=number, filename=f"takeout-demo-5-00{number}.zip", bytes=2_000_000, bytes_done=2_000_000, downloads_used=1,
                              attempts=1, status="done", ingest_state=state, ingest_queued_at=now - 5 * HOUR, entries=entries, entries_done=done,
                              held=sidecar + whole, failed_items=failed, **(FREED_PART if number == 1 else {})))
        opened.flush()
        for held in range(sidecar + whole):
            opened.add(TakeoutEntry(export_id=export.id, part_number=number, name=f"Takeout/Google Photos/Demo/held-{number}-{held}.jpg", kind="media", bytes=1000,
                                    crc32=held, outcome="waiting_sidecar" if held < sidecar else "waiting_export", listed_at=now - 4 * HOUR))
    opened.add(ExportPart(export_id=export.id, number=len(PART_IMPORT) + 1, bytes=2_000_000, bytes_done=1_240_000, downloads_used=1, attempts=1, status="downloading"))


def seed_transport(opened: Session, now: float) -> None:
    for spec in TRANSPORT:
        pixel = opened.execute(select(Pixel).where(Pixel.name == spec.pixel)).scalar_one()
        row = opened.get(PixelTransport, pixel.id) or PixelTransport(pixel_id=pixel.id, updated=now)
        opened.add(row)
        seen = None if spec.seen_ago is None else now - spec.seen_ago
        row.paired, row.connected, row.last_seen, row.completion, row.error, row.updated = spec.error is None, spec.connected, seen, None if seen is None else 100.0, spec.error, now
        row.synced = spec.connected and seen is not None
        pixel.last_sync = seen
        if spec.error == transport.REMOVED and not transport.removed_before(opened, pixel.id):
            events.record(opened, now - 2 * DAY, transport.FOLDER_REMOVED, f"pixel {pixel.id}")
        if spec.unaccounted:
            events.record(opened, now - 2 * HOUR, pixel_check.UNACCOUNTED, f"pixel {pixel.id}", f"files {spec.unaccounted}")
    opened.flush()


def transport_rows(opened: Session, error: str, now: float) -> None:
    for row in opened.execute(select(PixelTransport)).scalars().all():
        row.paired, row.connected, row.synced, row.error, row.updated = False, False, False, error, now


def syncthing_state(opened: Session, settings: Settings, state: str, now: float) -> None:
    db.write_lock(opened)
    if state == "down":
        events.record(opened, now - 2 * HOUR, transport.FAILED, transport.SUBJECT, transport.UNREACHABLE)
        transport_rows(opened, transport.DOWN, now)
    elif state == "share_check_failed":
        events.record(opened, now - HOUR, share_check.FAILED, share_check.SUBJECT, share_check.MISSING)
        events.record(opened, now - HOUR, transport.PAUSED, transport.SUBJECT, share_check.MISSING)
        transport_rows(opened, transport.SHARE, now)
    else:
        last = share_check.last_share_event(opened)
        if last is not None and last[0] == share_check.FAILED:
            events.record(opened, now, share_check.PASSED, share_check.SUBJECT)
        if transport.last_outcome(opened) not in (None, (transport.RECOVERED, None)):
            events.record(opened, now, transport.RECOVERED, transport.SUBJECT)
        seed_transport(opened, now)
    transport_status.review_unlocked(opened, settings, now, alerts.recorded)
    opened.commit()


def seed(opened: Session, photos: Photos, settings: Settings, today: date) -> Summary:
    now = noon(today)
    people: dict[str, Person] = {}
    originals = indexed = failed = 0
    for who in PEOPLE:
        person, written, report = seed_person(opened, photos, who, today, now)
        people[who.slug] = person
        originals, indexed, failed = originals + written, indexed + report.new, failed + report.failed
    opened_alerts = sum(seed_account(opened, settings, people, spec, now) for spec in ACCOUNTS)
    for extra in PIXELS:
        seed_pixel(opened, people, extra, now)
    opened.flush()
    for unpaired in UNPAIRED:
        seed_unpaired(opened, people, unpaired, now)
    seed_import(opened, photos, people, today, now)
    seed_download(opened, now)
    seed_failed_download(opened, now)
    seed_failed_download(opened, now, "casey", 4)
    seed_part_import(opened, now)
    events.record(opened, now - 3 * HOUR, transport.RECOVERED, transport.SUBJECT)
    seed_transport(opened, now)
    transport_status.review_unlocked(opened, settings, now - 2 * HOUR, alerts.recorded)
    opened.flush()
    held = len(opened.execute(select(Alert.id).where(Alert.kind == "pixel_held", Alert.closed.is_(None))).all())
    return Summary(len(PEOPLE), len(ACCOUNTS), sum(spec.pixel is not None for spec in ACCOUNTS) + len(PIXELS) + len(UNPAIRED), originals, indexed, failed,
                   opened_alerts + held)


def sign_in(opened: Session, settings: Settings, now: float) -> str:
    return oidc.demo_session(opened, settings, MEMBER, now)
