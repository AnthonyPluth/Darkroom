import dataclasses
import random
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import select

from darkroom import config, monitoring
from darkroom.domain import media
from darkroom.domain.ingest import heartbeat, pairing, part_ingest
from darkroom.server import jobs
from darkroom.storage.models import Alert, Event
from tests.import_world import NOW
from tests.privacy import no_leaks
from tests.shared import settings_from
from tests.test_part_ingest import PartWorld

PRIVATE_NAME = "Canary-Private-Name-4711.JPG"
PRIVATE_TEXT = "canary private description 4711"


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def logged_lines(log: mock.MagicMock) -> list[tuple[str, dict]]:
    return [(call.args[0], call.kwargs) for call in log.call_args_list]


def old_supplemental(media_path, sidecars):
    name = media_path.name
    for sidecar_name, path in sorted(sidecars.items()):
        stem = pairing.stem_of(sidecar_name)
        if stem.startswith(name) and len(stem) > len(name) and pairing.is_family_tail(stem[len(name):]):
            return path
    return None


def old_counter(media_path, sidecars):
    found = pairing.NUMBERED.match(media_path.stem)
    if not found:
        return None
    base, tag = f"{found.group('stem')}{media_path.suffix}", f"({found.group('number')})"
    for sidecar_name, path in sorted(sidecars.items()):
        stem = pairing.stem_of(sidecar_name)
        if not stem.endswith(tag):
            continue
        rest = stem[: -len(tag)]
        if rest == base or (rest.startswith(base) and pairing.is_family_tail(rest[len(base):])):
            return path
    return None


def old_truncated(media_path, sidecars):
    full = f"{media_path.name}{pairing.SUPPLEMENTAL}"
    if len(media_path.name) <= pairing.SIDECAR_NAME_LIMIT - 1 and len(full) <= pairing.SIDECAR_NAME_LIMIT:
        return None
    for sidecar_name, path in sorted(sidecars.items()):
        stem = pairing.stem_of(sidecar_name)
        if len(stem) == pairing.SIDECAR_NAME_LIMIT and full.startswith(stem):
            return path
    return None


def old_live_match(video, by_copy):
    stems = {video.path.stem.lower()} | ({video.path.name.lower()} if video.path.suffix.lower() == pairing.MOTION_CLIP_SUFFIX else set())
    stills = {item.path: sibling for sibling, item in by_copy.items()
              if sibling.parent == video.path.parent and sibling.stem.lower() in stems and sibling.suffix.lower() in pairing.STILL_SUFFIXES}
    return next(iter(stills.items())) if len(stills) == 1 else None


def random_folder(rng, count):
    stems = [f"IMG_{rng.randint(0, max(count // 3, 1)):04d}", "PXL_20190704_083015123.MP", "a" * 40, "b" * 44 + "(1)", "IMG_1004-edited", "IMG_1004",
             "IMG_1004(1)", "photo", "photo(2)", "x" * 47, "Screenshot_2019-07-04-08-30-15-123_com.example.app"]
    media_names, sidecars = set(), set()
    for _ in range(count):
        stem = rng.choice(stems) + rng.choice(["", "", "(1)", "(2)", "-edited"])
        full = stem + rng.choice([".jpg", ".JPG", ".heic", ".mp4", ".MOV", ".png", ".mp"])
        media_names.add(full)
        choice = rng.randrange(9)
        if choice == 0:
            sidecars.add(f"{full}.json")
        elif choice == 1:
            sidecars.add(f"{full}.supplemental-metadata.json")
        elif choice == 2:
            sidecars.add(f"{(full + '.supplemental-metadata')[:46]}.json")
        elif choice == 3:
            sidecars.add(f"{stem}.json")
        elif choice == 4:
            sidecars.add(f"{full}.supplemental-me{rng.choice(['.json', '.JSON'])}")
        elif choice == 5:
            sidecars.add(f"{stem}.metadata.json")
        elif choice == 6 and "(" in stem:
            sidecars.add(f"{stem.split('(')[0]}{Path(full).suffix}.supplemental-metadata({rng.choice('123')}).json")
        elif choice == 7:
            sidecars.add(f"{(stem.split('(')[0] + Path(full).suffix + '.supplemental-metadata')[:43]}(1).json")
    return sorted(media_names), sorted(sidecars)


