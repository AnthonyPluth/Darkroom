from __future__ import annotations

import io
import random
import struct
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import pillow_heif
from PIL import ExifTags, Image


class FixtureFailed(Exception):
    pass


def apple_heic(content_id: str) -> bytes:
    identifier = (content_id + "\0").encode("ascii")
    apple_header = b"Apple iOS\0\0\x01MM"
    entry = struct.pack(">HHHIII", 1, 0x11, 2, len(identifier), len(apple_header) + 18, 0)
    exif = Image.Exif()
    exif[271] = "Apple"
    exif[ExifTags.IFD.Exif] = {37500: apple_header + entry + identifier}
    image = Image.new("RGB", (64, 48), tuple(random.Random(content_id).randbytes(3)))
    result = io.BytesIO()
    pillow_heif.from_pillow(image).save(result, exif=exif.tobytes())
    return result.getvalue()


def video(encoder: str, seed: str, suffix: str, content_id: str | None = None) -> bytes:
    with TemporaryDirectory(prefix="darkroom-live-photo-") as folder:
        output = Path(folder) / f"fixture.{suffix}"
        command = [encoder, "-hide_banner", "-loglevel", "error", "-nostdin", "-n", "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=5",
                   "-t", "1", "-an", "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", "-metadata", f"title={seed}"]
        if content_id:
            command.extend(("-metadata", f"com.apple.quicktime.content.identifier={content_id}"))
        try:
            subprocess.run([*command, "-movflags", "use_metadata_tags", str(output)], check=True, capture_output=True, timeout=30)
            data = output.read_bytes()
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            raise FixtureFailed("FFmpeg could not generate a short fixture with libx264") from None
        if not data:
            raise FixtureFailed("FFmpeg produced an empty fixture")
        return data


def motion_jpeg(still: bytes, embedded_video: bytes) -> bytes:
    motion_xmp = (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description xmlns:GCamera="http://ns.google.com/photos/1.0/camera/" GCamera:MicroVideo="1" '
        f'GCamera:MicroVideoVersion="1" GCamera:MicroVideoOffset="{len(embedded_video)}" GCamera:MicroVideoPresentationTimestampUs="0"/>'
        '</rdf:RDF></x:xmpmeta>'
    ).encode()
    result = io.BytesIO()
    with Image.open(io.BytesIO(still)) as image:
        image.save(result, "JPEG", xmp=motion_xmp)
    return result.getvalue() + embedded_video
