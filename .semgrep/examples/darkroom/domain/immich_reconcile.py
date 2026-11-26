from darkroom.domain import legacy as legacy_import
from darkroom.domain.ingest import run as ingest_run


def person_lock(settings, person):
    return settings, person


def reconcile_and_account_locks(settings, person, account_id):
    # ruleid: darkroom-reconcile-writer-locks-one-helper
    with person_lock(settings, person), ingest_run.account_lock(settings, account_id):
        return person


def reconcile_and_legacy_locks(stack, settings, person):
    # ruleid: darkroom-reconcile-writer-locks-one-helper
    stack.enter_context(person_lock(settings, person))
    stack.enter_context(legacy_import.person_lock(settings, person))


def only_the_local_reconcile_lock(settings, person):
    # ok: darkroom-reconcile-writer-locks-one-helper
    with person_lock(settings, person):
        return person
