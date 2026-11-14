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
UNREADABLE_FILE_NOTHING_SEALED = ("the secret-key file in DARKROOM_DATA_DIR is there but holds no key Darkroom can read, and Darkroom will not replace it; "
                                  "no sealed values are stored in this database or in RESEND_API_KEY, so if this is the database Darkroom uses, "
                                  "delete the file and start again to generate a new key")
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


def stored_sealed_values(settings: config.Settings) -> bool | None:
    if secretbox.is_sealed(settings.resend_api_key):
        return True
    try:
        if not database_present(settings):
            return False
        engine = db.make_engine(db.database_url(settings))
    except (ArgumentError, ValueError):
        return None
    try:
        return sealed_in_database(engine)
    except OperationalError:
        return None
    finally:
        engine.dispose()


def unreadable_file(sealed: bool | None) -> str:
    return UNREADABLE_FILE_NOTHING_SEALED if sealed is False else UNREADABLE_FILE


def missing_key_problem(settings: config.Settings) -> str | None:
    if settings.secret_key:
        return None
    sealed = stored_sealed_values(settings)
    if key_file_present(settings):
        return unreadable_file(sealed)
    if sealed is None:
        return DATABASE_UNREACHABLE
    return SEALED_WITHOUT_KEY if sealed else None


def key_file_present(settings: config.Settings) -> bool:
    path = config.secret_key_file(settings.data_dir)
    return path.exists() or path.is_symlink()


def write_in_place(path: Path, key: str) -> None:
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w", encoding="ascii") as out:
            out.write(key + "\n")
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        os.unlink(path)
        raise


def write_new_key(path: Path) -> str:
    key = secrets.token_urlsafe(KEY_BYTES)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w", encoding="ascii") as out:
            out.write(key + "\n")
            out.flush()
            os.fsync(out.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            raise
        except OSError:
            write_in_place(path, key)
    finally:
        os.unlink(temporary)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return key


def adopt_stored_key(settings: config.Settings, engine: Engine) -> config.Settings:
    key = config.stored_secret_key(settings.data_dir)
    if not key:
        raise Refused(unreadable_file(sealed_values_exist(settings, engine)))
    return dataclasses.replace(settings, secret_key=key, key_origin="file")


def ensure(settings: config.Settings, engine: Engine) -> config.Settings:
    if settings.secret_key:
        return settings
    if key_file_present(settings):
        return adopt_stored_key(settings, engine)
    if sealed_values_exist(settings, engine):
        raise Refused(SEALED_WITHOUT_KEY)
    try:
        key = write_new_key(config.secret_key_file(settings.data_dir))
    except FileExistsError:
        return adopt_stored_key(settings, engine)
    monitoring.log("secret key generated")
    return dataclasses.replace(settings, secret_key=key, key_origin="file")
