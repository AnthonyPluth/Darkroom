import itertools
import json
from collections import Counter
import unittest
from pathlib import Path

from darkroom.domain import dedupe
from darkroom.domain.ingest import pairing, part_pairing
from darkroom.storage.models import TakeoutEntry, TakeoutSidecar
from tests.takeout_fixtures import GOOGLE_PHOTOS, SIDECAR_NAME_LIMIT

YEAR = "Photos from 2019"
CUT = "screen_capture_of_a_chat_with_a_long_title_AAA"
ONE, TWO = f"{CUT}_one.jpg", f"{CUT}_two.jpg"
CUT_NAME = ONE[:SIDECAR_NAME_LIMIT]
PARTS = (1, 2, 3)
BATCHES = (1, 2, 100)


PHONE = {"make": "Apple", "model": "iPhone 12", "width": 32, "height": 24}
TAKEN = 1_500_000_000
SHOT_WALL_CLOCK = "2017-07-13T22:40:00"


def media(name, folder=YEAR, edited_of=None, same=None, **fields):
    return {"name": f"{GOOGLE_PHOTOS}/{folder}/{name}", "edited_of": None if edited_of is None else f"{GOOGLE_PHOTOS}/{folder}/{edited_of}", "same": same,
            "fields": fields}


def sidecar(name, title, folder=YEAR, taken=None):
    data = {"title": title} | ({} if taken is None else {"photoTakenTime": {"timestamp": str(taken)}})
    return {"folder": f"{GOOGLE_PHOTOS}/{folder}", "name": name, "text": json.dumps(data)}


SUBJECTS = {
    "plain": ([media("IMG_1.JPG")], [sidecar("IMG_1.JPG.json", "IMG_1.JPG")]),
    "long names": ([media(ONE), media(TWO)], [sidecar(f"{CUT_NAME}.json", ONE), sidecar(f"{CUT_NAME}(1).json", TWO)]),
    "swapped titles": ([media(f"{CUT}_three.jpg", "Album"), media(f"{CUT}_four.jpg", "Album")],
                       [sidecar(f"{CUT_NAME}.json", f"{CUT}_four.jpg", "Album"), sidecar(f"{CUT_NAME}(1).json", f"{CUT}_three.jpg", "Album")]),
    "edited": ([media("IMG_2.JPG"), media("IMG_2-edited.JPG", edited_of="IMG_2.JPG")], [sidecar("IMG_2.JPG.json", "IMG_2.JPG")]),
    "trash": ([media("IMG_3.JPG", "Trash")], [sidecar("IMG_3.JPG.json", "IMG_3.JPG", "Trash")]),
    "numbered": ([media("IMG_5.JPG"), media("IMG_5(1).JPG")], [sidecar("IMG_5.JPG.json", "IMG_5.JPG"), sidecar("IMG_5.JPG(1).json", "IMG_5(1).JPG")]),
    "stem sidecar": ([media("IMG_7.jpg", "Stems"), media("IMG_7.mp4", "Stems")], [sidecar("IMG_7.json", "IMG_7.jpg", "Stems")]),
    "creation": ([media("IMG_8.jpg", "Made"), media("IMG_8-effects.jpg", "Made")],
                 [sidecar("IMG_8.jpg.json", "IMG_8.jpg", "Made"), sidecar("IMG_8-effects.jpg.json", "IMG_8-effects.jpg", "Made")]),
    "embedded gps, no date": ([media("IMG_6.JPG", "Gps", **PHONE)], [sidecar("IMG_6.JPG.json", "IMG_6.JPG", "Gps", taken=TAKEN)]),
    "copies": ([media("IMG_4.JPG", "Trip", same="IMG_4"), media("IMG_4.JPG", same="IMG_4"), media("IMG_4(1).JPG", "Trip", same="IMG_4"),
                media("IMG_4.JPG", "Trash", same="IMG_4")], []),
}
HASH_ORDERS = ("as listed", "reversed")
WHOLE_EXPORT_TIER_2 = {f"{GOOGLE_PHOTOS}/Gps/IMG_6.JPG"}
LAYOUTS = {
    "together": {name: (1, 1) for name in SUBJECTS},
    "sidecars first": {name: (2, 1) for name in SUBJECTS},
    "sidecars last": {name: (1, 3) for name in SUBJECTS},
    "mixed": dict(zip(SUBJECTS, ((1, 2), (3, 1), (2, 2), (1, 3), (3, 2), (2, 1), (1, 1), (2, 3), (3, 1), (2, 2)), strict=True)),
    "split pairs": dict(zip(SUBJECTS, ((2, 3), (1, 1), (3, 3), (2, 1), (1, 2), (3, 1), (3, 2), (1, 2), (1, 3), (3, 3)), strict=True)),
}


def spread(subject, parts):
    files, sidecars = SUBJECTS[subject]
    every = tuple(parts) if isinstance(parts, tuple) and len(parts) == len(files) + len(sidecars) else None
    if every is None:
        media_part, sidecar_part = parts
        every = (media_part,) * len(files) + (sidecar_part,) * len(sidecars)
    return every[:len(files)], every[len(files):]


