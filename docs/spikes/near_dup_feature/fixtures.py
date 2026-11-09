from __future__ import annotations

import io
import random
from collections.abc import Callable
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

SIZE = (640, 480)
PROCEDURAL_FAMILIES = 24
NATURAL_SAMPLES = ("astronaut", "camera", "chelsea", "coffee", "rocket", "coins", "clock", "brick")


@dataclass(frozen=True)
class Base:
    key: str
    family: str
    image: Image.Image


def gradient(rng: random.Random) -> Image.Image:
    top = tuple(rng.randrange(256) for _ in range(3))
    bottom = tuple(rng.randrange(256) for _ in range(3))
    image = Image.new("RGB", SIZE)
    draw = ImageDraw.Draw(image)
    for y in range(SIZE[1]):
        mix = y / (SIZE[1] - 1)
        draw.line([(0, y), (SIZE[0], y)], fill=tuple(round(a + (b - a) * mix) for a, b in zip(top, bottom, strict=True)))
    return image


def scatter(image: Image.Image, rng: random.Random) -> Image.Image:
    draw = ImageDraw.Draw(image)
    for _ in range(rng.randrange(7, 13)):
        x, y = rng.randrange(SIZE[0] - 80), rng.randrange(SIZE[1] - 80)
        w, h = rng.randrange(40, 200), rng.randrange(40, 180)
        colour = tuple(rng.randrange(256) for _ in range(3))
        shape = rng.choice(("ellipse", "rectangle", "polygon", "line"))
        if shape == "ellipse":
            draw.ellipse([x, y, x + w, y + h], fill=colour)
        elif shape == "rectangle":
            draw.rectangle([x, y, x + w, y + h], fill=colour)
        elif shape == "polygon":
            draw.polygon([(x, y + h), (x + w // 2, y), (x + w, y + h)], fill=colour)
        else:
            draw.line([(x, y), (x + w, y + h)], fill=colour, width=rng.randrange(3, 12))
    return image.filter(ImageFilter.GaussianBlur(0.8))


def procedural_family(seed: int) -> list[Base]:
    background = random.Random(seed)
    first = scatter(gradient(background), random.Random(seed * 2 + 1))
    second = scatter(gradient(random.Random(seed)), random.Random(seed * 2 + 2))
    return [Base(f"scene-{seed}-a", f"scene-{seed}", first), Base(f"scene-{seed}-b", f"scene-{seed}", second)]


def fitted(image: Image.Image) -> Image.Image:
    return image.convert("RGB").resize(SIZE, Image.Resampling.LANCZOS)


def natural_family(name: str) -> list[Base]:
    from skimage import data

    whole = Image.fromarray(getattr(data, name)()).convert("RGB")
    width, height = whole.size
    left, right = whole.crop((0, 0, width // 2, height)), whole.crop((width - width // 2, 0, width, height))
    return [Base(f"{name}-left", name, fitted(left)), Base(f"{name}-right", name, fitted(right))]


def bases(kind: str = "all") -> list[Base]:
    found = [base for seed in range(1, PROCEDURAL_FAMILIES + 1) for base in procedural_family(seed)] if kind in ("all", "procedural") else []
    if kind in ("all", "natural"):
        for name in NATURAL_SAMPLES:
            try:
                found.extend(natural_family(name))
            except (AttributeError, OSError, ValueError):
                continue
    return found


def jpeg(image: Image.Image, quality: int) -> Image.Image:
    out = io.BytesIO()
    image.save(out, "JPEG", quality=quality)
    return Image.open(io.BytesIO(out.getvalue())).convert("RGB")


def recompressed(image: Image.Image) -> Image.Image:
    small = image.resize((SIZE[0] * 6 // 10, SIZE[1] * 6 // 10), Image.Resampling.LANCZOS)
    return jpeg(small, 30).resize(SIZE, Image.Resampling.BICUBIC)


def thumbnail(image: Image.Image) -> Image.Image:
    return jpeg(image.resize((SIZE[0] // 2, SIZE[1] // 2), Image.Resampling.LANCZOS), 80)


def retouched(image: Image.Image) -> Image.Image:
    return ImageEnhance.Contrast(ImageEnhance.Brightness(image).enhance(1.35)).enhance(1.25)


def heavily_edited(image: Image.Image) -> Image.Image:
    edited = ImageEnhance.Color(image).enhance(1.9)
    edited = ImageEnhance.Contrast(edited).enhance(1.5)
    return ImageEnhance.Brightness(edited).enhance(0.8).filter(ImageFilter.GaussianBlur(1.4))


def grayscale(image: Image.Image) -> Image.Image:
    return image.convert("L").convert("RGB")


def tinted(image: Image.Image) -> Image.Image:
    grey = image.convert("L")
    return Image.merge("RGB", (grey.point(lambda v: min(255, v * 1.15)), grey.point(lambda v: v * 0.95), grey.point(lambda v: v * 0.75)))


def cropped(fraction: float) -> Callable[[Image.Image], Image.Image]:
    def crop(image: Image.Image) -> Image.Image:
        width, height = image.size
        w, h = round(width * fraction), round(height * fraction)
        left, top = (width - w) // 2, (height - h) // 2
        return image.crop((left, top, left + w, top + h)).resize(SIZE, Image.Resampling.LANCZOS)

    return crop


def rotated(image: Image.Image) -> Image.Image:
    return image.rotate(5, resample=Image.Resampling.BICUBIC, fillcolor=(0, 0, 0))


def watermarked(image: Image.Image) -> Image.Image:
    marked = image.copy()
    draw = ImageDraw.Draw(marked, "RGBA")
    draw.rectangle([SIZE[0] - 220, SIZE[1] - 56, SIZE[0] - 16, SIZE[1] - 16], fill=(0, 0, 0, 140))
    draw.text((SIZE[0] - 205, SIZE[1] - 44), "made-up studio", fill=(255, 255, 255, 230))
    return marked


def shifted(image: Image.Image) -> Image.Image:
    offset = SIZE[0] // 25
    moved = Image.new("RGB", SIZE, (0, 0, 0))
    moved.paste(image, (offset, 0))
    return moved


VARIANTS: dict[str, Callable[[Image.Image], Image.Image]] = {
    "recompressed": recompressed,
    "thumbnail": thumbnail,
    "retouched": retouched,
    "heavily-edited": heavily_edited,
    "grayscale": grayscale,
    "tinted": tinted,
    "crop-90": cropped(0.9),
    "crop-70": cropped(0.7),
    "rotated-5": rotated,
    "watermarked": watermarked,
    "shifted-4pct": shifted,
}
