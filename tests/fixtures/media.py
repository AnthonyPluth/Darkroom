from __future__ import annotations

import hashlib
import io
import random
import shutil
import struct
import subprocess
import zlib
from pathlib import Path

import pillow_heif
from PIL import Image, ImageChops, ImageDraw, ImageEnhance

pillow_heif.register_heif_opener()
TINY_VIDEO = Path(__file__).resolve().parent / "tiny.mp4"


TAGGED: dict[tuple[str, str, tuple[tuple[str, str], ...]], bytes] = {}


def set_tags(path: Path, tags: dict[str, str]) -> None:
    if not tags:
        return
    key = (path.suffix, hashlib.sha256(path.read_bytes()).hexdigest(), tuple(tags.items()))
    tagged = TAGGED.get(key)
    if tagged is not None:
        path.write_bytes(tagged)
        return
    arguments = [f"-{name}={value}" for name, value in tags.items()]
    subprocess.run(["exiftool", "-overwrite_original", "-q", *arguments, str(path)], check=True, capture_output=True)
    TAGGED[key] = path.read_bytes()


def exif_tags(taken: str | None, offset: str | None, gps: tuple[float, float] | None, make: str | None, model: str | None) -> dict[str, str]:
    tags: dict[str, str] = {}
    if taken:
        tags["DateTimeOriginal"] = taken
    if offset:
        tags["OffsetTimeOriginal"] = offset
    if gps:
        tags["GPSLatitude"] = str(abs(gps[0]))
        tags["GPSLatitudeRef"] = "N" if gps[0] >= 0 else "S"
        tags["GPSLongitude"] = str(abs(gps[1]))
        tags["GPSLongitudeRef"] = "E" if gps[1] >= 0 else "W"
    if make:
        tags["Make"] = make
    if model:
        tags["Model"] = model
    return tags


def make_jpeg(path: Path, taken: str | None = None, offset: str | None = None, gps: tuple[float, float] | None = None,
              make: str | None = None, model: str | None = None, size: tuple[int, int] = (64, 48), colour: tuple[int, int, int] = (200, 40, 40)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", size, colour)
    for x in range(0, size[0], 4):
        image.putpixel((x, x % size[1]), (255 - colour[0], 255 - colour[1], 255 - colour[2]))
    image.save(path, format="JPEG")
    set_tags(path, exif_tags(taken, offset, gps, make, model))
    return path


def make_heic(path: Path, taken: str | None = None, offset: str | None = None, gps: tuple[float, float] | None = None,
              make: str | None = None, model: str | None = None, size: tuple[int, int] = (64, 48)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (30, 60, 200)).save(path, format="HEIF")
    set_tags(path, exif_tags(taken, offset, gps, make, model))
    return path


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def pixel_bomb_png(width: int = 40_000, height: int = 40_000) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", header) + png_chunk(b"IDAT", zlib.compress(b"")) + png_chunk(b"IEND", b"")


def make_pixel_bomb(path: Path, width: int = 40_000, height: int = 40_000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pixel_bomb_png(width, height))
    return path


def make_video(path: Path, creation: str | None = None, quicktime_utc: str | None = None, gps: tuple[float, float] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(TINY_VIDEO, path)
    if creation:
        set_tags(path, {"QuickTime:CreateDate": creation, "Keys:CreationDate": creation})
    if quicktime_utc:
        set_tags(path, {"QuickTime:CreateDate": quicktime_utc})
    if gps:
        set_tags(path, {"UserData:GPSCoordinates": f"{gps[0]}, {gps[1]}"})
    return path


def scene(seed: int, size: tuple[int, int] = (320, 240)) -> Image.Image:
    chance = random.Random(seed)
    image = Image.new("RGB", size, (chance.randrange(256), chance.randrange(256), chance.randrange(256)))
    draw = ImageDraw.Draw(image)
    for _ in range(14):
        left, top = chance.randrange(size[0]), chance.randrange(size[1])
        box = (left, top, min(size[0], left + chance.randrange(30, 160)), min(size[1], top + chance.randrange(30, 120)))
        colour = (chance.randrange(256), chance.randrange(256), chance.randrange(256))
        (draw.ellipse if chance.random() < 0.5 else draw.rectangle)(box, fill=colour)
    return image


def recompressed(image: Image.Image, quality: int = 35) -> Image.Image:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    buffer.seek(0)
    return Image.open(buffer).convert("RGB")


def resized(image: Image.Image, factor: float = 0.5) -> Image.Image:
    return image.resize((round(image.width * factor), round(image.height * factor)))


def brightened(image: Image.Image, factor: float = 1.12) -> Image.Image:
    return ImageEnhance.Brightness(image).enhance(factor)


def shifted(image: Image.Image, across: int, down: int) -> Image.Image:
    return ImageChops.offset(image, across, down)


def save_jpeg(image: Image.Image, path: Path, quality: int = 92) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="JPEG", quality=quality)
    return path


PHONE_SIZES = ((4032, 3024), (3024, 4032), (4000, 3000), (2048, 1536), (1600, 1200), (1001, 667))


def photo(seed: int, size: tuple[int, int] = (4032, 3024)) -> Image.Image:
    chance = random.Random(seed)
    width, height = size
    image = Image.new("RGB", size, (chance.randrange(256), chance.randrange(256), chance.randrange(256)))
    for octave in range(1, 8):
        across = 2 ** octave
        down = max(1, across * height // width)
        layer = Image.frombytes("RGB", (across, down), chance.randbytes(across * down * 3)).resize(size, Image.Resampling.BICUBIC)
        image = Image.blend(image, layer, 1 / (octave + 1))
    draw = ImageDraw.Draw(image)
    for _ in range(chance.randrange(5, 40)):
        left, top = chance.randrange(width), chance.randrange(height)
        span = chance.randrange(8, max(9, width // 4))
        box = (left, top, min(width, left + span), min(height, top + chance.randrange(8, max(9, height // 4))))
        colour = (chance.randrange(256), chance.randrange(256), chance.randrange(256))
        (draw.ellipse if chance.random() < 0.5 else draw.rectangle)(box, fill=colour)
    grain = Image.frombytes("L", size, chance.randbytes(width * height)).convert("RGB")
    return Image.blend(image, grain, chance.uniform(0.02, 0.1))


def photo_jpeg(seed: int, path: Path, size: tuple[int, int] = (4032, 3024)) -> Path:
    return save_jpeg(photo(seed, size), path, quality=70 + seed % 26)
