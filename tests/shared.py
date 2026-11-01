from __future__ import annotations

import dataclasses
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from unittest import mock

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from darkroom import config
from darkroom.server import handler
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
