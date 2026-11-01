from __future__ import annotations

import contextlib
import io
import logging
import unittest
from collections.abc import Iterator

from sqlalchemy import Engine, MetaData, select


def found_in_database(engine: Engine, canaries: set[str]) -> list[str]:
    out = []
    meta = MetaData()
    with engine.connect() as connection:
        meta.reflect(connection)
        for table in meta.sorted_tables:
            for row in connection.execute(select(table)).mappings():
                for column, value in row.items():
                    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
                    out += [f"{canary} in {table.name}.{column}" for canary in canaries if canary in text]
    return sorted(set(out))


@contextlib.contextmanager
def no_leaks(case: unittest.TestCase, *canaries: str, database: Engine | None = None, replies: list[bytes] | None = None) -> Iterator[set[str]]:
    watched = set(canaries)
    out, err, logged = io.StringIO(), io.StringIO(), io.StringIO()
    handler = logging.StreamHandler(logged)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            yield watched
    finally:
        root.removeHandler(handler)
    case.assertTrue(watched and all(len(canary) >= 8 for canary in watched), "use canaries long enough not to match by chance")
    places = {"what was printed": out.getvalue(), "stderr": err.getvalue(), "Python's logging": logged.getvalue()}
    if replies is not None:
        places["a reply"] = "\n".join(reply.decode("utf-8", "replace") for reply in replies)
    leaks = [f"{canary} in {where}" for where, text in places.items() for canary in watched if canary in text]
    if database is not None:
        leaks += found_in_database(database, watched)
    case.assertEqual(leaks, [], "private values escaped (AGENTS.md, \"Private data in logs\")")