class IndexedRuleTests(unittest.TestCase):
    def test_the_indexed_rules_choose_what_the_sorted_scan_chose(self):
        rng = random.Random(556)
        for trial in range(300):
            folder = Path(f"Photos from {2000 + trial % 20}")
            names, sidecar_names = random_folder(rng, rng.randint(1, 50))
            sidecars = {name: folder / name for name in sidecar_names}
            indexed = pairing.IndexedSidecars(sidecars)
            for name in names:
                for old, new in ((old_supplemental, pairing.supplemental), (old_counter, pairing.counter), (old_truncated, pairing.truncated)):
                    with self.subTest(trial=trial, name=name, rule=new.__name__):
                        self.assertEqual(new(folder / name, sidecars), old(folder / name, sidecars))
                        self.assertEqual(new(folder / name, indexed), old(folder / name, sidecars))
                fits = frozenset(names)
                with self.subTest(trial=trial, name=name, rule="without_extension"):
                    self.assertEqual(pairing.without_extension(folder / name, indexed, fits),
                                     pairing.exclusive(pairing.fits_without_extension)(folder / name, sidecars, fits))
                with self.subTest(trial=trial, name=name, rule="cut_counter"):
                    self.assertEqual(pairing.cut_counter(folder / name, indexed, fits),
                                     pairing.exclusive(pairing.fits_cut_counter)(folder / name, sidecars, fits))

    def test_a_sidecar_two_media_files_fit_without_their_extension_goes_to_neither(self):
        folder = Path("Photos from 2019")
        sidecars = pairing.IndexedSidecars({"IMG_1.json": folder / "IMG_1.json"})
        self.assertIsNone(pairing.without_extension(folder / "IMG_1.jpg", sidecars, frozenset({"IMG_1.jpg", "IMG_1.png"})))
        self.assertEqual(pairing.without_extension(folder / "IMG_1.jpg", sidecars, frozenset({"IMG_1.jpg"})), folder / "IMG_1.json")

    def test_the_first_fitting_name_in_sorted_order_wins_whatever_the_json_case(self):
        folder = Path("Photos from 2019")
        names = ["IMG_1.jpg.supplemental-metadata.json", "IMG_1.jpg.s.JSON", "IMG_1.jpg.supplemental.json"]
        sidecars = {name: folder / name for name in names}
        self.assertEqual(pairing.supplemental(folder / "IMG_1.jpg", sidecars), folder / sorted(names)[0])

    def test_a_video_finds_its_one_still_by_folder_and_stem_as_the_scan_did(self):
        rng = random.Random(7)
        for trial in range(200):
            items, by_copy = [], {}
            for _ in range(rng.randint(1, 30)):
                folder = Path(rng.choice(["Photos from 2019", "Album", "Trash"]))
                name = f"{rng.choice(['IMG_1', 'img_1', 'PXL_2', 'IMG_1.MP'])}{rng.choice(['.jpg', '.HEIC', '.mp4', '.MP', '.mov', '.png'])}"
                item = pairing.TakeoutItem(folder / name, str(rng.randint(0, 5)), (), None)
                items.append(item)
                by_copy.setdefault(rng.choice(items).path if rng.random() < 0.2 else item.path, item)
            stills = pairing.stills_by_stem(by_copy)
            for video in items:
                with self.subTest(trial=trial, video=str(video.path)):
                    self.assertEqual(pairing.live_match(video, by_copy, stills), old_live_match(video, by_copy))
                    self.assertEqual(pairing.live_match(video, by_copy), old_live_match(video, by_copy))


class PairingScaleTests(unittest.TestCase):
    def test_a_folder_of_twenty_thousand_files_pairs_in_linear_time(self):
        folder = Path("Takeout/Google Photos/Photos from 2019")
        names = [f"PXL_{n:06d}.jpg" for n in range(18_000)] + [f"VID_{n:06d}.mp4" for n in range(1_500)] + [f"IMG_{n:06d}.HEIC" for n in range(500)]
        sidecars = [f"{name}.supplemental-metadata.json" for name in names[:19_500]]
        digests = {folder / name: str(index) for index, name in enumerate(names)}
        started = time.monotonic()
        report = pairing.pair_names([(folder, names)], digests, {folder: sidecars}, reading=lambda path: None)
        self.assertLess(time.monotonic() - started, 30.0)
        self.assertEqual(len(report.items), 20_000)
        self.assertEqual(len(report.unpaired_media), 500)


