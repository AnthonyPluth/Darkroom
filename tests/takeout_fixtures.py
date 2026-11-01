from __future__ import annotations

import json
import random
import zipfile
from dataclasses import dataclass, field
from pathlib import Path


from tests.fixtures import media as fixtures

GOOGLE_PHOTOS = "Takeout/Google Photos"
SIDECAR_NAME_LIMIT = 46
FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)
SIDECAR_FORMS = {
    "json": ".json",
    "supplemental": ".supplemental-metadata.json",
    "truncated": ".supplemental-metada.json",
    "short": ".supplemental-me.json",
    "tiny": ".s.json",
}


@dataclass
class Item:
    media_name: str
    folder: str
    part: int
    data: bytes
    sidecar_name: str | None
    sidecar_part: int
    sidecar_data: bytes | None


@dataclass
class Manifest:
    parts: list[Path] = field(default_factory=list)
    media: list[str] = field(default_factory=list)
    pairings: dict[str, str | None] = field(default_factory=dict)
    orphan_sidecars: list[str] = field(default_factory=list)
    non_media: list[str] = field(default_factory=list)
    albums: dict[str, list[str]] = field(default_factory=dict)
    edited_of: dict[str, str] = field(default_factory=dict)
    live_pairs: dict[str, str] = field(default_factory=dict)


