from __future__ import annotations

import contextlib
import hashlib
import io
from collections.abc import Callable
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from darkroom.domain import media
from darkroom.providers import exiftool
from darkroom.storage import paths
from darkroom.storage.paths import Photos

SIZE = 640
QUALITY = 82
PLACEHOLDER_SIZE = (480, 360)
PLACEHOLDER_COLOUR = (47, 43, 36)

Preview = Callable[[Path], bytes | None]


class NotAPhoto(LookupError):
    pass


def jpeg(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.convert("RGB").save(out, "JPEG", quality=QUALITY)
    return out.getvalue()


def placeholder() -> bytes:
    return jpeg(Image.new("RGB", PLACEHOLDER_SIZE, PLACEHOLDER_COLOUR))


def scaled(image: Image.Image) -> bytes:
    upright = ImageOps.exif_transpose(image)
    upright.thumbnail((SIZE, SIZE))
    return jpeg(upright)


def render(source: Path, preview: Preview) -> bytes:
    try:
        if source.suffix.lower() in media.VIDEO_SUFFIXES:
            data = preview(source)
            if not data:
                return placeholder()
            with Image.open(io.BytesIO(data)) as image:
                return scaled(image)
        with Image.open(source) as image:
            return scaled(image)
    except (UnidentifiedImageError, OSError, ValueError, exiftool.Missing, exiftool.Unreadable, Image.DecompressionBombError):
        return placeholder()


def checked(photos: Photos, source: Path) -> Path:
    if source.is_symlink() or not source.is_file():
        raise NotAPhoto("no such file")
    resolved = source.resolve()
    if not resolved.is_relative_to(photos.root) or resolved != source:
        raise NotAPhoto("the file is outside the photo folder")
    return resolved


def cache_name(photos: Photos, source: Path) -> str:
    stat = source.stat()
    key = f"{source.relative_to(photos.root)}:{stat.st_mtime_ns}:{stat.st_size}:{SIZE}"
    return f"{hashlib.sha256(key.encode()).hexdigest()[:40]}.jpg"


def thumbnail(photos: Photos, slug: str, source: Path, preview: Preview = exiftool.embedded_preview) -> bytes:
    found = checked(photos, source)
    cached = photos.thumbs(slug) / cache_name(photos, found)
    if cached.is_file() and not cached.is_symlink():
        return cached.read_bytes()
    data = render(found, preview)
    with contextlib.suppress(OSError):
        paths.write_once(cached, [data])
    return data
