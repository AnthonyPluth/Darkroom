from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import Engine

import run
from darkroom import config
from darkroom.providers import syncthing
from darkroom.storage import db
from darkroom.storage.models import Pixel
from tests.fakes.syncthing import FakeSyncthing, Received

KEY = "demo-syncthing-key-not-real"
SOCKET_NAME = "syncthing.sock"
OWN_ID = syncthing.device_id_of_certificate(b"made-up certificate of the demo's Darkroom")
PHONE_ID = syncthing.device_id_of_certificate(b"made-up certificate of a demo phone waiting to pair")
PHONE_NAME = "Pixel 9a"
PHONE_ADDRESS = "192.0.2.10:22000"
PHONE_SEEN = "2026-06-15T11:59:00Z"
PENDING = "/rest/cluster/pending/devices"


class Holder(unittest.TestCase):
    def runTest(self) -> None:
        pass


class DemoSyncthing(FakeSyncthing):
    def __init__(self, socket_path: str, engine: Engine):
        super().__init__(Holder(), KEY, OWN_ID, socket_path)
        self.engine = engine
        self.waiting_reads = 0
        self.phone_paired = False

    def phone_held(self) -> bool:
        with db.session(self.engine) as opened:
            return opened.execute(select(Pixel.id).where(Pixel.syncthing_device_id == PHONE_ID).limit(1)).first() is not None

    def answer(self, received: Received) -> tuple[int, object]:
        if (received.method, received.path) == ("GET", PENDING) and received.headers.get("x-api-key") == self.key:
            held = self.phone_held()
            if self.phone_paired and not held:
                self.waiting_reads = 0
            self.phone_paired = held
            self.waiting_reads += 1
            self.pending = {PHONE_ID: {"name": PHONE_NAME, "address": PHONE_ADDRESS, "time": PHONE_SEEN}} if self.waiting_reads > 1 else {}
        return super().answer(received)


def socket_of(data_dir: Path) -> Path:
    return data_dir / SOCKET_NAME


def hand_key() -> None:
    read, write = os.pipe()
    os.write(write, KEY.encode("ascii"))
    os.close(write)
    os.environ["DARKROOM_SYNCTHING_KEY_FD"] = str(read)
    os.environ["DARKROOM_SYNCTHING"] = "1"


def start(data_dir: Path) -> DemoSyncthing:
    path = socket_of(data_dir)
    path.unlink(missing_ok=True)
    fake = DemoSyncthing(str(path), db.make_engine(db.database_url(config.load())))
    config.SYNCTHING_SOCKET = path
    hand_key()
    return fake


def main() -> int:
    start(Path(os.environ["DARKROOM_DATA_DIR"]))
    return run.serve()


if __name__ == "__main__":
    sys.exit(main())
