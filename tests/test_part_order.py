import contextlib
import dataclasses
import hashlib
import itertools
import random
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from sqlalchemy import select

from darkroom.domain import index
from darkroom.domain.ingest import finish_export, part_ingest, run
from darkroom.providers import exiftool
from darkroom.storage import paths
from darkroom.storage.models import Alert, DedupeDecision, Event, Export, ExportPart, ImportRun, MediaItem, Person, ReviewItem
from tests.fixtures import media as fixtures
from tests.import_world import DAY, INDEXED, NOW
from tests.privacy import no_leaks
from tests.takeout_fixtures import TakeoutBuilder
from tests.test_finish_export import FinishWorld

GOOGLE_ID = "google-123"
STAMP = "20260101T000000Z"
SUMMARY = "Takeout import finished"
YEAR = "Photos from 2019"
ALBUM = "Family trip"
LEFT_OUT = ("/scratch/", "/staging/", "/.darkroom-unpacked/")
NOT_COMPARED_RUN = frozenset({"id", "paths_renamed", "sidecars_differ", "changed_people"})
UNDECIDED = frozenset({*part_ingest.HELD, part_ingest.LISTED})
TIER_2 = {"taken": "2019:07:04 08:30:15", "offset": "+02:00", "make": "Apple", "model": "iPhone 11"}
BURST = {"taken": "2019:09:09 09:09:09", "offset": "+02:00", "make": "Apple", "model": "iPhone 11"}
NEAR = {"offset": "+02:00", "make": "Apple", "model": "iPhone 11"}
ALBUM_COPIES = (("IMG_3001.JPG", 0, 3, None), ("IMG_3002.JPG", 2, 1, 4))
LONG_NAME = "a_made_up_long_file_name_from_a_camera_roll_0042.JPG"
VIDEO = "IMG_6101.MOV"


def digest(path):
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest(), path.suffix.lower()
    except OSError:
        return None


class Memo:
    def __init__(self):
        self.tags = {}
        self.sidecars = {}

    def read(self, given):
        found, missing = {}, []
        for path in given:
            key = digest(path)
            if key is not None and key in self.tags:
                found[path] = dict(self.tags[key])
            else:
                missing.append((path, key))
        if missing:
            fresh = exiftool.read([path for path, _ in missing])
            for path, key in missing:
                if path in fresh:
                    found[path] = fresh[path]
                    if key is not None:
                        self.tags[key] = dict(fresh[path])
        return found

    def write(self, sidecar_path, fields):
        key = (tuple(sorted(fields.items())), digest(sidecar_path))
        made = self.sidecars.get(key)
        if made is None:
            written = exiftool.write_xmp(sidecar_path, fields)
            if written:
                self.sidecars[key] = sidecar_path.read_bytes()
            return written
        paths.write_sidecar(sidecar_path, lambda temp, source: temp.write_bytes(made))
        return True


MEMO = Memo()


@dataclass(frozen=True)
class Fixture:
    zips: tuple[tuple[str, bytes], ...]
    originals: tuple[tuple[str, str, bytes], ...]


class Kit:
    def __init__(self, folder):
        self.folder, self.originals, self.made = folder, [], 0

    def original(self, slug, relative, data):
        self.originals.append((slug, relative, data))

    def jpeg(self, colour, **tags):
        self.made += 1
        path = fixtures.make_jpeg(self.folder / f"kit-{self.made}.jpg", colour=colour, **tags)
        data = path.read_bytes()
        path.unlink()
        return data


BUILT = {}


def fixture_of(build, parts, seed=7):
    key = (build.__name__, parts, seed)
    if key not in BUILT:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            kit = Kit(root / "kit")
            builder = TakeoutBuilder(root / "zips", seed=seed, parts=parts, stamp=STAMP)
            build(builder, kit)
            zips = tuple((path.name, path.read_bytes()) for path in builder.build().parts)
        BUILT[key] = Fixture(zips, tuple(kit.originals))
    return BUILT[key]