class DecodeBoundTests(unittest.TestCase):
    def test_a_file_whose_fingerprint_never_returns_fails_alone_and_the_part_finishes(self):
        world = PartWorld(self)
        export = world.create(world.scenario)
        release = threading.Event()
        self.addCleanup(release.set)
        real = media.decode

        def hangs(path, named=None):
            if named is not None and named.name == "IMG_2001.JPG":
                release.wait(120)
            return real(path, named)

        started = time.monotonic()
        with (mock.patch.object(media, "decode", side_effect=hangs), mock.patch.object(part_ingest, "DECODE_FILE_SECONDS", 2.0),
              mock.patch.object(part_ingest, "DECODE_BATCH_SECONDS", 5.0), mock.patch.object(part_ingest.monitoring, "log", wraps=monitoring.log) as log):
            report = world.ingest(export, 1)
        self.assertLess(time.monotonic() - started, 60.0)
        self.assertTrue(report.ingested)
        self.assertEqual(world.outcomes()["Photos from 2019/IMG_2001.JPG"], "failed")
        self.assertEqual(world.part(export, 1).ingest_state, "ingested")
        failed = [row.detail for row in world.opened.execute(select(Event).where(Event.type == "import_item_failed")).scalars()]
        self.assertEqual(failed, ["metadata: DecodeTimedOut"])
        left = [kwargs for event, kwargs in logged_lines(log) if event == "fingerprint workers left behind"]
        self.assertEqual([(each["stuck"], each["part"]) for each in left], [(1, 1)])

    def test_the_wait_for_each_batch_is_bounded(self):
        self.assertEqual(part_ingest.decode_seconds(1), part_ingest.DECODE_FILE_SECONDS)
        self.assertEqual(part_ingest.decode_seconds(0), part_ingest.DECODE_FILE_SECONDS)
        self.assertEqual(part_ingest.decode_seconds(10_000), part_ingest.DECODE_BATCH_SECONDS)


