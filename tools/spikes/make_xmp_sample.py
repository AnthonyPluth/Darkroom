import argparse
import subprocess
from pathlib import Path

from PIL import Image

WRONG = {
    "date": "2001:01:01 01:01:01",
    "lat": 10.0,
    "lon": 20.0,
    "description": "WRONG embedded description",
}
RIGHT = {
    "date": "2024:06:15 12:00:00",
    "lat": 45.4340,
    "lon": 12.3388,
    "description": "RIGHT sidecar description",
}


def run_exiftool(*args):
    subprocess.run(["exiftool", "-overwrite_original", "-q", *map(str, args)], check=True)


def exif_args(values):
    return [
        f"-EXIF:DateTimeOriginal={values['date']}",
        f"-EXIF:GPSLatitude={abs(values['lat'])}",
        f"-EXIF:GPSLatitudeRef={'N' if values['lat'] >= 0 else 'S'}",
        f"-EXIF:GPSLongitude={abs(values['lon'])}",
        f"-EXIF:GPSLongitudeRef={'E' if values['lon'] >= 0 else 'W'}",
        f"-EXIF:ImageDescription={values['description']}",
    ]


def xmp_args(values):
    return [
        f"-XMP-exif:DateTimeOriginal={values['date']}",
        f"-XMP-exif:GPSLatitude={values['lat']}",
        f"-XMP-exif:GPSLongitude={values['lon']}",
        f"-XMP-dc:Description={values['description']}",
    ]


def make_jpeg(path, color):
    Image.new("RGB", (64, 64), color).save(path, "JPEG")


def make_sidecar(sidecar_path, values):
    sidecar_path.unlink(missing_ok=True)
    subprocess.run(
        ["exiftool", "-q", "-o", str(sidecar_path), *xmp_args(values)],
        check=True,
    )


def build(out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    samples = {
        "sample-a.jpg": (out_dir / "sample-a.jpg.xmp", (200, 40, 40)),
        "sample-b.jpg": (out_dir / "sample-b.xmp", (40, 200, 40)),
        "sample-c.jpg": (None, (40, 40, 200)),
    }
    written = []
    for name, (sidecar, color) in samples.items():
        image = out_dir / name
        make_jpeg(image, color)
        run_exiftool(*exif_args(WRONG), image)
        written.append(image)
        if sidecar is not None:
            make_sidecar(sidecar, RIGHT)
            written.append(sidecar)
    return written


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("out_dir", type=Path)
    for path in build(parser.parse_args().out_dir):
        print(path)


if __name__ == "__main__":
    main()
