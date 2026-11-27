import ast
import gc
import hashlib
import io
import os
import shutil
import stat
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path, PurePosixPath
from unittest import mock

from sqlalchemy.orm import Session

from darkroom.domain.ingest import part_reader, run, unpack
from darkroom.storage import db, paths
from darkroom.storage.models import Event, Export, ExportPart, GoogleAccount, Person, TakeoutEntry, TakeoutSidecar
from darkroom.storage.paths import Photos
from tests.privacy import no_leaks
from tests.shared import add_person, own_database
from tests.takeout_fixtures import TakeoutBuilder, corrupt_compressed_stream, standard_export

PART = "takeout-20260101T000000Z-001.zip"
ROOT = Path(__file__).resolve().parents[1]
COMPRESSIONS = (("deflate", zipfile.ZIP_DEFLATED, 0), ("bzip2", zipfile.ZIP_BZIP2, 0), ("lzma", zipfile.ZIP_LZMA, 9))
MEDIA = "Takeout/Google Photos/Photos from 2019/IMG_0001.JPG"
SIDECAR = f"{MEDIA}.json"
DAMAGED = "part 1 has a damaged file inside"


def reached_through_the_reader(text):
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any("part_reader" in alias.name for alias in node.names):
            raise AssertionError("part_reader is imported by its full name")
        if isinstance(node, ast.ImportFrom) and "part_reader" in (node.module or ""):
            raise AssertionError("names are imported from part_reader")
        if isinstance(node, ast.ImportFrom) and any(alias.name == "part_reader" and alias.asname for alias in node.names):
            raise AssertionError("part_reader is imported under another name")
    attributes = [node for node in ast.walk(tree) if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "part_reader"]
    if sum(1 for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id == "part_reader") != len(attributes):
        raise AssertionError("part_reader is used other than through one of its names")
    return {node.attr for node in attributes}


class Crash(Exception):
    pass


def sha1_of(data):
    return hashlib.sha1(data, usedforsecurity=False).hexdigest()


def zip_entries(path):
    with zipfile.ZipFile(path) as archive:
        return [(info, archive.read(info) if not info.is_dir() else b"") for info in archive.infolist()]


def normalised(info):
    name = str(PurePosixPath(info.filename.replace("\\", "/")))
    return f"{name}/" if info.is_dir() else name


def write_zip(path, entries, compression=zipfile.ZIP_STORED):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(path, "w", compression) as archive:
            for entry, data in entries:
                archive.writestr(entry, data)


def corrupt_zip(path, compression, keep, entries):
    write_zip(path, entries, compression)
    corrupt_compressed_stream(path, entries[0][0], keep)
    return path


def special(name, mode):
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = mode << 16
    return info


def tree_of(root):
    files, folders = {}, set()
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink() or path.is_file():
            files[relative] = path.read_bytes() if path.is_file() and not path.is_symlink() else os.readlink(path)
        elif path.is_dir():
            folders.add(relative)
    return files, folders


class World:
    def __init__(self, case):
        folder = tempfile.TemporaryDirectory()
        case.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.photos = Photos(self.root / "photos")
        self.engine = own_database(case)
        with db.session(self.engine) as opened:
            person_id = add_person(opened, "jo")
            opened.add(GoogleAccount(email="jo@example.com", default_person_id=person_id, created=1.0))
        self.inbox = self.photos.manual_inbox("jo")
        self.inbox.mkdir(parents=True)

    def register(self):
        with db.session(self.engine) as opened:
            unpack.inspect_manual_inbox(opened, self.photos, opened.query(Person).one(), 5.0)

    def read(self, number=1, **kwargs):
        with db.session(self.engine) as opened:
            export = opened.query(Export).one()
            part = opened.query(ExportPart).filter_by(export_id=export.id, number=number).one()
            return part_reader.read_part(opened, self.photos, opened.query(Person).one(), export, part, 10.0, **kwargs)

    def refusal(self, number=1, **kwargs):
        try:
            self.read(number, **kwargs)
        except part_reader.PartRefused as refused:
            return refused
        raise AssertionError("the part was not refused")

    def ledger(self):
        with db.session(self.engine) as opened:
            entries = sorted((e.part_number, e.name, e.kind, e.bytes, e.crc32, e.sha1, e.outcome) for e in opened.query(TakeoutEntry))
            sidecars = sorted((s.part_number, s.folder, s.name, s.text) for s in opened.query(TakeoutSidecar))
            counts = sorted((p.number, p.entries) for p in opened.query(ExportPart))
            return entries, sidecars, counts

    def scratch(self):
        with db.session(self.engine) as opened:
            return self.photos.scratch("jo", unpack.staging_name(opened.query(Export).one()))

    def unpack_public(self):
        with db.session(self.engine) as opened:
            export = opened.query(Export).one()
            report = unpack.unpack(opened, self.photos, opened.query(Person).one(), export, 20.0)
            return report.public


