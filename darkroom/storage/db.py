from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, event, func, select
from sqlalchemy.dialects.postgresql import Insert as PostgresqlInsert
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import Insert as SqliteInsert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from darkroom.config import Settings
from darkroom.storage.models import Base

__all__ = [
    "MIGRATIONS", "Base", "UnsupportedDatabase", "alembic_config", "connect", "create_fresh", "database_url", "dialect_of", "is_busy", "make_engine", "migrate", "normalise_url",
    "serialise_writes", "session", "upsert", "write_lock",
]

MIGRATIONS = Path(__file__).resolve().parent / "migrations"
LOCK_WAIT_MS = 30000
RECYCLE_SECONDS = 1800
WRITE_LOCK_KEY = 0x44524B52
POSTGRESQL_PREFIXES = ("postgresql://", "postgres://")
POSTGRESQL_DRIVER_URL = "postgresql+psycopg://"
LOCK_NOT_AVAILABLE = "55P03"
WRITE_LOCK_HELD = "darkroom_write_lock_held"
WRITE_LOCK_SQL = f"SELECT pg_advisory_xact_lock({WRITE_LOCK_KEY})"
READ_ONLY_STATEMENT = re.compile(r"^\s*(?:SELECT|WITH|SHOW|SET|RESET|BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE|EXPLAIN)\b", re.IGNORECASE)
LOCKING_OR_WRITING = re.compile(r"\bFOR\s+(?:NO\s+KEY\s+)?(?:UPDATE|SHARE)\b|\b(?:INSERT|UPDATE|DELETE|MERGE)\b", re.IGNORECASE)


class UnsupportedDatabase(ValueError):
    pass


def database_url(settings: Settings) -> str:
    return settings.database_url or f"sqlite:///{settings.data_dir / 'darkroom.db'}"


def set_sqlite_pragmas(dbapi_connection: sqlite3.Connection, record: object) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def normalise_url(url: str) -> str:
    for prefix in POSTGRESQL_PREFIXES:
        if url.startswith(prefix):
            return POSTGRESQL_DRIVER_URL + url[len(prefix):]
    return url


def unsupported(name: str) -> UnsupportedDatabase:
    return UnsupportedDatabase(f"Darkroom supports SQLite (sqlite:///path) and PostgreSQL (postgresql://user:password@host/database), not {name!r}")


def make_engine(url: str) -> Engine:
    url = normalise_url(url)
    name = make_url(url).get_backend_name()
    if name == "sqlite":
        engine = create_engine(url, connect_args={"timeout": 30})
        event.listen(engine, "connect", set_sqlite_pragmas)
        return engine
    if name == "postgresql":
        engine = create_engine(url, pool_pre_ping=True, pool_recycle=RECYCLE_SECONDS, connect_args={"options": f"-c lock_timeout={LOCK_WAIT_MS}"})
        serialise_writes(engine)
        return engine
    raise unsupported(name)


def writes(statement: str) -> bool:
    return READ_ONLY_STATEMENT.match(statement) is None or LOCKING_OR_WRITING.search(statement) is not None


def forget_write_lock(connection: Connection) -> None:
    connection.info.pop(WRITE_LOCK_HELD, None)


def take_write_lock_before_writing(connection: Connection, cursor: object, statement: str, parameters: object, context: object, executemany: bool) -> None:
    if connection.info.get(WRITE_LOCK_HELD) or not writes(statement):
        return
    connection.exec_driver_sql(WRITE_LOCK_SQL)
    connection.info[WRITE_LOCK_HELD] = True


def serialise_writes(engine: Engine) -> None:
    if engine.dialect.name != "postgresql":
        return
    event.listen(engine, "begin", forget_write_lock)
    event.listen(engine, "before_cursor_execute", take_write_lock_before_writing)


def dialect_of(opened: Session | Connection) -> str:
    return opened.get_bind().dialect.name if isinstance(opened, Session) else opened.dialect.name


def is_busy(error: OperationalError) -> bool:
    return "database is locked" in str(error.orig) or getattr(error.orig, "sqlstate", None) == LOCK_NOT_AVAILABLE


def write_lock(opened: Session) -> None:
    if dialect_of(opened) == "postgresql":
        opened.execute(select(func.pg_advisory_xact_lock(WRITE_LOCK_KEY)))


@contextmanager
def session(engine: Engine, writing: bool = False) -> Iterator[Session]:
    with Session(engine, expire_on_commit=False) as opened:
        try:
            if writing:
                write_lock(opened)
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


def insert_for(dialect: str, model: type[Base]) -> SqliteInsert | PostgresqlInsert:
    if dialect == "sqlite":
        return sqlite_insert(model)
    if dialect == "postgresql":
        return postgresql_insert(model)
    raise unsupported(dialect)


def upsert(opened: Session, model: type[Base], values: dict[str, Any], keys: tuple[str, ...]) -> None:
    statement = insert_for(dialect_of(opened), model).values(**values)
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
