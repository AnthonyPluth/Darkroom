from darkroom.domain import immich_reconcile
from darkroom.domain.ingest.run import account_lock as lock_account


def person_lock(settings, person):
    return settings, person


def legacy_and_account_locks(stack, settings, person, account_id):
    stack.enter_context(lock_account(settings, account_id))
    # ruleid: darkroom-legacy-writer-locks-one-helper
    stack.enter_context(person_lock(settings, person))


def legacy_and_reconcile_locks(settings, person):
    # ruleid: darkroom-legacy-writer-locks-one-helper
    with person_lock(settings, person), immich_reconcile.person_lock(settings, person):
        return person


def only_the_local_legacy_lock(settings, person):
    # ok: darkroom-legacy-writer-locks-one-helper
    with person_lock(settings, person):
        return person