class ListingTests(unittest.TestCase):
    def setUp(self):
        self.world = World(self)

    def test_lists_a_builder_export_part_by_part(self):
        manifest = standard_export(self.world.inbox, parts=2)
        self.world.register()
        reads = [self.world.read(1), self.world.read(2)]
        entries, sidecars, counts = self.world.ledger()
        expected_entries, expected_sidecars = [], []
        for number, path in enumerate(manifest.parts, start=1):
            for info, data in zip_entries(path):
                name = normalised(info)
                kind = "media" if name in manifest.media else "sidecar" if name in set(manifest.pairings.values()) | set(manifest.orphan_sidecars) else "other"
                expected_entries.append((number, name, kind, info.file_size, info.CRC, sha1_of(data) if kind == "media" else None, "listed"))
                if kind == "sidecar":
                    folder, leaf = name.rsplit("/", 1)
                    expected_sidecars.append((number, folder, leaf, data.decode()))
        self.assertEqual(entries, sorted(expected_entries))
        self.assertEqual(sidecars, sorted(expected_sidecars))
        self.assertEqual(counts, [(1, len(zip_entries(manifest.parts[0]))), (2, len(zip_entries(manifest.parts[1])))])
        self.assertTrue(any(kind == "other" for _, _, kind, *_ in entries))
        self.assertEqual(sum(r.media for r in reads), len(manifest.media))
        self.assertEqual(sum(r.streamed for r in reads), len(manifest.media))
        self.assertFalse(any(r.relisted for r in reads))
        self.assertFalse(self.world.scratch().exists())

    def test_the_sha1_of_the_streamed_bytes_is_the_sha1_of_the_file(self):
        manifest = standard_export(self.world.inbox, parts=1)
        self.world.register()
        seen = {}

        def check(opened, batch):
            for streamed in batch:
                self.assertTrue(streamed.path.is_file())
                seen[streamed.name] = (streamed.sha1, paths.file_sha1(streamed.path), streamed.path.stat().st_size, streamed.bytes)

        self.world.read(each_batch=check)
        from_zip = {normalised(info): sha1_of(data) for info, data in zip_entries(manifest.parts[0])}
        self.assertEqual(sorted(seen), sorted(manifest.media))
        for name, (streamed, on_disk, size, declared) in seen.items():
            self.assertEqual(streamed, on_disk)
            self.assertEqual(streamed, from_zip[name])
            self.assertEqual(size, declared)

    def test_a_part_with_no_media_lists_and_streams_nothing(self):
        write_zip(self.world.inbox / PART, [("Takeout/archive_browser.html", b"<html></html>"),
                                             ("Takeout/Google Photos/Photos from 2019/IMG_1.JPG.json", b'{"title": "a"}')])
        self.world.register()
        called = []
        read = self.world.read(each_batch=lambda opened, batch: called.append(batch))
        self.assertEqual((read.entries, read.media, read.sidecars, read.other, read.streamed, read.batches), (2, 0, 1, 1, 0, 0))
        self.assertEqual(called, [])
        self.assertFalse(self.world.scratch().parent.exists())

    def test_folders_and_dot_folders_are_other_entries(self):
        write_zip(self.world.inbox / PART, [("Takeout/Google Photos/Album/", b""), ("Takeout/.hidden/IMG_1.JPG", b"x"),
                                             ("Takeout/.hidden/IMG_1.JPG.json", b"{}"), ("Takeout/Google Photos/metadata.json", b"{}")])
        self.world.register()
        read = self.world.read()
        entries, sidecars, _ = self.world.ledger()
        self.assertEqual([(name, kind) for _, name, kind, *_ in entries],
                         [("Takeout/.hidden/IMG_1.JPG", "other"), ("Takeout/.hidden/IMG_1.JPG.json", "other"),
                          ("Takeout/Google Photos/Album/", "other"), ("Takeout/Google Photos/metadata.json", "other")])
        self.assertEqual((read.media, read.sidecars, sidecars), (0, 0, []))

    def test_a_sidecar_too_big_or_not_clean_text_is_listed_without_its_text(self):
        folder = "Takeout/Google Photos/Photos from 2019"
        write_zip(self.world.inbox / PART, [(f"{folder}/a.jpg.json", b"{" + b" " * part_reader.SIDECAR_MAX_BYTES + b"}"),
                                             (f"{folder}/b.jpg.json", b'{"title": "\xff"}'), (f"{folder}/c.jpg.json", b'{"title": "\x00"}'),
                                             (f"{folder}/d.jpg.json", '{"title": "café"}'.encode())])
        self.world.register()
        read = self.world.read()
        entries, sidecars, _ = self.world.ledger()
        self.assertEqual([kind for _, _, kind, *_ in entries], ["sidecar"] * 4)
        self.assertEqual(sidecars, [(1, folder, "d.jpg.json", '{"title": "café"}')])
        self.assertEqual((read.sidecars, read.sidecars_unread), (4, 3))

    def test_the_batch_size_is_the_existing_pinned_batch(self):
        self.assertEqual(part_reader.BATCH_FILES, run.PINNED_AT_ONCE)

    def test_only_part_ingest_calls_the_reader(self):
        sources = {str(p.relative_to(ROOT)): p.read_text() for p in sorted((ROOT / "darkroom").rglob("*.py")) if p.name != "part_reader.py"}
        users = [name for name, text in sources.items() if "part_reader" in text]
        self.assertEqual(users, ["darkroom/domain/ingest/part_free.py", "darkroom/domain/ingest/part_ingest.py"])
        reached = {name: reached_through_the_reader(sources[name]) for name in users}
        self.assertEqual(reached, {
            "darkroom/domain/ingest/part_free.py": {"PartRefused", "entry_chunks", "entry_listing", "recorded", "scratch_name", "zip_of"},
            "darkroom/domain/ingest/part_ingest.py": {"BATCH_BYTES", "BATCH_FILES", "PartRefused", "READ_ERRORS", "Streamed", "batches_of", "grouped_batches_of",
                                                      "entries_by_id", "entry_chunks", "entry_listing", "read_part", "stream_one", "sweep_part_scratch", "zip_of"},
        })

    def test_the_boundary_check_sees_every_way_a_module_can_reach_the_reader(self):
        self.assertEqual(reached_through_the_reader("from darkroom.domain.ingest import part_reader\npart_reader.stream_media(1)\n"), {"stream_media"})
        for text in ("from darkroom.domain.ingest.part_reader import stream_media\n", "from darkroom.domain.ingest import part_reader as reader\n",
                     "import darkroom.domain.ingest.part_reader\n", "from darkroom.domain.ingest import part_reader\nuse(part_reader)\n"):
            with self.subTest(text=text), self.assertRaises(AssertionError):
                reached_through_the_reader(text)


