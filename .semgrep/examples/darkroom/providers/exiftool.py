import subprocess


def rewrite_in_place(program, path):
    # ruleid: darkroom-xmp-only-sidecars
    subprocess.run([program, "-overwrite_original", "-XMP-dc:Description=x", str(path)], check=True)


def keep_a_backup(program, path):
    # ruleid: darkroom-xmp-only-sidecars
    subprocess.run([program, "-EXIF:GPSLatitude=1", str(path)], check=True)


def write_a_named_tag(program, path, name, value):
    # ruleid: darkroom-xmp-only-sidecars
    subprocess.run([program, f"-{name}={value}", str(path)], check=True)


def copy_tags(program, source, path):
    # ruleid: darkroom-xmp-only-sidecars
    subprocess.run([program, "-tagsFromFile", str(source), str(path)], check=True)


def write_out(program, path, out):
    # ruleid: darkroom-xmp-only-sidecars
    subprocess.run([program, "-o", str(out), str(path)], check=True)


def in_place_too(program, path):
    # ruleid: darkroom-xmp-only-sidecars
    flags = ["-overwrite_original_in_place"]
    return subprocess.run([program, *flags, str(path)], check=True)


def read(program, paths):
    # ok: darkroom-xmp-only-sidecars
    return subprocess.run([program, "-j", "-n", "-G1", "-api", "largefilesupport=1", "-fast2", *map(str, paths)], capture_output=True)


def write_xmp(program, sidecar, fields):
    assignments = [f"-{name}={value}" for name, value in fields.items()]
    # ok: darkroom-xmp-only-sidecars
    return subprocess.run([program, "-q", "-n", "-o", str(sidecar), *assignments], check=True)
