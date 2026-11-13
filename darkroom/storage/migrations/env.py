from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from darkroom.storage.schema import metadata

config = context.config


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        context.configure(connection=connection, target_metadata=metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = config.attributes.get("engine") or engine_from_config(config.get_section(config.config_ini_section) or {}, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with engine.connect() as opened:
        context.configure(connection=opened, target_metadata=metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