class SameRefusalsAsUnpackTests(unittest.TestCase):
    def assert_same_refusal(self, entries=None, raw=None, after_register=None, reason=None):
        world = World(self)
        if raw is not None:
            (world.inbox / PART).write_bytes(raw)
        else:
            write_zip(world.inbox / PART, entries)
        world.register()
        if after_register:
            after_register(world)
        before = world.ledger()
        refused = world.refusal()
        self.assertEqual(world.ledger(), before)
        self.assertFalse(world.scratch().parent.exists())
        self.assertEqual(refused.reason, reason)
        self.assertEqual([refused.public], world.unpack_public())
        return refused

    def test_a_traversal_entry(self):
        self.assert_same_refusal([("Takeout/ok.txt", b"fine"), ("../escape.txt", b"bad")], reason="outside")

    def test_an_absolute_entry(self):
        self.assert_same_refusal([("/tmp/darkroom-absolute.txt", b"bad")], reason="outside")

    def test_a_backslash_traversal_entry(self):
        self.assert_same_refusal([("Takeout\\..\\..\\escape.txt", b"bad")], reason="outside")

    def test_a_symbolic_link_entry(self):
        self.assert_same_refusal([("Takeout/a.txt", b"a"), (special("Takeout/link", stat.S_IFLNK | 0o777), "/etc/passwd")], reason="symlink_entry")

    def test_a_file_then_a_folder_of_the_same_name(self):
        self.assert_same_refusal([("Takeout/a", b"file"), ("Takeout/a/b.txt", b"inside")], reason="file_and_folder")

    def test_a_folder_then_a_file_of_the_same_name(self):
        self.assert_same_refusal([("Takeout/a/b.txt", b"inside"), ("Takeout/a", b"file")], reason="file_and_folder")

    def test_a_folder_entry_where_a_file_is(self):
        self.assert_same_refusal([("Takeout/a", b"file"), ("Takeout/a/", b"")], reason="file_and_folder")

    def test_an_empty_part(self):
        self.assert_same_refusal(raw=b"", reason="empty")

    def test_not_a_zip(self):
        self.assert_same_refusal(raw=b"not a zip at all", reason="not_a_zip")

    def test_a_damaged_part(self):
        world = World(self)
        builder = TakeoutBuilder(world.inbox)
        builder.photo("IMG_1.JPG")
        builder.corrupt_part = 0
        builder.build()
        raw = (world.inbox / PART).read_bytes()
        self.assert_same_refusal(raw=raw, reason="damaged")

    def test_a_missing_part(self):
        self.assert_same_refusal([("Takeout/a.txt", b"a")], after_register=lambda world: (world.inbox / PART).unlink(), reason="missing")

    def test_a_part_that_is_a_link(self):
        def swap(world):
            elsewhere = world.root / "elsewhere.zip"
            shutil.move(world.inbox / PART, elsewhere)
            os.symlink(elsewhere, world.inbox / PART)

        self.assert_same_refusal([("Takeout/a.txt", b"a")], after_register=swap, reason="not_plain")

    def test_a_damaged_compressed_stream(self):
        for label, compression, keep in COMPRESSIONS:
            with self.subTest(compression=label):
                folder = Path(tempfile.mkdtemp())
                self.addCleanup(shutil.rmtree, folder)
                raw = corrupt_zip(folder / "part.zip", compression, keep, [(MEDIA, bytes(range(256)) * 200)]).read_bytes()
                refused = self.assert_same_refusal(raw=raw, reason="damaged")
                self.assertEqual(refused.public, DAMAGED)

    def test_a_damaged_part_wins_over_a_bad_entry_as_in_unpack(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("../escape.txt", b"x" * 64)
        raw = bytearray(buffer.getvalue())
        start = raw.index(b"x" * 64)
        raw[start:start + 8] = b"y" * 8
        self.assert_same_refusal(raw=bytes(raw), reason="damaged")


class HostileZipTests(unittest.TestCase):
    def setUp(self):
        self.world = World(self)

    def refused(self, entries, compression=zipfile.ZIP_STORED, **kwargs):
        write_zip(self.world.inbox / PART, entries, compression)
        self.world.register()
        before = tree_of(self.world.photos.root), self.world.ledger()
        refused = self.world.refusal(**kwargs)
        self.assertEqual((tree_of(self.world.photos.root), self.world.ledger()), before)
        return refused

    def test_a_zip_bomb_is_refused_without_being_decompressed(self):
        with mock.patch.object(zipfile.ZipFile, "testzip", side_effect=AssertionError("decompressed")), \
                mock.patch.object(part_reader, "RATIO_FLOOR", 64 * 1024):
            refused = self.refused([("Takeout/Google Photos/IMG_1.JPG", bytes(8 * 1024 * 1024))], zipfile.ZIP_DEFLATED)
        self.assertEqual(refused.reason, "zip_bomb")

    def test_a_flat_image_that_compresses_far_more_than_a_hundred_times_is_not_a_zip_bomb(self):
        flat = bytes(8 * 1024 * 1024)
        write_zip(self.world.inbox / PART, [("Takeout/Google Photos/Photos from 2019/SCAN_0001.TIF", flat)], zipfile.ZIP_DEFLATED)
        with zipfile.ZipFile(self.world.inbox / PART) as archive:
            info = archive.infolist()[0]
        self.assertGreater(info.file_size, 100 * info.compress_size)
        self.world.register()
        sha1s = []
        read = self.world.read(each_batch=lambda opened, batch: sha1s.extend(s.sha1 for s in batch))
        self.assertEqual((read.media, sha1s), (1, [sha1_of(flat)]))

    def test_ordinary_compression_is_not_a_zip_bomb(self):
        data = os.urandom(2 * 1024 * 1024)
        write_zip(self.world.inbox / PART, [("Takeout/Google Photos/IMG_1.JPG", data), ("Takeout/a.json", b"{" + b" " * 4000 + b"}")], zipfile.ZIP_DEFLATED)
        self.world.register()
        self.assertEqual(self.world.read().media, 1)

    def test_too_many_entries(self):
        with mock.patch.object(part_reader, "MAX_ENTRIES", 3):
            refused = self.refused([(f"Takeout/{n}.txt", b"x") for n in range(4)])
        self.assertEqual(refused.reason, "too_many_entries")

    def test_too_many_bytes_in_total(self):
        with mock.patch.object(part_reader, "MAX_UNPACKED_BYTES", 10):
            refused = self.refused([("Takeout/a.txt", b"123456"), ("Takeout/b.txt", b"123456")])
        self.assertEqual(refused.reason, "too_large")

    def test_device_pipe_and_socket_entries(self):
        for mode in (stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO, stat.S_IFSOCK):
            with self.subTest(mode=mode):
                world = self.world = World(self)
                refused = self.refused([(special("Takeout/dev", mode | 0o644), b"")])
                self.assertEqual(refused.reason, "device_entry")
                self.assertEqual(world.unpack_public(), [])

    def test_two_entries_with_the_same_name(self):
        refused = self.refused([("Takeout/a.txt", b"one"), ("Takeout/a.txt", b"two")])
        self.assertEqual(refused.reason, "duplicate_name")

    def test_two_entries_with_the_same_name_after_normalising(self):
        refused = self.refused([("Takeout/x/a.txt", b"one"), ("Takeout\\x\\a.txt", b"two")])
        self.assertEqual(refused.reason, "duplicate_name")

    def test_a_name_part_too_long_for_the_disk(self):
        refused = self.refused([(f"Takeout/{'n' * 256}.txt", b"x")])
        self.assertEqual(refused.reason, "name_too_long")

    def test_a_name_too_long_in_all(self):
        refused = self.refused([("Takeout/" + "/".join(["d" * 200] * 21) + "/a.txt", b"x")])
        self.assertEqual(refused.reason, "name_too_long")

    def test_an_encrypted_entry(self):
        buffer = io.BytesIO()
        write_zip(buffer, [("Takeout/secret.txt", b"x")])
        raw = bytearray(buffer.getvalue())
        raw[6] |= 0x1
        central = raw.rindex(b"PK\x01\x02")
        raw[central + 8] |= 0x1
        (self.world.inbox / PART).write_bytes(bytes(raw))
        self.world.register()
        self.assertEqual(self.world.refusal().reason, "unreadable_entry")
        self.assertEqual(self.world.ledger()[0], [])

    def test_an_entry_whose_declared_size_lies_is_damaged(self):
        buffer = io.BytesIO()
        write_zip(buffer, [("Takeout/Google Photos/IMG_1.JPG", b"z" * 100)])
        raw = bytearray(buffer.getvalue())
        central = raw.rindex(b"PK\x01\x02")
        raw[central + 24:central + 28] = (40).to_bytes(4, "little")
        (self.world.inbox / PART).write_bytes(bytes(raw))
        self.world.register()
        self.assertEqual(self.world.refusal().reason, "damaged")
        self.assertEqual(self.world.ledger()[0], [])

    def test_bytes_that_differ_from_the_declared_size_while_streaming_are_refused(self):
        write_zip(self.world.inbox / PART, [("Takeout/Google Photos/IMG_1.JPG", b"z" * 100)])
        self.world.register()
        real_open = zipfile.ZipFile.open

        def longer(archive, name, *args, **kwargs):
            opened = real_open(archive, name, *args, **kwargs)
            if isinstance(name, zipfile.ZipInfo) and name.filename.endswith(".JPG"):
                return io.BytesIO(opened.read() + b"extra")
            return opened

        with mock.patch.object(zipfile.ZipFile, "open", longer):
            refused = self.world.refusal()
        self.assertEqual(refused.reason, "size_lies")
        self.assertFalse(self.world.scratch().exists())
        self.assertEqual([sha1 for *_, sha1, _ in self.world.ledger()[0]], [None])

    def read_without_the_first_check(self, entries, compression, keep):
        world = self.world = World(self)
        corrupt_zip(world.inbox / PART, compression, keep, entries)
        world.register()
        with mock.patch.object(unpack, "test_archive"):
            refused = world.refusal()
        self.assertEqual((refused.reason, refused.public), ("damaged", DAMAGED))
        self.assertFalse(world.scratch().exists())
        return world

    def test_a_damaged_stream_found_while_streaming_media_is_refused(self):
        for label, compression, keep in COMPRESSIONS:
            with self.subTest(compression=label):
                world = self.read_without_the_first_check([(MEDIA, bytes(range(256)) * 200)], compression, keep)
                self.assertEqual([(name, sha1) for _, name, _, _, _, sha1, _ in world.ledger()[0]], [(MEDIA, None)])

    def test_a_damaged_stream_found_while_reading_a_sidecar_writes_nothing(self):
        for label, compression, keep in COMPRESSIONS:
            with self.subTest(compression=label):
                world = self.read_without_the_first_check([(SIDECAR, b'{"title": "' + b"a" * 4000 + b'"}')], compression, keep)
                self.assertEqual(world.ledger(), ([], [], [(1, None)]))

    def test_a_disk_error_while_streaming_stays_a_disk_error(self):
        write_zip(self.world.inbox / PART, [(MEDIA, b"z" * 100)])
        self.world.register()

        def full(photos, target, chunks, sha1=None):
            list(chunks)
            raise OSError(28, "No space left on device")

        with mock.patch.object(paths, "write_inbox_once", full), self.assertRaises(OSError) as raised:
            self.world.read()
        self.assertNotIsInstance(raised.exception, part_reader.PartRefused)
        self.assertEqual(raised.exception.errno, 28)

    def test_refusals_never_name_an_entry(self):
        canary = "canary-holiday-0001"
        refused = self.refused([(f"../{canary}.txt", b"x")])
        self.assertNotIn(canary, refused.public)
        self.assertNotIn(canary, str(refused))


class UnrecordedFolderTests(unittest.TestCase):
    def setUp(self):
        self.world = World(self)
        builder = TakeoutBuilder(self.world.inbox)
        builder.photo("IMG_0001.JPG")
        builder.build()
        self.world.register()
        with db.session(self.world.engine) as opened:
            export = opened.query(Export).one()
            self.name = unpack.staging_name(export)
            self.folder = self.world.photos.root / "_inbox" / "jo" / self.name
            self.folder.mkdir(parents=True)
            shutil.copy(self.world.inbox / PART, self.folder / PART)
            export.inbox_dir = None

    def test_a_part_is_read_from_the_export_folder_when_no_folder_is_recorded(self):
        self.world.read()
        self.assertTrue(self.world.ledger()[0])

    def assert_refused_and_nothing_recorded(self):
        before = self.world.ledger()
        self.world.refusal()
        self.assertEqual(self.world.ledger(), before)

    def test_a_linked_export_folder_is_refused_before_anything_is_read(self):
        elsewhere = self.world.root / "elsewhere"
        shutil.move(self.folder, elsewhere)
        os.symlink(elsewhere, self.folder)
        self.assert_refused_and_nothing_recorded()

    def test_a_linked_person_inbox_is_refused_before_anything_is_read(self):
        person = self.world.photos.root / "_inbox" / "jo"
        elsewhere = self.world.root / "elsewhere-jo"
        shutil.move(person, elsewhere)
        os.symlink(elsewhere, person)
        self.assert_refused_and_nothing_recorded()


class ScratchBatchTests(unittest.TestCase):
    def setUp(self):
        self.world = World(self)
        builder = TakeoutBuilder(self.world.inbox)
        for n in range(7):
            builder.photo(f"IMG_{n:04d}.JPG")
        self.manifest = builder.build()
        self.world.register()

    def test_no_more_than_one_batch_is_ever_on_disk_and_each_is_swept(self):
        scratch = self.world.scratch()
        seen = []

        def check(opened, batch):
            on_disk = sorted(p.name for p in scratch.iterdir())
            self.assertLessEqual(len(on_disk), 3)
            self.assertEqual(on_disk, sorted(s.path.name for s in batch))
            seen.append(len(batch))

        read = self.world.read(each_batch=check, batch_files=3)
        self.assertEqual((seen, read.batches, read.streamed), ([3, 3, 1], 3, 7))
        self.assertFalse(scratch.exists())

    def test_batches_are_capped_by_bytes_too(self):
        seen = []
        self.world.read(each_batch=lambda opened, batch: seen.append(len(batch)), batch_bytes=1)
        self.assertEqual(seen, [1] * 7)

    def test_the_scratch_folder_is_swept_before_the_first_batch(self):
        scratch = self.world.scratch()
        scratch.mkdir(parents=True)
        (scratch / "12.jpg").write_bytes(b"left over")
        (scratch / ".write-once-0123456789abcdef").write_bytes(b"left over")
        outside = self.world.root / "outside.jpg"
        outside.write_bytes(b"not ours")
        os.symlink(outside, scratch / "999999.jpg")
        (scratch / "keep.txt").write_bytes(b"not a scratch file")
        before = []
        self.world.read(each_batch=lambda opened, batch: before.append(sorted(p.name for p in scratch.iterdir())), batch_files=10)
        self.assertEqual(len(before), 1)
        self.assertNotIn("12.jpg", before[0])
        self.assertNotIn(".write-once-0123456789abcdef", before[0])
        self.assertIn("999999.jpg", before[0])
        self.assertEqual(sorted(p.name for p in scratch.iterdir()), ["999999.jpg", "keep.txt"])
        self.assertEqual(outside.read_bytes(), b"not ours")

    def test_a_link_planted_where_a_scratch_file_goes_is_never_written_through(self):
        with db.session(self.world.engine) as opened:
            first = opened.query(TakeoutEntry).count()
        self.assertEqual(first, 0)
        self.world.read(batch_files=10)
        with db.session(self.world.engine) as opened:
            row = opened.query(TakeoutEntry).filter_by(kind="media").order_by(TakeoutEntry.id).first()
            row.sha1 = None
            name = part_reader.scratch_name(row)
        scratch = self.world.scratch()
        scratch.mkdir(parents=True)
        outside = self.world.root / "outside.jpg"
        outside.write_bytes(b"not ours")
        os.symlink(outside, scratch / name)
        with self.assertRaises(paths.AlreadyExists):
            self.world.read(batch_files=10)
        self.assertEqual(outside.read_bytes(), b"not ours")
        self.assertEqual(sorted(p.name for p in scratch.iterdir()), [name])

    def test_a_failing_batch_leaves_nothing_in_scratch(self):
        def fail(opened, batch):
            raise Crash

        with self.assertRaises(Crash):
            self.world.read(each_batch=fail, batch_files=3)
        self.assertFalse(self.world.scratch().exists())

    def plant_victims(self, folder):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "1.jpg").write_bytes(b"a scratch-named victim")
        (folder / "1609459200000.jpg").write_bytes(b"a camera-numbered original")
        (folder / ".write-once-0123456789abcdef").write_bytes(b"a temp-named victim")
        (folder / "keep.txt").write_bytes(b"a foreign victim")
        (folder / "2.jpg").mkdir(exist_ok=True)
        (folder / "2.jpg" / "3.jpg").write_bytes(b"a nested victim")
        other = self.world.root / "hard-link-source.jpg"
        other.write_bytes(b"a hard-linked victim")
        os.link(other, folder / "4.jpg")
        return tree_of(folder)

    def assert_link_refused_and_nothing_removed(self, victims, before):
        with db.session(self.world.engine) as opened:
            export_folder = unpack.staging_name(opened.query(Export).one())
        with self.assertRaises((paths.LinkRefused, part_reader.PartRefused)):
            self.world.read()
        self.assertEqual(tree_of(victims), before)
        with self.assertRaises(paths.LinkRefused):
            paths.sweep_scratch(self.world.photos, "jo", export_folder)
        self.assertEqual(tree_of(victims), before)

    def export_folder(self):
        with db.session(self.world.engine) as opened:
            return self.world.photos.root / "_inbox" / "jo" / unpack.staging_name(opened.query(Export).one())

    def test_an_export_folder_that_is_a_link_is_never_swept_through(self):
        export_folder = self.export_folder()
        elsewhere = self.world.root / "elsewhere"
        before = self.plant_victims(elsewhere / "scratch")
        export_folder.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(elsewhere, export_folder)
        self.assert_link_refused_and_nothing_removed(elsewhere / "scratch", before)

    def test_a_scratch_folder_that_is_a_link_into_originals_is_never_swept_through(self):
        originals = self.world.photos.originals("jo")
        before = self.plant_victims(originals)
        export_folder = self.export_folder()
        export_folder.mkdir(parents=True)
        os.symlink(originals, export_folder / "scratch")
        self.assert_link_refused_and_nothing_removed(originals, before)

    def test_a_person_inbox_that_is_a_link_is_never_swept_through(self):
        person = self.world.photos.root / "_inbox" / "jo"
        export_name = self.export_folder().name
        elsewhere = self.world.root / "elsewhere-jo"
        shutil.move(person, elsewhere)
        before = self.plant_victims(elsewhere / export_name / "scratch")
        os.symlink(elsewhere, person)
        self.assert_link_refused_and_nothing_removed(elsewhere / export_name / "scratch", before)

    def test_an_inbox_that_is_a_link_is_never_swept_through(self):
        inbox = self.world.photos.root / "_inbox"
        export_name = self.export_folder().name
        elsewhere = self.world.root / "elsewhere-inbox"
        shutil.move(inbox, elsewhere)
        before = self.plant_victims(elsewhere / "jo" / export_name / "scratch")
        os.symlink(elsewhere, inbox)
        self.assert_link_refused_and_nothing_removed(elsewhere / "jo" / export_name / "scratch", before)

    def test_a_link_at_a_scratch_file_name_is_never_followed_by_the_sweep(self):
        victims = self.world.root / "victims"
        before = self.plant_victims(victims)
        scratch = self.world.scratch()
        scratch.mkdir(parents=True)
        os.symlink(victims / "1.jpg", scratch / "1.jpg")
        os.symlink(victims, scratch / "5.jpg")
        self.assertEqual(paths.sweep_scratch(self.world.photos, "jo", scratch.parent.name), 0)
        self.assertEqual(sorted(p.name for p in scratch.iterdir()), ["1.jpg", "5.jpg"])
        self.assertEqual(tree_of(victims), before)

    def test_the_sweep_never_removes_a_hard_link_a_folder_or_anything_outside_scratch(self):
        originals = self.world.photos.originals("jo")
        originals.mkdir(parents=True)
        original = originals / "IMG_0001.JPG"
        original.write_bytes(b"an original")
        scratch = self.world.scratch()
        scratch.mkdir(parents=True)
        os.link(original, scratch / "5.jpg")
        (scratch / "7.jpg").mkdir()
        (scratch / "7.jpg" / "8.jpg").write_bytes(b"inside a folder")
        export_folder = scratch.parent
        siblings = {export_folder / "staging" / "9.jpg": b"staged", self.world.photos.root / "_inbox" / "jo" / "review" / "10.jpg": b"held"}
        for path, data in siblings.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.assertEqual(paths.sweep_scratch(self.world.photos, "jo", export_folder.name), 0)
        self.assertEqual(sorted(p.name for p in scratch.iterdir()), ["5.jpg", "7.jpg"])
        self.assertEqual((original.read_bytes(), (scratch / "7.jpg" / "8.jpg").read_bytes()), (b"an original", b"inside a folder"))
        for path, data in siblings.items():
            self.assertEqual(path.read_bytes(), data)

    def test_the_sweep_removes_only_the_scratch_files_and_then_the_folder(self):
        scratch = self.world.scratch()
        scratch.mkdir(parents=True)
        for name in ("1.jpg", "22", ".write-once-0123456789abcdef"):
            (scratch / name).write_bytes(b"left over")
        self.assertEqual(paths.sweep_scratch(self.world.photos, "jo", scratch.parent.name), 3)
        self.assertFalse(scratch.exists())
        self.assertTrue(scratch.parent.is_dir())

    def test_sweep_scratch_needs_no_folder(self):
        self.assertEqual(paths.sweep_scratch(self.world.photos, "jo", "export-404"), 0)

    def test_scratch_file_names_are_only_scratch_names(self):
        with self.assertRaises(paths.OutsideRoot):
            paths.scratch_file(self.world.photos, "jo", "export-1", "../a.jpg")
        with self.assertRaises(paths.OutsideRoot):
            paths.scratch_file(self.world.photos, "jo", "export-1", ".write-once-0123456789abcdef")