class HeartbeatTests(unittest.TestCase):
    def private_world(self):
        world = PartWorld(self)

        def build(builder):
            world.scenario(builder)
            builder.photo(PRIVATE_NAME, part=0, description=PRIVATE_TEXT)
        return world, world.create(build)

    def test_an_import_tick_logs_its_start_its_steps_each_part_and_its_progress_with_no_private_data(self):
        world, export = self.private_world()
        scheduler = jobs.build(world.settings, world.engine, NOW)
        with (no_leaks(self, PRIVATE_NAME, PRIVATE_TEXT, "Canary-Private-Name"), mock.patch.object(heartbeat, "ITEMS_BETWEEN_LOGS", 1),
              mock.patch.object(monitoring, "log", wraps=monitoring.log) as log):
            scheduler.run_one("imports", NOW)
        lines = logged_lines(log)
        events = [event for event, _ in lines]
        self.assertEqual(events[0], "import tick started")
        self.assertEqual(lines[0][1], {"waiting": 0, "parts": 3})
        self.assertEqual(events[-1], "import tick finished")
        self.assertTrue(lines[-1][1]["completed"])
        steps = [kwargs["step"] for event, kwargs in lines if event == "import tick step"]
        self.assertEqual(steps, ["whole exports", "freeing zips", "part ingest", "finishing exports"])
        started = [(kwargs["export"], kwargs["part"], kwargs["resumed"]) for event, kwargs in lines if event == "part ingest started"]
        self.assertEqual(started, [(export.id, 1, False), (export.id, 2, False), (export.id, 3, False)])
        part_steps = [kwargs["step"] for event, kwargs in lines if event == "part ingest step" and kwargs["part"] == 1]
        self.assertEqual(part_steps, ["testing the zip", "reading sidecars", "streaming", "settling", "finishing"])
        progress = [kwargs["done"] for event, kwargs in lines if event == "part ingest progress" and kwargs["part"] == 1]
        self.assertTrue(progress)
        self.assertEqual(progress, sorted(set(progress)))
        self.assertLessEqual(progress[-1], world.part(export, 1).entries_done)
        for _, kwargs in lines:
            self.assertTrue(all(isinstance(value, (int, bool)) or value.isascii() for value in kwargs.values()))

    def test_progress_is_logged_every_n_items_or_every_few_minutes_and_never_when_nothing_moved(self):
        clock = Clock()
        beat = heartbeat.Heartbeat(clock)
        with mock.patch.object(heartbeat.monitoring, "log") as log:
            beat.part_began(3, 2, 2478, 9008, True)
            for count in range(2479, 2479 + heartbeat.ITEMS_BETWEEN_LOGS):
                beat.counted(count, 9008)
            clock.now += heartbeat.SECONDS_BETWEEN_LOGS
            beat.counted(2478 + heartbeat.ITEMS_BETWEEN_LOGS, 9008)
            beat.counted(2479 + heartbeat.ITEMS_BETWEEN_LOGS, 9008)
        progress = [kwargs["done"] for event, kwargs in logged_lines(log) if event == "part ingest progress"]
        self.assertEqual(progress, [2478 + heartbeat.ITEMS_BETWEEN_LOGS, 2479 + heartbeat.ITEMS_BETWEEN_LOGS])
        self.assertEqual(logged_lines(log)[0], ("part ingest started", {"export": 3, "part": 2, "done": 2478, "entries": 9008, "resumed": True}))

    def test_a_tick_that_fails_still_logs_that_it_ended(self):
        beat = heartbeat.Heartbeat(Clock())
        with mock.patch.object(heartbeat.monitoring, "log") as log, self.assertRaises(RuntimeError):
            with beat.ticking(parts=1):
                raise RuntimeError("boom")
        self.assertEqual(logged_lines(log)[-1], ("import tick finished", {"seconds": 0, "completed": False}))
        self.assertEqual(beat.snapshot(), heartbeat.Snapshot(None, None, None, None))


class StillRunningTests(unittest.TestCase):
    def test_a_job_whose_thread_is_still_busy_says_so_every_ten_minutes_with_what_it_is_doing(self):
        scheduler = jobs.Scheduler()
        release = threading.Event()
        self.addCleanup(release.set)
        scheduler.every("imports", 60, lambda now: release.wait(30), own_thread=True, describe=lambda: {"step": "part ingest", "part": 2, "done": 2478})
        with mock.patch.object(jobs.monitoring, "log") as log:
            for now in (0, 60, 599, 600, 900, 1199, 1200):
                scheduler.run_due(now)
        noted = [(kwargs["seconds"], kwargs["step"], kwargs["done"]) for event, kwargs in logged_lines(log) if event == "job still running"]
        self.assertEqual(noted, [(600, "part ingest", 2478), (1200, "part ingest", 2478)])
        release.set()
        scheduler.jobs["imports"].worker.join(10)


