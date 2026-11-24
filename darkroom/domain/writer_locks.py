from __future__ import annotations

import contextlib
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom import config
from darkroom.domain import accounts as account_links
from darkroom.domain import immich_reconcile, legacy
from darkroom.domain.ingest import run as ingest_run
from darkroom.storage.models import Export, Person


class Busy(RuntimeError):
    pass


def accounts_of(opened: Session, person: Person) -> set[int]:
    owned = select(Export.account_id).where(Export.owner_person_id == person.id)
    return account_links.accounts_serving(opened, person.id) | set(opened.execute(owned).scalars())


def hold(stack: contextlib.ExitStack, opened: Session, settings: config.Settings, people: list[Person], accounts: Iterable[int]) -> None:
    try:
        for account_id in sorted(set(accounts)):
            stack.enter_context(ingest_run.account_lock(settings, account_id))
        for person in sorted(people, key=lambda found: found.id):
            stack.enter_context(legacy.person_lock(settings, person))
            stack.enter_context(immich_reconcile.person_lock(settings, person))
    except (ingest_run.Busy, legacy.Busy, immich_reconcile.Busy) as error:
        raise Busy(str(error)) from error