class Ledger:
    def __init__(self, layout, repeated_path):
        self.all_entries: list[TakeoutEntry] = []
        self.same: dict[int, str] = {}
        self.pending: dict[int, list[TakeoutEntry]] = {part: [] for part in PARTS}
        self.pending_sidecars: dict[int, list[TakeoutSidecar]] = {part: [] for part in PARTS}
        for subject, parts in layout.items():
            files, sidecars = SUBJECTS[subject]
            media_parts, sidecar_parts = spread(subject, parts)
            for found, part in zip(files, media_parts, strict=True):
                row = self.add_media(part, found["name"], found["edited_of"], found["fields"])
                if found["same"] is not None:
                    self.same[row.id] = found["same"]
            for found, part in zip(sidecars, sidecar_parts, strict=True):
                self.add_sidecar(part, found)
        if repeated_path:
            self.add_media(1, f"{GOOGLE_PHOTOS}/{YEAR}/IMG_9.JPG", None, {})
            self.add_media(3, f"{GOOGLE_PHOTOS}/{YEAR}/IMG_9.JPG", None, {})
            self.add_sidecar(2, sidecar("IMG_9.JPG.json", "IMG_9.JPG"))

    def entry(self, part, name, kind, edited_of=None):
        number = len(self.all_entries) + 1
        row = TakeoutEntry(id=number, export_id=1, part_number=part, name=name, kind=kind, bytes=1, crc32=1, outcome="listed",
                           metadata_pending=False, sha1=None, is_video=False, is_edited=edited_of is not None, edited_of=edited_of,
                           copy_of_id=None, sidecar_entry_id=None, listed_at=1.0)
        self.all_entries.append(row)
        return row

    def add_media(self, part, name, edited_of, fields):
        row = self.entry(part, name, "media", edited_of)
        for key, value in fields.items():
            setattr(row, key, value)
        self.pending[part].append(row)
        return row

    def add_sidecar(self, part, found):
        carrier = self.entry(part, f"{found['folder']}/{found['name']}", "sidecar")
        row = TakeoutSidecar(id=carrier.id, export_id=1, part_number=part, entry_id=carrier.id, folder=found["folder"], name=found["name"],
                             text=found["text"])
        self.pending_sidecars[part].append(row)

    def sha1_of(self, row):
        same = self.same.get(row.id)
        return f"{row.id:040d}" if same is None else same.encode().hex().ljust(40, "0")


def index_of_nothing(ledger):
    return dedupe.PersonIndex(account_id=1, default_person_id=1, person_ids=frozenset({1}), phone=())


def index_of_everything(ledger):
    shots = tuple(
        dedupe.Shot(id=row.id, sha1=ledger.sha1_of(row), person_id=1, source="phone", phash=None, captured_utc=None, captured_local=None, width=None, height=None,
                    duration_ms=None, make=None, model=None, is_video=False, is_raw=False)
        for row in ledger.all_entries if row.kind == "media"
    )
    return dedupe.PersonIndex(account_id=1, default_person_id=1, person_ids=frozenset({1}), phone=shots)


def index_of_a_wall_clock_shot(ledger):
    shot = dedupe.Shot(id=1, sha1="f" * 40, person_id=1, source="phone", phash=None, captured_utc=None, captured_local=SHOT_WALL_CLOCK, duration_ms=None,
                       is_video=False, is_raw=False, **PHONE)
    return dedupe.PersonIndex(account_id=1, default_person_id=1, person_ids=frozenset({1}), phone=(shot,))


INDEXES = {"nothing matches": index_of_nothing, "every entry is tier 1": index_of_everything, "a phone shot on a wall clock": index_of_a_wall_clock_shot}




def act_on(result, released):
    released.update(row.name for row in result.ready)
    for row in result.ready:
        row.outcome, row.decision_id = "placed", row.id
    for row in result.metadata_due:
        row.metadata_pending = False


def whole_export(ledger):
    media_rows = [row for part in PARTS for row in ledger.pending[part]]
    sidecars = [row for part in PARTS for row in ledger.pending_sidecars[part]]
    folders: dict[Path, list[str]] = {}
    for row in media_rows:
        folders.setdefault(Path(row.name).parent, []).append(Path(row.name).name)
    for found in sidecars:
        folders.setdefault(Path(found.folder), []).append(found.name)
    digests = {Path(row.name): ledger.sha1_of(row) for row in media_rows}
    texts = {Path(found.folder) / found.name: found.text for found in sidecars}
    owners = {Path(found.folder) / found.name: found.entry_id for found in sidecars}
    report = pairing.pair_names(sorted(folders.items()), digests, reading=texts.get)
    expected = {}
    shared = {ledger.sha1_of(row) for row in media_rows if row.id in ledger.same}
    items = Counter()
    for item in report.items:
        wanted = None if item.sidecar is None or item.sidecar_from_still else owners[item.sidecar]
        for copy in item.copies:
            expected[copy.as_posix()] = wanted
        if item.sha1 in shared:
            items[item.sha1, frozenset(copy.parent for copy in item.copies)] += 1
    return expected, items