class CrashTests(unittest.TestCase):
    def build(self):
        world = World(self)
        builder = TakeoutBuilder(world.inbox, seed=3, parts=2)
        for n in range(5):
            builder.photo(f"IMG_{n:04d}.JPG", part=n % 2, sidecar_part=(n + 1) % 2)
        builder.non_media(part=1)
        builder.build()
        world.register()
        return world

    def read_counting(self, world, crash_at=None):
        original = Session.commit
        calls = [0]

        def counting(opened):
            calls[0] += 1
            if calls[0] == crash_at:
                raise Crash
            return original(opened)

        with mock.patch.object(Session, "commit", counting):
            for number in (1, 2):
                world.read(number, batch_files=2)
        return calls[0]

    def test_a_crash_at_every_commit_leaves_no_scratch_and_resumes_to_the_same_ledger(self):
        reference = self.build()
        total = self.read_counting(reference)
        self.assertGreaterEqual(total, 6)
        expected = reference.ledger()
        for crash_at in range(1, total + 1):
            with self.subTest(crash_at=crash_at):
                world = self.build()
                with self.assertRaises(Crash):
                    self.read_counting(world, crash_at)
                self.assertFalse(world.scratch().exists())
                world.read(1, batch_files=2)
                world.read(2, batch_files=2)
                self.assertEqual(world.ledger(), expected)
                self.assertFalse(world.scratch().exists())


