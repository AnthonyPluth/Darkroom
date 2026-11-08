from __future__ import annotations

import dataclasses
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from email.message import Message
from pathlib import Path
from unittest import mock

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from darkroom import config
from darkroom.server import handler
from darkroom.storage import db


TEST_DATABASE_URL = "TEST_DATABASE_URL"
CREATE_TRIES = 50


def on_postgres() -> bool:
    return bool(os.environ.get(TEST_DATABASE_URL))


def own_database_url(test: unittest.TestCase) -> str:
    server = os.environ.get(TEST_DATABASE_URL)
    if not server:
        folder = tempfile.TemporaryDirectory()
        test.addCleanup(folder.cleanup)
        return f"sqlite:///{Path(folder.name) / 'test.db'}"
    admin = create_engine(db.normalise_url(server), isolation_level="AUTOCOMMIT")
    test.addCleanup(admin.dispose)
    name = f"darkroom_test_{uuid.uuid4().hex}"
    for attempt in range(CREATE_TRIES):
        try:
            with admin.connect() as connection:
                connection.exec_driver_sql(f'CREATE DATABASE "{name}" TEMPLATE template0')
            break
        except OperationalError:
            if attempt == CREATE_TRIES - 1:
                raise
            time.sleep(0.1)

    def drop() -> None:
        with admin.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')

    test.addCleanup(drop)
    return make_url(db.normalise_url(server)).set(database=name).render_as_string(hide_password=False)


def own_database(test: unittest.TestCase) -> Engine:
    engine = db.make_engine(own_database_url(test))
    db.Base.metadata.create_all(engine)
    test.addCleanup(engine.dispose)
    return engine


def own_sqlite_database(test: unittest.TestCase) -> Engine:
    folder = tempfile.TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    engine = db.make_engine(f"sqlite:///{Path(folder.name) / 'test.db'}")
    db.Base.metadata.create_all(engine)
    test.addCleanup(engine.dispose)
    return engine


def impatient_engine(test: unittest.TestCase, engine: Engine, seconds: float) -> Engine:
    if engine.dialect.name == "sqlite":
        other = create_engine(engine.url, connect_args={"timeout": seconds})
        event.listen(other, "connect", db.set_sqlite_pragmas)
    else:
        other = create_engine(engine.url, connect_args={"options": f"-c lock_timeout={int(seconds * 1000)}"})
        db.serialise_writes(other)
    test.addCleanup(other.dispose)
    return other


def try_elsewhere(test: unittest.TestCase, engine: Engine, work: Callable[[Session], object]) -> str:
    other = impatient_engine(test, engine, 0.1)
    try:
        with db.session(other) as opened:
            work(opened)
    except OperationalError as error:
        if not db.is_busy(error):
            raise
        return "locked"
    return "committed"


def write_elsewhere(test: unittest.TestCase, engine: Engine, statement: str, **values: object) -> str:
    return try_elsewhere(test, engine, lambda opened: opened.execute(text(statement), values))


@contextmanager
def another_writer_holds_the_lock(engine: Engine) -> Iterator[None]:
    if engine.dialect.name == "sqlite":
        holder = sqlite3.connect(engine.url.database, timeout=0, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        try:
            yield
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        return
    with engine.connect() as connection:
        connection.exec_driver_sql(f"SELECT pg_advisory_xact_lock({db.WRITE_LOCK_KEY})")
        try:
            yield
        finally:
            connection.rollback()


def add_person(opened: Session, slug: str, name: str = "") -> int:
    from darkroom.storage.models import Person

    person = Person(name=name or slug.title(), slug=slug, originals_path=f"{slug}/originals", google_only_path=f"{slug}/google-only", created=1.0)
    opened.add(person)
    opened.flush()
    return person.id


def settings_from(**values: str) -> config.Settings:
    with mock.patch.dict(os.environ, values, clear=True):
        return config.load()


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(base: str, method: str, path: str, headers: dict[str, str] | None = None, body: bytes | None = None) -> tuple[int, Message, bytes]:
    request = urllib.request.Request(base + path, data=body, headers=headers or {}, method=method)
    opener = urllib.request.build_opener(NoRedirects())
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        with error:
            return error.code, error.headers, error.read()


def start_server(test: unittest.TestCase, settings: config.Settings, engine: Engine | None = None) -> tuple[str, config.Settings]:
    server = handler.make_server(settings, host="127.0.0.1", port=0, engine=engine or own_database(test))
    base = f"http://127.0.0.1:{server.server_address[1]}"
    bound = dataclasses.replace(settings, public_url=settings.public_url or base)
    server.RequestHandlerClass.settings = bound
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def stop() -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    test.addCleanup(stop)
    return base, bound


def cookies_set(headers: Message) -> dict[str, str]:
    found = {}
    for line in headers.get_all("Set-Cookie") or []:
        name, _, value = line.split(";")[0].partition("=")
        found[name.strip()] = value
    return found
