import contextlib

from darkroom.domain import immich_reconcile, legacy, writer_locks
from darkroom.domain import legacy as legacy_import
from darkroom.domain.immich_reconcile import person_lock
from darkroom.domain.ingest import run as ingest_run
from darkroom.domain.ingest.run import account_lock
from darkroom.domain.ingest.run import account_lock as lock_account
from darkroom.domain.legacy import person_lock as lock_legacy_person


def reverse_order_by_hand(stack, settings, person, account_id):
    # ruleid: darkroom-writer-locks-one-helper
    stack.enter_context(immich_reconcile.person_lock(settings, person))
    # ruleid: darkroom-writer-locks-one-helper
    stack.enter_context(ingest_run.account_lock(settings, account_id))


def nested_withs(settings, person, account_id):
    # ruleid: darkroom-writer-locks-one-helper
    with ingest_run.account_lock(settings, account_id):
        # ruleid: darkroom-writer-locks-one-helper
        with legacy.person_lock(settings, person):
            return person


def one_with_for_both(settings, person):
    # ruleid: darkroom-writer-locks-one-helper
    with legacy.person_lock(settings, person), immich_reconcile.person_lock(settings, person):
        return person


def bare_name_with_a_person_lock(settings, person, account_id):
    # ruleid: darkroom-writer-locks-one-helper
    with account_lock(settings, account_id):
        # ruleid: darkroom-writer-locks-one-helper
        with immich_reconcile.person_lock(settings, person):
            return person


def aliased_account_and_legacy_locks(stack, settings, person, account_id):
    # ruleid: darkroom-writer-locks-one-helper
    stack.enter_context(lock_account(settings, account_id))
    # ruleid: darkroom-writer-locks-one-helper
    stack.enter_context(lock_legacy_person(settings, person))


def bare_reconcile_and_module_aliased_legacy_locks(settings, person):
    # ruleid: darkroom-writer-locks-one-helper
    with person_lock(settings, person), legacy_import.person_lock(settings, person):
        return person


def through_the_helper(opened, settings, people, account_ids):
    with contextlib.ExitStack() as stack:
        # ok: darkroom-writer-locks-one-helper
        writer_locks.hold(stack, opened, settings, people, account_ids)


def only_the_account_lock(settings, account_id):
    # ok: darkroom-writer-locks-one-helper
    with ingest_run.account_lock(settings, account_id):
        return account_id


def only_the_legacy_lock(settings, person):
    # ok: darkroom-writer-locks-one-helper
    with legacy.person_lock(settings, person):
        return person


def only_the_reconcile_lock(settings, person):
    # ok: darkroom-writer-locks-one-helper
    with immich_reconcile.person_lock(settings, person):
        return person


def one_lock_referenced_two_ways(stack, settings, person):
    # ok: darkroom-writer-locks-one-helper
    stack.enter_context(legacy_import.person_lock(settings, person))
    # ok: darkroom-writer-locks-one-helper
    stack.enter_context(lock_legacy_person(settings, person))


def unrelated_method_named_account_lock(stack, settings, person, account_id, other):
    # ok: darkroom-writer-locks-one-helper
    stack.enter_context(legacy.person_lock(settings, person))
    # ok: darkroom-writer-locks-one-helper
    stack.enter_context(other.account_lock(settings, account_id))