class RelistTests(unittest.TestCase):
    def setUp(self):
        self.world = World(self)
        self.manifest = standard_export(self.world.inbox, parts=1)
        self.world.register()
        self.world.read()

    def ids(self):
        with db.session(self.world.engine) as opened:
            return sorted(e.id for e in opened.query(TakeoutEntry)), sorted(s.id for s in opened.query(TakeoutSidecar))

    def test_reading_the_same_part_again_changes_nothing(self):
        before, ids = self.world.ledger(), self.ids()
        again = self.world.read()
        self.assertTrue(again.relisted)
        self.assertEqual((self.world.ledger(), self.ids()), (before, ids))

    def test_a_re_downloaded_copy_of_the_same_part_changes_nothing(self):
        before = self.world.ledger()
        path = self.world.inbox / PART
        copy = path.read_bytes()
        path.unlink()
        path.write_bytes(copy)
        self.assertTrue(self.world.read().relisted)
        self.assertEqual(self.world.ledger(), before)

    def assert_refused_as_different(self, entries):
        before = self.world.ledger()
        (self.world.inbox / PART).unlink()
        write_zip(self.world.inbox / PART, entries)
        refused = self.world.refusal()
        self.assertEqual(refused.reason, "listing_differs")
        self.assertEqual(self.world.ledger(), before)
        self.assertFalse(self.world.scratch().exists())

    def test_a_part_with_an_extra_entry_is_refused(self):
        self.assert_refused_as_different([*((info, data) for info, data in zip_entries(self.manifest.parts[0])), ("Takeout/new.txt", b"new")])

    def test_a_part_with_a_changed_entry_is_refused(self):
        entries = zip_entries(self.manifest.parts[0])
        info = next(i for i, _ in entries if i.filename.endswith(".JPG"))
        changed = [(i, d if i is not info else d[:-1] + bytes([d[-1] ^ 1])) for i, d in entries]
        self.assert_refused_as_different(changed)

    def test_a_part_missing_an_entry_is_refused(self):
        self.assert_refused_as_different(zip_entries(self.manifest.parts[0])[1:])

    def test_a_part_listed_under_an_older_classification_is_not_refused_and_is_reclassified(self):
        expected = self.world.ledger()
        with db.session(self.world.engine) as opened:
            sidecar = opened.query(TakeoutEntry).filter_by(kind="sidecar").order_by(TakeoutEntry.id).first()
            opened.query(TakeoutSidecar).filter_by(entry_id=sidecar.id).delete()
            sidecar.kind = "other"
            media = opened.query(TakeoutEntry).filter_by(kind="media").order_by(TakeoutEntry.id).first()
            media.kind = "other"
        again = self.world.read()
        self.assertTrue(again.relisted)
        self.assertEqual(self.world.ledger(), expected)

    def test_media_whose_recorded_sha1_differs_is_refused(self):
        with db.session(self.world.engine) as opened:
            row = opened.query(TakeoutEntry).filter_by(kind="media").order_by(TakeoutEntry.id).first()
            row.sha1 = "0" * 40
        before = self.world.ledger()
        refused = self.world.refusal()
        self.assertEqual(refused.reason, "bytes_differ")
        self.assertEqual(self.world.ledger(), before)
        self.assertFalse(self.world.scratch().exists())

    def test_streamed_media_already_past_listed_is_not_streamed_again(self):
        with db.session(self.world.engine) as opened:
            for row in opened.query(TakeoutEntry).filter_by(kind="media"):
                row.outcome = "placed"
        called = []
        read = self.world.read(each_batch=lambda opened, batch: called.append(batch))
        self.assertEqual((read.streamed, called), (0, []))


