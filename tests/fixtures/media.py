from __future__ import annotations

import io
import random
import shutil
import subprocess
from pathlib import Path

import pillow_heif
from PIL import Image, ImageChops, ImageDraw, ImageEnhance

pillow_heif.register_heif_opener()
TINY_VIDEO = Path(__file__).resolve().parent / "tiny.mp4"


def set_tags(path: Path, tags: dict[str, str]) -> None:
    if not tags:
        return
    arguments = [f"-{name}={value}" for name, value in tags.items()]
    subprocess.run(["exiftool", "-overwrite_original", "-q", *arguments, str(path)], check=True, capture_output=True)


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


def make_video(path: Path, creation: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(TINY_VIDEO, path)
    if creation:
        set_tags(path, {"QuickTime:CreateDate": creation, "Keys:CreationDate": creation})
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
