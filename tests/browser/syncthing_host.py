import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from xml.etree import ElementTree

MARKER = ".stfolder"
CHANGED = 3


def kind(mode):
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISLNK(mode):
        return "link"
    return "other"


def file_sha1(path):
    digest = hashlib.sha1(usedforsecurity=False)
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def entry(path):
    found = os.lstat(path)
    shown = {"type": kind(found.st_mode), "mode": oct(stat.S_IMODE(found.st_mode)), "size": found.st_size if not stat.S_ISDIR(found.st_mode) else 0}
    if stat.S_ISREG(found.st_mode):
        shown |= {"mtime_ns": found.st_mtime_ns, "sha1": file_sha1(path)}
    elif stat.S_ISLNK(found.st_mode):
        shown |= {"mtime_ns": found.st_mtime_ns, "target": os.readlink(path)}
    return shown


def snapshot(root):
    root = Path(root)
    tree = {}
    for folder, folders, files in os.walk(root):
        folders[:] = sorted(name for name in folders if name != MARKER)
        for name in [*folders, *sorted(files)]:
            path = Path(folder) / name
            tree[path.relative_to(root).as_posix()] = entry(path)
    return tree


def digest(tree):
    lines = "".join(f"{path}\0{json.dumps(tree[path], sort_keys=True)}\n" for path in sorted(tree))
    return hashlib.sha1(lines.encode(), usedforsecurity=False).hexdigest()


def allowed_with_parents(allowed):
    found = set()
    for path in allowed:
        parts = Path(path).parts
        found.update(Path(*parts[:end]).as_posix() for end in range(1, len(parts) + 1))
    return found


def changed(before, after, allowed=()):
    excused = allowed_with_parents(allowed)
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path) and path not in excused)


def copies_changed(before, after):
    earlier = {row["id"]: row for row in before}
    later = {row["id"]: row for row in after}
    paths = set()
    for copy_id in set(earlier) | set(later):
        if earlier.get(copy_id) != later.get(copy_id):
            paths.update(row["path"] for row in (earlier.get(copy_id), later.get(copy_id)) if row is not None and row["path"] is not None)
    return sorted(paths)


def contents(root):
    return {path: shown["sha1"] for path, shown in snapshot(root).items() if shown["type"] == "file" and not Path(path).name.startswith((".st", ".syncthing"))}


def retype(config, identity, kind_wanted):
    tree = ElementTree.parse(config)
    found = [folder for folder in tree.getroot().iter("folder") if folder.get("id") == identity]
    if len(found) != 1:
        raise SystemExit(f"expected one folder {identity} in the configuration, found {len(found)}")
    found[0].set("type", kind_wanted)
    tree.write(config, encoding="utf-8", xml_declaration=False)


def read_json(path):
    return json.loads(Path(path).read_text())


def compare(before_path, after_path, copies_before=None, copies_after=None):
    before, after = read_json(before_path), read_json(after_path)
    allowed = copies_changed(read_json(copies_before), read_json(copies_after)) if copies_before and copies_after else []
    print(f"whole-tree SHA-1 before {digest(before)} after {digest(after)}; {len(before)} entries before, {len(after)} after; "
          f"{len(allowed)} paths Darkroom placed or pruned in between")
    differing = changed(before, after, allowed)
    for path in differing:
        print(f"changed: {path}: {before.get(path)} -> {after.get(path)}", file=sys.stderr)
    return CHANGED if differing else 0


def main(command, *args):
    if command == "snapshot":
        print(json.dumps(snapshot(args[0]), sort_keys=True))
        return 0
    if command == "compare":
        return compare(*args)
    if command == "matches":
        return 0 if contents(args[0]) == contents(args[1]) else 1
    if command == "retype":
        retype(*args)
        return 0
    raise SystemExit(f"unknown command {command}")


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
