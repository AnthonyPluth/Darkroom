import itertools
import json
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


def media(name, folder=YEAR, edited_of=None):
    return {"name": f"{GOOGLE_PHOTOS}/{folder}/{name}", "edited_of": None if edited_of is None else f"{GOOGLE_PHOTOS}/{folder}/{edited_of}"}


def sidecar(name, title, folder=YEAR):
    return {"folder": f"{GOOGLE_PHOTOS}/{folder}", "name": name, "text": json.dumps({"title": title})}


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
}
LAYOUTS = {
    "together": {name: (1, 1) for name in SUBJECTS},
    "sidecars first": {name: (2, 1) for name in SUBJECTS},
    "sidecars last": {name: (1, 3) for name in SUBJECTS},
    "mixed": dict(zip(SUBJECTS, ((1, 2), (3, 1), (2, 2), (1, 3), (3, 2), (2, 1), (1, 1), (2, 3)), strict=True)),
    "split pairs": dict(zip(SUBJECTS, ((2, 3), (1, 1), (3, 3), (2, 1), (1, 2), (3, 1), (3, 2), (1, 2)), strict=True)),
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
        self.pending: dict[int, list[TakeoutEntry]] = {part: [] for part in PARTS}
        self.pending_sidecars: dict[int, list[TakeoutSidecar]] = {part: [] for part in PARTS}
        for subject, parts in layout.items():
            files, sidecars = SUBJECTS[subject]
            media_parts, sidecar_parts = spread(subject, parts)
            for found, part in zip(files, media_parts, strict=True):
                self.add_media(part, found["name"], found["edited_of"])
            for found, part in zip(sidecars, sidecar_parts, strict=True):
                self.add_sidecar(part, found)
        if repeated_path:
            self.add_media(1, f"{GOOGLE_PHOTOS}/{YEAR}/IMG_9.JPG", None)
            self.add_media(3, f"{GOOGLE_PHOTOS}/{YEAR}/IMG_9.JPG", None)
            self.add_sidecar(2, sidecar("IMG_9.JPG.json", "IMG_9.JPG"))

    def entry(self, part, name, kind, edited_of=None):
        number = len(self.all_entries) + 1
        row = TakeoutEntry(id=number, export_id=1, part_number=part, name=name, kind=kind, bytes=1, crc32=1, outcome="listed",
                           metadata_pending=False, sha1=None, is_video=False, is_edited=edited_of is not None, edited_of=edited_of,
                           copy_of_id=None, sidecar_entry_id=None, listed_at=1.0)
        self.all_entries.append(row)
        return row

    def add_media(self, part, name, edited_of):
        row = self.entry(part, name, "media", edited_of)
        self.pending[part].append(row)

    def add_sidecar(self, part, found):
        carrier = self.entry(part, f"{found['folder']}/{found['name']}", "sidecar")
        row = TakeoutSidecar(id=carrier.id, export_id=1, part_number=part, entry_id=carrier.id, folder=found["folder"], name=found["name"],
                             text=found["text"])
        self.pending_sidecars[part].append(row)


def index_of_nothing(ledger):
    return dedupe.PersonIndex(account_id=1, default_person_id=1, person_ids=frozenset({1}), phone=())


def index_of_everything(ledger):
    shots = tuple(
        dedupe.Shot(id=row.id, sha1=sha1_of(row), person_id=1, source="phone", phash=None, captured_utc=None, captured_local=None, width=None, height=None,
                    duration_ms=None, make=None, model=None, is_video=False, is_raw=False)
        for row in ledger.all_entries if row.kind == "media"
    )
    return dedupe.PersonIndex(account_id=1, default_person_id=1, person_ids=frozenset({1}), phone=shots)


INDEXES = {"nothing matches": index_of_nothing, "every entry is tier 1": index_of_everything}


def sha1_of(row):
    return f"{row.id:040d}"


def act_on(result):
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
    digests = {Path(row.name): str(row.id) for row in media_rows}
    texts = {Path(found.folder) / found.name: found.text for found in sidecars}
    owners = {Path(found.folder) / found.name: found.entry_id for found in sidecars}
    report = pairing.pair_names(sorted(folders.items()), digests, reading=texts.get)
    expected = {}
    for item in report.items:
        wanted = None if item.sidecar is None or item.sidecar_from_still else owners[item.sidecar]
        for copy in item.copies:
            expected[copy.as_posix()] = wanted
    return expected


def run(ledger, order, batch, listed_first, index):
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
        here = ledger.pending[part]
        for start in range(0, max(len(here), 1), batch):
            for row in here[start:start + batch]:
                row.sha1 = sha1_of(row)
            act_on(part_pairing.pair_part(entries, sidecars, part, index))
    return entries


class PartPairingEquivalenceTests(unittest.TestCase):
    def check(self, layout, repeated, order, batch, listed_first, index):
        ledger = Ledger(layout, repeated)
        expected = whole_export(ledger)
        for row in run(ledger, order, batch, listed_first, index(ledger)):
            if row.kind == "media":
                self.assertEqual(row.sidecar_entry_id, expected[row.name], row.name)

    def test_every_arrival_order_and_batch_split_pairs_as_the_whole_export_does(self):
        checked = 0
        for layout_name, layout in LAYOUTS.items():
            for repeated in (False, True):
                for order in itertools.permutations(PARTS):
                    for batch in BATCHES:
                        for listed_first in (False, True):
                            for index_name, index in INDEXES.items():
                                with self.subTest(layout=layout_name, repeated=repeated, order=order, batch=batch, listed_first=listed_first, index=index_name):
                                    self.check(layout, repeated, order, batch, listed_first, index)
                                    checked += 1
        self.assertEqual(checked, len(LAYOUTS) * 2 * 6 * len(BATCHES) * 2 * len(INDEXES))

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


if __name__ == "__main__":
    unittest.main()
