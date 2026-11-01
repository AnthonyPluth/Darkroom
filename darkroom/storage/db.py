from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from darkroom.config import Settings
from darkroom.storage.models import Base

__all__ = ["MIGRATIONS", "Base", "alembic_config", "connect", "create_fresh", "database_url", "make_engine", "migrate", "session", "upsert"]

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def database_url(settings: Settings) -> str:
    return settings.database_url or f"sqlite:///{settings.data_dir / 'darkroom.db'}"


def set_sqlite_pragmas(dbapi_connection: sqlite3.Connection, record: object) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def make_engine(url: str) -> Engine:
    engine = create_engine(url, connect_args={"timeout": 30} if url.startswith("sqlite") else {})
    if url.startswith("sqlite"):
        event.listen(engine, "connect", set_sqlite_pragmas)
    return engine


@contextmanager
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine, expire_on_commit=False) as opened:
        try:
            yield opened
            opened.commit()
        except BaseException:
            opened.rollback()
            raise


@contextmanager
def connect(engine: Engine) -> Iterator[Connection]:
    with engine.begin() as opened:
        yield opened


def create_fresh(url: str = "sqlite://") -> Engine:
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    return engine


def upsert(opened: Session, model: type[Base], values: dict[str, Any], keys: tuple[str, ...]) -> None:
    statement = sqlite_insert(model).values(**values)
    updates = {name: value for name, value in values.items() if name not in keys}
    opened.execute(statement.on_conflict_do_update(index_elements=list(keys), set_=updates) if updates else statement.on_conflict_do_nothing(index_elements=list(keys)))


def alembic_config(engine: Engine | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    if engine is not None:
        config.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False))
    return config


def migrate(engine: Engine) -> None:
    config = alembic_config(engine)
    with engine.begin() as opened:
        config.attributes["connection"] = opened
        command.upgrade(config, "head")