class PromiseTests(unittest.TestCase):
    def test_nothing_is_written_beyond_the_ledger_and_the_scratch(self):
        world = World(self)
        originals = world.photos.originals("jo")
        originals.mkdir(parents=True)
        (originals / "IMG_0001.JPG").write_bytes(b"an original")
        standard_export(world.inbox, parts=2)
        world.register()
        files, folders = tree_of(world.photos.root)
        world.read(1)
        world.read(2)
        after_files, after_folders = tree_of(world.photos.root)
        self.assertEqual(after_files, files)
        self.assertEqual(after_folders - folders, {str(world.scratch().parent.relative_to(world.photos.root))})
        self.assertEqual(folders - after_folders, set())

    def test_events_and_logs_carry_no_entry_name(self):
        world = World(self)
        canary = "canary-beach-name-7781"
        write_zip(world.inbox / PART, [(f"Takeout/Google Photos/{canary}/{canary}.JPG", b"x" * 10),
                                        (f"Takeout/Google Photos/{canary}/{canary}.JPG.json", f'{{"title": "{canary}"}}'.encode())])
        world.register()
        with no_leaks(self, canary):
            world.read()
        with db.session(world.engine) as opened:
            self.assertFalse([e for e in opened.query(Event) if canary in f"{e.type} {e.subject} {e.detail}"])


