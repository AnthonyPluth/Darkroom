import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "spikes" / "make_xmp_sample.py"
spec = importlib.util.spec_from_file_location("make_xmp_sample", SCRIPT)
make_xmp_sample = importlib.util.module_from_spec(spec)
spec.loader.exec_module(make_xmp_sample)


def read_tags(path):
    out = subprocess.run(
        ["exiftool", "-j", "-n", "-a", "-G1", str(path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(out)[0]


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    out_dir = tmp_path_factory.mktemp("xmp-samples")
    make_xmp_sample.build(out_dir)
    return out_dir


def test_writes_exactly_the_five_files(samples):
    assert sorted(p.name for p in samples.iterdir()) == [
        "sample-a.jpg",
        "sample-a.jpg.xmp",
        "sample-b.jpg",
        "sample-b.xmp",
        "sample-c.jpg",
    ]


@pytest.mark.parametrize("name", ["sample-a.jpg", "sample-b.jpg", "sample-c.jpg"])
def test_embedded_values_are_the_wrong_ones(samples, name):
    tags = read_tags(samples / name)
    assert tags["ExifIFD:DateTimeOriginal"] == "2001:01:01 01:01:01"
    assert tags["IFD0:ImageDescription"] == "WRONG embedded description"
    assert tags["GPS:GPSLatitude"] == pytest.approx(10.0)
    assert tags["GPS:GPSLongitude"] == pytest.approx(20.0)


@pytest.mark.parametrize("name", ["sample-a.jpg.xmp", "sample-b.xmp"])
def test_sidecar_values_are_the_right_ones(samples, name):
    tags = read_tags(samples / name)
    assert tags["XMP-exif:DateTimeOriginal"] == "2024:06:15 12:00:00"
    assert tags["XMP-dc:Description"] == "RIGHT sidecar description"
    assert tags["XMP-exif:GPSLatitude"] == pytest.approx(45.4340)
    assert tags["XMP-exif:GPSLongitude"] == pytest.approx(12.3388)


def test_control_has_no_sidecar(samples):
    assert not (samples / "sample-c.jpg.xmp").exists()
    assert not (samples / "sample-c.xmp").exists()


def test_embedded_jpeg_has_no_xmp_values(samples):
    tags = read_tags(samples / "sample-a.jpg")
    assert "XMP-dc:Description" not in tags
    assert "XMP-exif:DateTimeOriginal" not in tags


def test_rerun_overwrites_existing_output(samples):
    make_xmp_sample.build(samples)
    assert read_tags(samples / "sample-a.jpg.xmp")["XMP-dc:Description"] == "RIGHT sidecar description"
