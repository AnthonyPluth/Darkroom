from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from darkroom.storage import db


def own_database(test: unittest.TestCase) -> Engine:
    folder = tempfile.TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    engine = db.make_engine(f"sqlite:///{Path(folder.name) / 'test.db'}")
    db.Base.metadata.create_all(engine)
    test.addCleanup(engine.dispose)
    return engine


def add_person(opened: Session, slug: str, name: str = "") -> int:
    from darkroom.storage.models import Person

    person = Person(name=name or slug.title(), slug=slug, originals_path=f"{slug}/originals", google_only_path=f"{slug}/google-only", created=1.0)
    opened.add(person)
    opened.flush()
    return person.id