def five_parts(builder, kit):
    exact = builder.image_bytes(".JPG")
    kit.original("jo", "IMG_1001.JPG", exact)
    builder.photo("IMG_1001.JPG", data=exact, gps=None, part=0, sidecar_part=3)
    late = builder.image_bytes(".JPG")
    kit.original("sam", "2019/IMG_1002.JPG", late)
    builder.photo("IMG_1002.JPG", data=late, part=1, sidecar_part=4, gps=(46.0, 11.0), description="a late description")
    kit.original("jo", "IMG_1003.JPG", kit.jpeg((10, 120, 30), **TIER_2))
    builder.photo("IMG_1003.JPG", data=kit.jpeg((12, 118, 33), **TIER_2), gps=None, taken=1562221815, part=2, sidecar_part=0)
    base = builder.image_bytes(".JPG")
    kit.original("sam", "IMG_1004.JPG", base)
    builder.photo("IMG_1004.JPG", data=base, part=3)
    builder.add("IMG_1004-edited.JPG", builder.image_bytes(".JPG"), YEAR, 0, None, None, None, "")
    kit.original("jo", "IMG_1005.JPG", kit.jpeg((90, 90, 200), taken="2019:08:01 10:00:00", **NEAR))
    builder.photo("IMG_1005.JPG", data=kit.jpeg((90, 90, 200), taken="2019:08:01 10:00:01", **NEAR), gps=None, part=4)
    kit.original("jo", "IMG_1006.JPG", kit.jpeg((200, 10, 10), **BURST))
    builder.photo("IMG_1006.JPG", data=kit.jpeg((201, 12, 10), **BURST), gps=None, part=0)
    builder.photo("IMG_1007.JPG", data=kit.jpeg((202, 14, 12), **BURST), gps=None, part=4)
    builder.photo("IMG_2001.JPG", gps=(45.4, 12.3), description="A made-up canal", part=2)
    builder.photo("IMG_2002.JPG", gps=(47.0, 12.0), part=1, sidecar_part=3)
    builder.photo("IMG_2003.JPG", form=None, part=4)
    for name, album, year, album_sidecar in ALBUM_COPIES:
        data = builder.image_bytes(".JPG")
        builder.add(name, data, YEAR, year, "json", None, (45.4, 12.3), "", taken=1_500_000_000)
        builder.add(name, data, ALBUM, album, "json", album_sidecar, (45.4, 12.3), "", taken=1_500_000_000)
    builder.photo("IMG_4000.JPG", part=2)
    builder.add("IMG_4000(1).JPG", builder.image_bytes(".JPG"), YEAR, 4, None, 1, (41.0, 13.0), "", sidecar_name="IMG_4000.JPG(1).json")
    builder.photo("IMG_6001.JPG", part=1)
    builder.add("IMG_6001.MOV", builder.video_bytes(), YEAR, 3, "json", 0, None, "")


def three_parts(builder, kit, offset=0):
    builder.photo(LONG_NAME, form="limit", part=offset + 2, sidecar_part=offset)
    builder.photo("IMG_4100.JPG", form="truncated", part=offset + 1, sidecar_part=offset + 2)
    builder.photo("IMG_7001.JPG", form="metadata", part=offset, sidecar_part=offset + 1)
    builder.add("IMG_9002.JPG", builder.image_bytes(".JPG"), "Trash", offset + 1, "json", None, (45.4, 12.3), "")
    builder.add("IMG_9003.JPG", builder.image_bytes(".JPG"), "Bin", offset + 2, "json", None, (45.4, 12.3), "")
    builder.orphan_sidecar("IMG_9999.JPG", part=offset + 2)
    builder.non_media(part=offset + 1)
    builder.loose_file("notes-made-up.txt", part=offset)
    builder.photo("IMG_3003.JPG", part=offset + 2)
    builder.add("IMG_3003-edited.JPG", builder.image_bytes(".JPG"), YEAR, offset, None, None, None, "")
    builder.photo("IMG_5005.JPG", part=offset)


def seven_parts(builder, kit):
    five_parts(builder, kit)
    three_parts(builder, kit, offset=4)


def live_photo_apart(builder, kit):
    builder.photo("IMG_6101.JPG", part=0)
    builder.add(VIDEO, builder.video_bytes(), YEAR, 2, None, None, None, "")
    builder.photo("IMG_6102.JPG", part=1, sidecar_part=2)


def canaries(builder, kit):
    builder.photo("canary-name-0001.JPG", part=0, sidecar_part=1, description="canary-text-0001")
    builder.photo("canary-name-0002.JPG", form=None, part=1)
    builder.add("canary-name-0003.JPG", builder.image_bytes(".JPG"), "Trash", 0, "json", None, (45.4, 12.3), "canary-text-0003")
    builder.loose_file("canary-name-0004.txt", part=1)
    builder.orphan_sidecar("canary-name-0005.JPG", part=1)


def album_renames(albums):
    def renamed(order):
        position = {number: place for place, number in enumerate(order)}
        return sum(1 for _, album, year, _ in albums if position[album + 1] < position[year + 1])

    return renamed