class TakeoutBuilder:
    def __init__(self, root: Path, seed: int = 0, stamp: str = "20260101T000000Z", parts: int = 1) -> None:
        self.root = root
        self.random = random.Random(seed)
        self.stamp = stamp
        self.part_count = parts
        self.items: list[Item] = []
        self.extra_files: list[tuple[int, str, bytes]] = []
        self.manifest = Manifest()
        self.counter = 0
        self.zero_byte_part: int | None = None
        self.corrupt_part: int | None = None

    def sidecar_for(self, media_name: str, form: str) -> str:
        if form == "limit":
            return f"{media_name[:SIDECAR_NAME_LIMIT]}.json"
        return f"{media_name}{SIDECAR_FORMS[form]}"

    def sidecar_json(self, title: str, taken: int, gps: tuple[float, float] | None, description: str) -> bytes:
        stamp = {"timestamp": str(taken), "formatted": f"taken {taken}"}
        geo = {"latitude": gps[0], "longitude": gps[1], "altitude": 0.0, "latitudeSpan": 0.0, "longitudeSpan": 0.0} if gps else \
            {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0, "latitudeSpan": 0.0, "longitudeSpan": 0.0}
        return json.dumps({"title": title, "description": description, "imageViews": "0", "creationTime": stamp, "photoTakenTime": stamp,
                           "geoData": geo, "geoDataExif": geo, "url": "https://photos.google.com/photo/example",
                           "googlePhotosOrigin": {"mobileUpload": {"deviceType": "IOS_PHONE"}}}, sort_keys=True).encode()

    def image_bytes(self, suffix: str) -> bytes:
        colour = (self.random.randrange(256), self.random.randrange(256), self.random.randrange(256))
        path = self.root / f"_scratch{self.counter}{suffix}"
        self.counter += 1
        if suffix.lower() in (".heic", ".heif"):
            fixtures.make_heic(path, size=(32, 24))
        else:
            fixtures.make_jpeg(path, size=(32, 24), colour=colour)
        data = path.read_bytes()
        path.unlink()
        return data

    def video_bytes(self) -> bytes:
        return fixtures.TINY_VIDEO.read_bytes() + bytes([self.random.randrange(256)])

    def add(self, media_name: str, data: bytes, folder: str, part: int, form: str | None, sidecar_part: int | None,
            gps: tuple[float, float] | None, description: str, sidecar_name: str | None = None) -> Item:
        taken = 1_500_000_000 + len(self.items) * 3600
        name = sidecar_name or (self.sidecar_for(media_name, form) if form else None)
        payload = self.sidecar_json(media_name, taken, gps, description) if name else None
        item = Item(media_name, folder, part, data, name, part if sidecar_part is None else sidecar_part, payload)
        self.items.append(item)
        return item

    def photo(self, name: str, year: int = 2019, form: str | None = "json", part: int = 0, sidecar_part: int | None = None,
              album: str | None = None, gps: tuple[float, float] | None = (40.7, -74.0), description: str = "", data: bytes | None = None) -> Item:
        payload = data if data is not None else self.image_bytes(Path(name).suffix)
        item = self.add(name, payload, f"Photos from {year}", part, form, sidecar_part, gps, description)
        if album:
            self.add(name, payload, album, part, form, sidecar_part, gps, description)
        return item

    def numbered_duplicate(self, base: str, extension: str, year: int = 2019, part: int = 0) -> tuple[Item, Item]:
        first = self.photo(f"{base}{extension}", year, part=part)
        second = self.add(f"{base}(1){extension}", self.image_bytes(extension), f"Photos from {year}", part, None, None, None, "",
                          sidecar_name=f"{base}{extension}(1).json")
        return first, second

    def edited(self, base: str, extension: str, year: int = 2019, part: int = 0) -> tuple[Item, Item]:
        original = self.photo(f"{base}{extension}", year, part=part)
        edit = self.add(f"{base}-edited{extension}", self.image_bytes(extension), f"Photos from {year}", part, None, None, None, "")
        self.manifest.edited_of[edit.media_name] = original.media_name
        return original, edit

    def live_photo(self, base: str, still_extension: str = ".HEIC", video_extension: str = ".MOV", year: int = 2019, part: int = 0,
                   video_sidecar: bool = True) -> tuple[Item, Item]:
        still = self.photo(f"{base}{still_extension}", year, part=part)
        video = self.add(f"{base}{video_extension}", self.video_bytes(), f"Photos from {year}", part, "json" if video_sidecar else None, None, None, "")
        self.manifest.live_pairs[video.media_name] = still.media_name
        return still, video

    def orphan_sidecar(self, media_name: str, year: int = 2019, part: int = 0) -> None:
        name = f"{GOOGLE_PHOTOS}/Photos from {year}/{media_name}.json"
        self.extra_files.append((part, name, self.sidecar_json(media_name, 1_400_000_000, None, "")))
        self.manifest.orphan_sidecars.append(name)

    def non_media(self, part: int = 0) -> None:
        for name, data in (("Takeout/archive_browser.html", b"<html>archive</html>"),
                           (f"{GOOGLE_PHOTOS}/print-subscriptions.json", b"[]"),
                           (f"{GOOGLE_PHOTOS}/user-generated-memory-titles.json", b"{}")):
            self.extra_files.append((part, name, data))
            self.manifest.non_media.append(name)

    def album_metadata(self, album: str) -> tuple[str, bytes]:
        return f"{GOOGLE_PHOTOS}/{album}/metadata.json", json.dumps({"title": album, "description": "", "access": "", "date": {"timestamp": "1500000000"}}).encode()

    def entries(self) -> list[list[tuple[str, bytes]]]:
        parts: list[list[tuple[str, bytes]]] = [[] for _ in range(self.part_count)]
        albums: dict[str, list[str]] = {}
        for item in self.items:
            media_path = f"{GOOGLE_PHOTOS}/{item.folder}/{item.media_name}"
            parts[item.part].append((media_path, item.data))
            sidecar_path = None
            if item.sidecar_name and item.sidecar_data is not None:
                sidecar_path = f"{GOOGLE_PHOTOS}/{item.folder}/{item.sidecar_name}"
                parts[item.sidecar_part].append((sidecar_path, item.sidecar_data))
            self.manifest.media.append(media_path)
            self.manifest.pairings[media_path] = sidecar_path
            if not item.folder.startswith("Photos from "):
                albums.setdefault(item.folder, []).append(media_path)
        for album in sorted(albums):
            parts[0].append(self.album_metadata(album))
        self.manifest.albums = albums
        for part, name, data in self.extra_files:
            parts[part].append((name, data))
        return parts

    def build(self) -> Manifest:
        self.root.mkdir(parents=True, exist_ok=True)
        for number, entries in enumerate(self.entries(), start=1):
            path = self.root / f"takeout-{self.stamp}-{number:03d}.zip"
            if self.zero_byte_part == number - 1:
                path.write_bytes(b"")
            else:
                with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
                    for name, data in sorted(entries):
                        archive.writestr(zipfile.ZipInfo(name, FIXED_ZIP_TIME), data)
                if self.corrupt_part == number - 1:
                    raw = bytearray(path.read_bytes())
                    middle = len(raw) // 3
                    raw[middle:middle + 16] = bytes(16)
                    path.write_bytes(bytes(raw))
            self.manifest.parts.append(path)
        return self.manifest


def standard_export(root: Path, seed: int = 0, parts: int = 2) -> Manifest:
    builder = TakeoutBuilder(root, seed=seed, parts=parts)
    builder.photo("IMG_0001.JPG", form="json")
    builder.photo("IMG_0002.JPG", form="supplemental", part=0, sidecar_part=min(1, parts - 1))
    builder.photo("IMG_0003.JPG", form="truncated")
    builder.photo("IMG_0004.JPG", form="short")
    builder.photo("IMG_0005.JPG", form="tiny")
    builder.photo("IMG_0006.JPG", form=None)
    builder.photo("a_very_long_original_file_name_from_a_camera_roll_0007.JPG", form="limit")
    builder.numbered_duplicate("IMG_0008", ".JPG")
    builder.edited("IMG_0009", ".JPG")
    builder.live_photo("IMG_0010")
    builder.live_photo("IMG_0011", still_extension=".JPG", video_extension=".MP4", video_sidecar=False)
    builder.photo("IMG_0012.JPG", album="Family trip")
    builder.orphan_sidecar("IMG_9999.JPG")
    builder.non_media()
    return builder.build()