def items_of(ledger, rows):
    members: dict[int, set[Path]] = {}
    for row in rows:
        if row.kind == "media" and row.id in ledger.same:
            root = row.id if row.copy_of_id is None else row.copy_of_id
            members.setdefault(root, set()).add(Path(row.name).parent)
    return Counter((ledger.sha1_of(next(r for r in rows if r.id == root)), frozenset(folders)) for root, folders in members.items())


def run(ledger, order, batch, listed_first, index, released, hashing="as listed"):
    entries: list[TakeoutEntry] = []
    sidecars: list[TakeoutSidecar] = []
    if listed_first:
        for part in PARTS:
            entries += ledger.pending[part]
            sidecars += ledger.pending_sidecars[part]
    for part in order:
        if not listed_first:
            entries += ledger.pending[part]
            sidecars += ledger.pending_sidecars[part]
        here = ledger.pending[part] if hashing == "as listed" else list(reversed(ledger.pending[part]))
        for start in range(0, max(len(here), 1), batch):
            for row in here[start:start + batch]:
                row.sha1 = ledger.sha1_of(row)
            act_on(part_pairing.pair_part(entries, sidecars, part, index), released)
    return entries


class PartPairingEquivalenceTests(unittest.TestCase):
    def check(self, layout, repeated, order, batch, listed_first, index, hashing="as listed"):
        ledger = Ledger(layout, repeated)
        expected, items = whole_export(ledger)
        released: set[str] = set()
        rows = run(ledger, order, batch, listed_first, index(ledger), released, hashing)
        for row in rows:
            if row.kind == "media":
                self.assertEqual(row.sidecar_entry_id, expected[row.name], row.name)
        self.assertEqual(items_of(ledger, rows), items)
        if index is index_of_a_wall_clock_shot:
            self.assertEqual(released & WHOLE_EXPORT_TIER_2, set())

    def test_every_arrival_order_and_batch_split_pairs_as_the_whole_export_does(self):
        checked = 0
        for layout_name, layout in LAYOUTS.items():
            for repeated in (False, True):
                for order in itertools.permutations(PARTS):
                    for batch in BATCHES:
                        for listed_first in (False, True):
                            for index_name, index in INDEXES.items():
                                for hashing in HASH_ORDERS:
                                    with self.subTest(layout=layout_name, repeated=repeated, order=order, batch=batch, listed_first=listed_first,
                                                      index=index_name, hashing=hashing):
                                        self.check(layout, repeated, order, batch, listed_first, index, hashing)
                                        checked += 1
        self.assertEqual(checked, len(LAYOUTS) * 2 * 6 * len(BATCHES) * 2 * len(INDEXES) * len(HASH_ORDERS))

    def test_a_subjects_files_spread_over_parts_in_every_way_pair_as_the_whole_export_does(self):
        checked = 0
        for subject, (files, sidecars) in SUBJECTS.items():
            for parts in itertools.product(PARTS, repeat=len(files) + len(sidecars)):
                for order in itertools.permutations(PARTS):
                    for index_name, index in INDEXES.items():
                        with self.subTest(subject=subject, parts=parts, order=order, index=index_name):
                            self.check({subject: parts}, False, order, 1, False, index)
                            checked += 1
        self.assertEqual(checked, sum(len(PARTS) ** (len(f) + len(c)) for f, c in SUBJECTS.values()) * 6 * len(INDEXES))

    def test_copies_spread_over_parts_and_hashed_out_of_listing_order_make_the_whole_exports_items(self):
        files, sidecars = SUBJECTS["copies"]
        checked = 0
        for parts in itertools.product(PARTS, repeat=len(files) + len(sidecars)):
            for order in itertools.permutations(PARTS):
                for listed_first in (False, True):
                    for hashing in HASH_ORDERS:
                        with self.subTest(parts=parts, order=order, listed_first=listed_first, hashing=hashing):
                            self.check({"copies": parts}, False, order, 1, listed_first, index_of_nothing, hashing)
                            checked += 1
        self.assertEqual(checked, len(PARTS) ** len(files) * 6 * 2 * len(HASH_ORDERS))

    def test_an_entry_listed_first_but_hashed_after_its_copy_becomes_that_copys_item(self):
        year, album = media("IMG_10.JPG", same="IMG_10"), media("IMG_10.JPG", "Album", same="IMG_10")
        ledger = Ledger({}, False)
        for found, part in ((year, 1), (album, 2)):
            ledger.same[ledger.add_media(part, found["name"], None, {}).id] = found["same"]
        _, items = whole_export(ledger)
        rows = run(ledger, (2, 1), 1, True, index_of_nothing(ledger), set())
        self.assertEqual(items_of(ledger, rows), items)
        self.assertEqual(sum(1 for row in rows if row.copy_of_id is not None), 1)


if __name__ == "__main__":
    unittest.main()