def columns(row, skipped, mapped=None):
    mapped = mapped or {}
    return tuple((column.name, mapped[column.name](getattr(row, column.name)) if column.name in mapped else getattr(row, column.name))
                 for column in row.__table__.columns if column.name not in skipped)


IDENTITY = {"media_items": "sha1", "dedupe_decisions": "source_path", "review_items": "decision_id"}


def compact(mine, expected):
    found = {}
    for table, column in IDENTITY.items():
        ours = {dict(row)[column]: dict(row) for row in mine["records"][table]}
        theirs = {dict(row)[column]: dict(row) for row in expected["records"][table]}
        for key in sorted(set(ours) | set(theirs), key=repr):
            left, right = ours.get(key, {}), theirs.get(key, {})
            changed = {name: (left.get(name), right.get(name)) for name in sorted(set(left) | set(right)) if left.get(name) != right.get(name)}
            if changed:
                found[f"{table} {key}"] = changed
    files = {key: (mine["files"].get(key), expected["files"].get(key)) for key in sorted(set(mine["files"]) | set(expected["files"]))
             if mine["files"].get(key) != expected["files"].get(key)}
    if files:
        found["files"] = files
    for part in ("counts", "summary", "export"):
        if mine[part] != expected[part]:
            found[part] = (mine[part], expected[part])
    return found


class Scope:
    def __init__(self, case):
        self.case, self.cleanups = case, []

    def addCleanup(self, function, *args, **kwargs):
        self.cleanups.append((function, args, kwargs))

    def __getattr__(self, name):
        return getattr(self.case, name)

    def close(self):
        while self.cleanups:
            function, args, kwargs = self.cleanups.pop()
            function(*args, **kwargs)


@contextlib.contextmanager
def scoped(case):
    scope = Scope(case)
    try:
        yield scope
    finally:
        scope.close()


class OrderWorld(FinishWorld):
    def __init__(self, test):
        super().__init__(test, immich_configured=False)
        self.tools = run.Tools(read=MEMO.read, write=MEMO.write, notifier=self.recorder)

    def index_all(self, at=INDEXED):
        for slug in self.people:
            index.index_person(self.opened, self.photos, self.opened.get(Person, self.ids[slug]), at, read=MEMO.read)
        self.opened.commit()

    def made(self, fixture, whole=False):
        for slug, relative, data in fixture.originals:
            self.original(slug, relative, data)
        inbox = self.photos.inbox(self.people[0]) / GOOGLE_ID
        inbox.mkdir(parents=True, exist_ok=True)
        for name, data in fixture.zips:
            (inbox / name).write_bytes(data)
        self.index_all()
        export = Export(account_id=self.account, kind="initial", google_id=GOOGLE_ID, created_at=NOW - DAY, ready_at=NOW, expires_at=NOW + 7 * DAY,
                        part_count=len(fixture.zips), status="downloaded" if whole else "downloading", inbox_dir=str(inbox))
        self.opened.add(export)
        self.opened.flush()
        self.opened.add_all(ExportPart(export_id=export.id, number=number, filename=name, bytes=len(data), status="done",
                                       ingest_state="not_queued" if whole else "queued", ingest_queued_at=None if whole else NOW)
                            for number, (name, data) in enumerate(fixture.zips, start=1))
        self.opened.commit()
        return export

    def records(self):
        self.opened.expire_all()
        items = list(self.opened.execute(select(MediaItem).order_by(MediaItem.source, MediaItem.path)).scalars())
        path_of = {row.id: row.path for row in items}.get
        decisions = list(self.opened.execute(select(DedupeDecision).order_by(DedupeDecision.source_path)).scalars())
        source_of = {row.id: row.source_path for row in decisions}.get
        reviews = self.opened.execute(select(ReviewItem)).scalars()
        return {"media_items": [columns(row, {"id", "mtime"}) for row in items],
                "dedupe_decisions": [columns(row, {"id"}, {"takeout_item_id": path_of, "matched_item_id": path_of}) for row in decisions],
                "review_items": sorted((columns(row, {"id"}, {"decision_id": source_of}) for row in reviews), key=repr)}

    def placed_files(self):
        return {key: value for key, value in self.files().items() if not any(part in f"/{key}" for part in LEFT_OUT)}

    def the_run(self):
        self.opened.expire_all()
        return self.opened.execute(select(ImportRun)).scalar_one()

    def snapshot(self):
        self.opened.expire_all()
        export = self.opened.execute(select(Export)).scalar_one()
        return {"records": self.records(), "files": self.placed_files(), "counts": columns(self.the_run(), NOT_COMPARED_RUN),
                "summary": [sent for sent in self.recorder.sent if sent[0] == SUMMARY], "export": (export.status, export.stage)}

    def differences(self):
        row = self.the_run()
        return row.paths_renamed, row.sidecars_differ

    def undecided(self):
        return {name for name, outcome in self.outcomes().items() if outcome in UNDECIDED}

    def item_named(self, name):
        self.opened.expire_all()
        return self.opened.execute(select(MediaItem).where(MediaItem.source == "takeout", MediaItem.path.endswith(f"/{name}", autoescape=True))).scalar_one()

    def item_decided_as(self, name):
        found = self.opened.get(MediaItem, self.decision_named(name).takeout_item_id)
        assert found is not None
        return found

    def decision_named(self, name):
        self.opened.expire_all()
        return self.opened.execute(select(DedupeDecision).where(DedupeDecision.source_path.endswith(f"/{name}", autoescape=True))).scalar_one()