class StallWatchTests(unittest.TestCase):
    def setUp(self):
        self.world = PartWorld(self)
        self.export = self.world.create(self.world.scenario)
        part = self.world.part(self.export, 2)
        part.ingest_state, part.entries, part.entries_done = "ingesting", 9008, 2478
        self.world.opened.commit()
        self.clock = Clock()
        self.beat = heartbeat.Heartbeat(self.clock)
        self.watch = heartbeat.StallWatch()
        self.limit = 60.0 * config.import_stall_minutes(self.world.settings)

    def review(self, at):
        self.world.opened.expire_all()
        found = self.watch.review(self.world.opened, self.world.settings, self.beat, at)
        self.world.opened.commit()
        return found

    def open_alerts(self):
        self.world.opened.expire_all()
        return [(row.key, row.kind) for row in self.world.opened.execute(select(Alert).where(Alert.kind == heartbeat.STALLED_KIND, Alert.closed.is_(None)))
                .scalars()]

    def set_count(self, count, state="ingesting"):
        part = self.world.part(self.export, 2)
        part.entries_done, part.ingest_state = count, state
        self.world.opened.commit()

    def working(self):
        with mock.patch.object(heartbeat.monitoring, "log"):
            ticking = self.beat.ticking()
            ticking.__enter__()
            self.addCleanup(lambda: ticking.__exit__(None, None, None))
            self.beat.part_began(self.export.id, 2, 2478, 9008, True)

    def test_a_part_whose_count_stands_still_for_the_stall_time_opens_exactly_one_alert(self):
        self.working()
        self.assertEqual(self.review(NOW), [])
        self.assertEqual(self.review(NOW + self.limit - 1), [])
        self.assertEqual(self.open_alerts(), [])
        with no_leaks(self, PRIVATE_NAME), mock.patch.object(heartbeat.monitoring, "log", wraps=monitoring.log) as log:
            self.assertEqual(self.review(NOW + self.limit), [(self.export.id, 2)])
        self.assertEqual(self.review(NOW + self.limit + 60), [(self.export.id, 2)])
        self.assertEqual(self.open_alerts(), [(f"part:stalled:{self.export.id}:2", "part_stalled")])
        lines = logged_lines(log)
        self.assertEqual([kwargs for event, kwargs in lines if event == "part ingest stalled"],
                         [{"export": self.export.id, "part": 2, "minutes": 30, "step": "none"}])
        stacks = [kwargs for event, kwargs in lines if event == "stalled import stack"]
        self.assertEqual(stacks[0]["thread"], "import")
        self.assertIn("test_ingest_stall.py", stacks[0]["frames"])
        alert = self.world.opened.execute(select(Alert).where(Alert.kind == heartbeat.STALLED_KIND)).scalar_one()
        self.assertEqual(alert.message, f"Part 2 of export {self.export.id} has imported nothing for 30 minutes although Darkroom is still working on it. "
                         "Nothing was lost; the logs say which step it is in. The alert closes once the count moves again.")

    def test_a_part_whose_count_moves_opens_none_and_moving_again_closes_an_open_one(self):
        self.working()
        self.review(NOW)
        for step in range(1, 5):
            self.set_count(2478 + step)
            self.assertEqual(self.review(NOW + step * self.limit * 0.75), [])
        self.assertEqual(self.open_alerts(), [])
        self.review(NOW + 10 * self.limit)
        self.assertEqual(len(self.open_alerts()), 1)
        self.set_count(2500)
        self.assertEqual(self.review(NOW + 10 * self.limit + 60), [])
        self.assertEqual(self.open_alerts(), [])

    def test_a_finished_part_or_one_the_tick_is_not_working_on_opens_none_and_closes_its_alert(self):
        self.assertEqual(self.review(NOW), [])
        self.assertEqual(self.review(NOW + 2 * self.limit), [])
        self.working()
        self.review(NOW + 2 * self.limit)
        self.review(NOW + 4 * self.limit)
        self.assertEqual(len(self.open_alerts()), 1)
        self.set_count(9008, state="ingested")
        self.assertEqual(self.review(NOW + 4 * self.limit + 60), [])
        self.assertEqual(self.open_alerts(), [])
        self.assertEqual(self.review(NOW + 10 * self.limit), [])

    def test_the_stall_time_is_configurable_and_held_to_a_bounded_range(self):
        self.assertEqual(settings_from().import_stall_minutes, 30)
        for raw, used in (("10", 10), ("1", 5), ("0", 5), ("junk", 5), ("100000", 1440)):
            with self.subTest(raw=raw):
                settings = settings_from(DARKROOM_IMPORT_STALL_MINUTES=raw)
                self.assertEqual(config.import_stall_minutes(settings), used)
                self.assertEqual(bool(config.import_stall_problems(settings)), raw != "10")
        self.assertEqual(config.import_stall_problems(settings_from()), [])
        shorter = dataclasses.replace(self.world.settings, import_stall_minutes=10)
        self.working()
        self.watch.review(self.world.opened, shorter, self.beat, NOW)
        self.assertEqual(self.watch.review(self.world.opened, shorter, self.beat, NOW + 600), [(self.export.id, 2)])


if __name__ == "__main__":
    unittest.main()