class WholeExportPathTests(unittest.TestCase):
    def test_unpack_after_a_read_stages_the_same_tree(self):
        plain, read_first = World(self), World(self)
        for world in (plain, read_first):
            standard_export(world.inbox, parts=2)
            world.register()
        read_first.read(1)
        read_first.read(2)
        self.assertEqual(plain.unpack_public(), [])
        self.assertEqual(read_first.unpack_public(), [])
        trees = []
        for world in (plain, read_first):
            with db.session(world.engine) as opened:
                staging = unpack.staging_dir(world.photos, opened.query(Person).one(), opened.query(Export).one())
            trees.append(tree_of(staging))
        self.assertEqual(trees[0], trees[1])

    def test_check_parts_still_marks_the_part(self):
        world = World(self)
        (world.inbox / PART).write_bytes(b"not a zip")
        world.register()
        self.assertEqual(world.unpack_public(), ["part 1 is not a valid zip"])
        with db.session(world.engine) as opened:
            self.assertEqual(opened.query(ExportPart).one().status, "corrupt")


class SidecarMemoryTests(unittest.TestCase):
    def setUp(self):
        self.world = World(self)
        folder = "Takeout/Google Photos/Photos from 2019"
        self.texts = {f"{folder}/IMG_{n:04d}.JPG.json": '{"title": "%s"}' % (f"{n:04d}" * 248) for n in range(40)}
        self.size = len(next(iter(self.texts.values())))
        write_zip(self.world.inbox / PART, [(name, text.encode()) for name, text in self.texts.items()], zipfile.ZIP_DEFLATED)
        self.world.register()

    def live_sidecars(self):
        return sum(1 for found in gc.get_objects() if isinstance(found, TakeoutSidecar))

    def test_a_part_of_many_sidecars_never_holds_more_than_a_flush_of_their_text(self):
        real_flush, real_text = part_reader.flush_out, part_reader.sidecar_text
        at_flush, while_reading = [], []

        def flushing(opened, held):
            at_flush.append(self.live_sidecars())
            return real_flush(opened, held)

        def reading(*args):
            while_reading.append(self.live_sidecars())
            return real_text(*args)

        with mock.patch.object(part_reader, "FLUSH_TEXT_BYTES", 3 * self.size), mock.patch.object(part_reader, "flush_out", flushing), \
                mock.patch.object(part_reader, "sidecar_text", reading):
            read = self.world.read()
        self.assertGreaterEqual(len(at_flush), 13)
        self.assertLessEqual(max(at_flush), 3)
        self.assertEqual((len(while_reading), max(while_reading)), (40, 0))
        self.assertEqual(read.sidecars, 40)
        self.assertEqual({f"{folder}/{name}": text for _, folder, name, text in self.world.ledger()[1]}, self.texts)

    def test_a_refusal_while_reading_sidecars_writes_nothing(self):
        real = part_reader.sidecar_text
        calls = [0]

        def lying(*args):
            calls[0] += 1
            if calls[0] == 30:
                raise part_reader.PartRefused("size_lies", part_reader.SIZE_LIES)
            return real(*args)

        with mock.patch.object(part_reader, "sidecar_text", lying):
            refused = self.world.refusal()
        self.assertEqual(refused.reason, "size_lies")
        self.assertEqual(self.world.ledger(), ([], [], [(1, None)]))

    def test_a_failure_after_some_sidecars_were_flushed_writes_nothing(self):
        real = part_reader.flush_out
        calls = [0]

        def failing(opened, held):
            calls[0] += 1
            if calls[0] == 4:
                raise OSError(5, "Input/output error")
            return real(opened, held)

        with mock.patch.object(part_reader, "FLUSH_TEXT_BYTES", 2 * self.size), mock.patch.object(part_reader, "FLUSH_ROWS", 7), \
                mock.patch.object(part_reader, "flush_out", failing), self.assertRaises(OSError):
            self.world.read()
        self.assertEqual(self.world.ledger(), ([], [], [(1, None)]))

    def test_sidecars_are_read_from_the_zip_before_the_write_lock_is_taken(self):
        locked = []
        real_lock, real_text = part_reader.db.write_lock, part_reader.sidecar_text

        def lock(opened):
            locked.append(True)
            return real_lock(opened)

        def reading(*args):
            self.assertEqual(locked, [])
            return real_text(*args)

        with mock.patch.object(part_reader.db, "write_lock", lock), mock.patch.object(part_reader, "sidecar_text", reading):
            self.assertEqual(self.world.read().sidecars_unread, 0)
        self.assertEqual(locked, [True])

    def test_sidecars_too_big_in_all_are_refused_without_being_decompressed(self):
        with mock.patch.object(part_reader, "MAX_SIDECAR_BYTES", 40 * self.size - 1), \
                mock.patch.object(zipfile.ZipFile, "testzip", side_effect=AssertionError("decompressed")):
            refused = self.world.refusal()
        self.assertEqual((refused.reason, refused.public), ("sidecars_too_big", part_reader.SIDECARS_TOO_BIG))
        self.assertEqual(self.world.ledger(), ([], [], [(1, None)]))

    def test_sidecars_at_the_total_cap_are_read(self):
        with mock.patch.object(part_reader, "MAX_SIDECAR_BYTES", 40 * self.size):
            self.assertEqual(self.world.read().sidecars_unread, 0)

    def test_sidecars_too_big_to_read_do_not_count_towards_the_total(self):
        with mock.patch.object(part_reader, "MAX_SIDECAR_BYTES", self.size), mock.patch.object(part_reader, "SIDECAR_MAX_BYTES", self.size - 1):
            read = self.world.read()
        self.assertEqual((read.sidecars, read.sidecars_unread), (40, 40))