class OrderCase(unittest.TestCase):
    maxDiff = None

    def whole(self, fixture):
        twin = OrderWorld(self)
        twin.run_import(twin.made(fixture, whole=True))
        return twin

    def per_part(self, world, fixture, order):
        export = world.made(fixture)
        try:
            world.ingest_all(export, order)
            world.finish(export)
        except Exception as error:
            raise AssertionError(f"ingesting the parts in the order {order} failed") from error
        return world

    def check_orders(self, fixture, orders, renamed):
        expected = self.whole(fixture).snapshot()
        self.assertEqual(expected["export"], ("imported", "done"))
        differ = {}
        for order in orders:
            with scoped(self) as scope:
                world = self.per_part(OrderWorld(scope), fixture, order)
                found = compact(world.snapshot(), expected)
                if world.differences() != (renamed(order), 0):
                    found["paths_renamed, sidecars_differ"] = (world.differences(), (renamed(order), 0))
                if world.undecided():
                    found["undecided"] = sorted(world.undecided())
                if found:
                    differ[order] = found
        self.assertEqual(differ, {}, f"{len(differ)} of {len(orders)} part orders differ from whole-export ingest (per-part, whole-export)")
        return expected


class EveryOrderTests(OrderCase):
    def test_every_order_of_a_five_part_export_gives_the_records_and_files_of_whole_export_ingest(self):
        orders = list(itertools.permutations(range(1, 6)))
        self.assertEqual(len(orders), 120)
        expected = self.check_orders(fixture_of(five_parts, 5), orders, album_renames(ALBUM_COPIES))
        decided = [dict(row) for row in expected["records"]["dedupe_decisions"]]
        tiers = {row["source_path"].rsplit("/", 1)[-1]: row["tier"] for row in decided}
        self.assertEqual({name: tiers[name] for name in ("IMG_1001.JPG", "IMG_1003.JPG", "IMG_1004-edited.JPG", "IMG_1006.JPG", "IMG_1007.JPG", "IMG_2002.JPG")},
                         {"IMG_1001.JPG": 1, "IMG_1003.JPG": 2, "IMG_1004-edited.JPG": 3, "IMG_1006.JPG": 4, "IMG_1007.JPG": 4, "IMG_2002.JPG": 5})
        self.assertEqual(sorted(row["source_path"].removeprefix("Takeout/Google Photos/") for row in decided if "IMG_300" in row["source_path"]),
                         [f"{YEAR}/IMG_3001.JPG", f"{YEAR}/IMG_3002.JPG"])

    def test_every_order_of_a_three_part_export_with_trash_orphans_and_cut_names_gives_whole_export_ingest(self):
        orders = list(itertools.permutations(range(1, 4)))
        expected = self.check_orders(fixture_of(three_parts, 3), orders, lambda order: 0)
        decided = {dict(row)["source_path"].rsplit("/", 1)[-1] for row in expected["records"]["dedupe_decisions"]}
        self.assertLessEqual({LONG_NAME, "IMG_4100.JPG", "IMG_7001.JPG", "IMG_3003-edited.JPG"}, decided)
        self.assertEqual(decided & {"IMG_9002.JPG", "IMG_9003.JPG"}, set())
        self.assertGreaterEqual(dict(expected["counts"])["orphan_sidecars"], 1)

    def test_seeded_orders_of_a_seven_part_export_give_whole_export_ingest(self):
        chance = random.Random(484)
        orders = [tuple(chance.sample(range(1, 8), 7)) for _ in range(4)]
        self.check_orders(fixture_of(seven_parts, 7), orders, album_renames(ALBUM_COPIES))


def own_part_sidecars_only(self):
    rows = [row for row in REAL_SIDECAR_ROWS(self) if row.part_number == self.part.number]
    self.texts = {row.entry_id: row.text for row in rows}
    return rows


