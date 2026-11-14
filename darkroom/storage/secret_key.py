from __future__ import annotations

import dataclasses
import os
import secrets
import tempfile
from pathlib import Path

from sqlalchemy import Engine, func, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, OperationalError

from darkroom import config, monitoring
from darkroom.storage import db, secretbox
from darkroom.storage.models import AuthSession, Person

KEY_BYTES = 32
SEALED_COLUMNS = ((Person, Person.immich_api_key_sealed), (AuthSession, AuthSession.refresh_token_sealed))
UNREADABLE_FILE = ("the secret-key file in DARKROOM_DATA_DIR is there but holds no key Darkroom can read, and Darkroom will not replace it; "
                   "put the key back in it, or set DARKROOM_SECRET_KEY to the key the stored values were sealed with")
SEALED_WITHOUT_KEY = ("there is no secret key, but sealed values are already stored (an Immich key, a sign-in session or RESEND_API_KEY), "
                      "and Darkroom will not make a new key that could never read them; set DARKROOM_SECRET_KEY to the key they were sealed with, "
                      "or put the secret-key file back in DARKROOM_DATA_DIR")
DATABASE_UNREACHABLE = "the database could not be reached to check for sealed values, so whether a secret key can be generated is not known"


class Refused(Exception):
    pass


def sealed_in_database(engine: Engine) -> bool:
    present = inspect(engine)
    with db.session(engine) as opened:
        for model, column in SEALED_COLUMNS:
            if present.has_table(model.__tablename__) and opened.scalar(select(func.count()).select_from(model).where(column.is_not(None), column != "")):
                return True
    return False


def sealed_values_exist(settings: config.Settings, engine: Engine) -> bool:
    return secretbox.is_sealed(settings.resend_api_key) or sealed_in_database(engine)


def database_present(settings: config.Settings) -> bool:
    url = make_url(db.normalise_url(db.database_url(settings)))
    return url.get_backend_name() != "sqlite" or bool(url.database and Path(url.database).is_file())


def missing_key_problem(settings: config.Settings) -> str | None:
    if settings.secret_key:
        return None
    if key_file_present(settings):
        return UNREADABLE_FILE
    if secretbox.is_sealed(settings.resend_api_key):
        return SEALED_WITHOUT_KEY
    try:
        if not database_present(settings):
            return None
        engine = db.make_engine(db.database_url(settings))
    except (ArgumentError, ValueError):
        return DATABASE_UNREACHABLE
    try:
        sealed = sealed_in_database(engine)
    except OperationalError:
        return DATABASE_UNREACHABLE
    finally:
        engine.dispose()
    return SEALED_WITHOUT_KEY if sealed else None


def key_file_present(settings: config.Settings) -> bool:
    path = config.secret_key_file(settings.data_dir)
    return path.exists() or path.is_symlink()


def write_new_key(path: Path) -> str:
    key = secrets.token_urlsafe(KEY_BYTES)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w", encoding="ascii") as out:
            out.write(key + "\n")
            out.flush()
            os.fsync(out.fileno())
        os.link(temporary, path)
    finally:
        os.unlink(temporary)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return key


def ensure(settings: config.Settings, engine: Engine) -> config.Settings:
    if settings.secret_key:
        return settings
    if key_file_present(settings):
        raise Refused(UNREADABLE_FILE)
    if sealed_values_exist(settings, engine):
        raise Refused(SEALED_WITHOUT_KEY)
    try:
        key = write_new_key(config.secret_key_file(settings.data_dir))
    except FileExistsError:
        key = config.stored_secret_key(settings.data_dir)
        if not key:
            raise Refused(UNREADABLE_FILE) from None
    else:
        monitoring.log("secret key generated")
    return dataclasses.replace(settings, secret_key=key, secret_key_source="file")
