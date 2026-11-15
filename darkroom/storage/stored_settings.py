from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import Engine, inspect, select
from sqlalchemy.exc import ArgumentError, OperationalError
from sqlalchemy.orm import Session

from darkroom import config
from darkroom.storage import db, secret_key, secretbox
from darkroom.storage.settings_keys import SHARE_SENTINEL
from darkroom.storage.models import StoredSetting

Source = Literal["environment", "stored", "default"]
REGISTRY = config.STORABLE
BY_VARIABLE = {entry.variable: entry for entry in REGISTRY}
RECORDED = (SHARE_SENTINEL,)
UNREADABLE = "{variable} is stored sealed but cannot be read with this secret key; set {variable}, or set DARKROOM_SECRET_KEY to the key it was sealed with"
NOT_READ = "the database could not be reached to read the stored settings, so the sources below leave them out"


class NotStorable(Exception):
    pass


@dataclass(frozen=True)
class Effective:
    settings: config.Settings
    sources: dict[str, Source]
    problems: tuple[str, ...] = ()
    recorded: dict[str, str] = field(default_factory=dict)


def has_table(engine: Engine) -> bool:
    return inspect(engine).has_table(StoredSetting.__tablename__)


def stored_values(engine: Engine) -> dict[str, tuple[str | None, str | None]]:
    if not has_table(engine):
        return {}
    with db.session(engine) as opened:
        return {row.name: (row.value, row.value_sealed) for row in opened.scalars(select(StoredSetting))}


def parsed(entry: config.Storable, value: str | None, sealed: str | None, key: str) -> str | tuple[str, ...] | None:
    if entry.secret:
        if not sealed:
            return None
        plain = secretbox.unseal(sealed, key)
        return sealed if entry.kind == "sealed" else plain
    if value is None or not value.strip():
        return None
    if entry.kind == "list":
        return config.comma_list(value) or None
    return value.strip().rstrip("/") if entry.kind == "address" else value.strip()


def combined(settings: config.Settings, rows: dict[str, tuple[str | None, str | None]]) -> Effective:
    changes: dict[str, Any] = {}
    sources: dict[str, Source] = {}
    problems: list[str] = []
    for entry in REGISTRY:
        if entry.variable in settings.from_environment:
            sources[entry.variable] = "environment"
            continue
        value, sealed = rows.get(entry.variable, (None, None))
        try:
            found = parsed(entry, value, sealed, settings.secret_key)
        except secretbox.Unreadable:
            problems.append(UNREADABLE.format(variable=entry.variable))
            sources[entry.variable] = "stored"
            continue
        if found is None:
            sources[entry.variable] = "default"
            continue
        changes[entry.field] = found
        sources[entry.variable] = "stored"
    recorded = {name: value for name in RECORDED for value in (rows.get(name, (None, None))[0],) if value}
    return Effective(dataclasses.replace(settings, **changes), sources, tuple(problems), recorded)


def effective(settings: config.Settings, engine: Engine) -> Effective:
    return combined(settings, stored_values(engine))


def for_check(settings: config.Settings) -> Effective:
    try:
        if not secret_key.database_present(settings):
            return combined(settings, {})
        engine = db.make_engine(db.database_url(settings))
    except (ArgumentError, ValueError):
        return dataclasses.replace(combined(settings, {}), problems=(NOT_READ,))
    try:
        return effective(settings, engine)
    except OperationalError:
        return dataclasses.replace(combined(settings, {}), problems=(NOT_READ,))
    finally:
        engine.dispose()


def store(opened: Session, variable: str, plain: str, key: str, now: float) -> None:
    entry = BY_VARIABLE.get(variable)
    if entry is None:
        raise NotStorable(f"{variable} is not a setting Darkroom stores")
    value, sealed = (None, secretbox.seal(plain, key)) if entry.secret else (plain, None)
    db.upsert(opened, StoredSetting, {"name": variable, "value": value, "value_sealed": sealed, "updated": now}, ("name",))


def recorded_share(opened: Session) -> str | None:
    row = opened.get(StoredSetting, SHARE_SENTINEL, populate_existing=True)
    return None if row is None else row.value


def record_share(opened: Session, token: str, now: float) -> None:
    db.upsert(opened, StoredSetting, {"name": SHARE_SENTINEL, "value": token, "value_sealed": None, "updated": now}, ("name",))