def edits_as_originals(self, unit):
    return dataclasses.replace(REAL_CANDIDATE(self, unit), is_edited=False, edited_of=None)


REAL_SIDECAR_ROWS = part_ingest.Ingest.sidecar_rows
REAL_CANDIDATE = finish_export.Finish.candidate


class BrokenVariantTests(OrderCase):
    def setUp(self):
        self.fixture = fixture_of(five_parts, 5)
        self.twin = self.whole(self.fixture)

    def test_the_gate_fails_when_a_part_pairs_only_with_its_own_sidecars(self):
        with mock.patch.object(part_ingest.Ingest, "sidecar_rows", own_part_sidecars_only):
            broken = self.per_part(OrderWorld(self), self.fixture, (1, 2, 3, 4, 5))
        self.assertNotEqual(broken.snapshot(), self.twin.snapshot())
        self.assertEqual(round(self.twin.item_decided_as("IMG_4000(1).JPG").lat, 5), 41.0)
        self.assertIsNone(broken.item_decided_as("IMG_4000(1).JPG").lat)

    def test_the_gate_fails_when_an_edited_copy_is_decided_as_an_original(self):
        with mock.patch.object(finish_export.Finish, "candidate", edits_as_originals):
            broken = self.per_part(OrderWorld(self), self.fixture, (1, 2, 3, 4, 5))
        self.assertNotEqual(broken.snapshot(), self.twin.snapshot())
        self.assertEqual(self.twin.decision_named("IMG_1004-edited.JPG").tier, 3)
        self.assertNotEqual(broken.decision_named("IMG_1004-edited.JPG").tier, 3)


def apart_from(snapshot, name):
    return {"records": {table: [row for row in rows if name not in repr(row)] for table, rows in snapshot["records"].items()},
            "files": {key: value for key, value in snapshot["files"].items() if name not in key},
            "counts": tuple(pair for pair in snapshot["counts"] if pair[0] != "missing_gps"), "export": snapshot["export"]}


class LivePhotoDifferenceTests(OrderCase):
    def facts(self, world):
        item = world.item_named(VIDEO)
        position = None if item.lat is None else (round(item.lat, 5), round(item.lon, 5))
        xmps = sorted(key.rsplit("/", 1)[-1] for key in world.placed_files() if VIDEO in key and key.endswith(".xmp"))
        return item.sidecar_json is None, item.sidecar_from_still, position, world.decision_named(VIDEO).tier, xmps

    def test_a_live_photo_video_without_its_own_sidecar_is_the_one_recorded_difference_in_every_order(self):
        fixture = fixture_of(live_photo_apart, 3)
        twin = self.whole(fixture)
        expected = twin.snapshot()
        self.assertEqual(self.facts(twin)[1:], (True, (40.7, -74.0), 5, [f"{VIDEO}.xmp"]))
        theirs = dict(expected["counts"])["missing_gps"]
        for order in itertools.permutations(range(1, 4)):
            with scoped(self) as scope:
                world = self.per_part(OrderWorld(scope), fixture, order)
                said = f"parts ingested in the order {order}"
                mine = world.snapshot()
                self.assertEqual(apart_from(mine, VIDEO), apart_from(expected, VIDEO), said)
                self.assertEqual(self.facts(world), (True, False, None, 5, []), said)
                self.assertEqual(dict(mine["counts"])["missing_gps"], theirs + 1, said)
                self.assertEqual(mine["summary"], [(title, text.replace(f"{theirs} missing GPS", f"{theirs + 1} missing GPS")) for title, text in expected["summary"]], said)
                self.assertEqual(world.differences(), (0, 0), said)


class PrivacyTests(unittest.TestCase):
    def test_a_part_ingest_and_a_finish_log_no_file_name_or_sidecar_text(self):
        world = OrderWorld(self)
        export = world.made(fixture_of(canaries, 2))
        with no_leaks(self, "canary-name", "canary-text"):
            world.ingest_all(export, (2, 1))
            world.finish(export)
        self.assertEqual(world.opened.get(Export, export.id).status, "imported")
        said = [f"{row.subject} {row.detail}" for row in world.opened.execute(select(Event)).scalars()]
        said += [f"{row.subject} {row.message}" for row in world.opened.execute(select(Alert)).scalars()]
        said += [f"{title} {text}" for title, text in world.recorder.sent]
        self.assertTrue(said)
        self.assertEqual([text for text in said if "canary" in text], [])


if __name__ == "__main__":
    unittest.main()
