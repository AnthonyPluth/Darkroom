from alembic import op
import sqlalchemy as sa


revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None

TABLE = "people"
RULE_NAME = "ck_people_managed_imports"
STATES = ("pausing", "on", "stopped")
RULE = f"managed_imports IN ({', '.join(repr(state) for state in STATES)})"
COLUMNS = ("managed_imports_since", "managed_switch_waiting")


def dependents() -> list[str]:
    found = sa.inspect(op.get_bind())
    referring = {name: {fk["referred_table"] for fk in found.get_foreign_keys(name)} - {name} for name in found.get_table_names()}
    reached = {TABLE}
    while grown := {name for name, parents in referring.items() if parents & reached} - reached:
        reached |= grown
    pending = reached - {TABLE}
    order: list[str] = []
    while pending:
        ready = sorted(name for name in pending if not (referring[name] & pending))
        if not ready:
            raise RuntimeError("the tables that refer to people refer to each other in a cycle")
        order.extend(ready)
        pending -= set(ready)
    return order


def recreate(change) -> None:
    children = dependents()
    for child in children:
        op.execute(f"DROP TABLE IF EXISTS _kept_{child}")
        op.execute(f"CREATE TABLE _kept_{child} AS SELECT * FROM {child}")
    for child in reversed(children):
        op.execute(f"DELETE FROM {child}")
    with op.batch_alter_table(TABLE, schema=None, recreate="always") as batch_op:
        change(batch_op)
    for child in children:
        op.execute(f"INSERT INTO {child} SELECT * FROM _kept_{child}")
        op.execute(f"DROP TABLE _kept_{child}")


def constrain(batch_op) -> None:
    batch_op.create_check_constraint(RULE_NAME, RULE)


def release(batch_op) -> None:
    batch_op.drop_constraint(RULE_NAME, type_="check")


def upgrade() -> None:
    connection = op.get_bind()
    people = sa.table(TABLE, sa.column("id"), sa.column("managed_imports"))
    odd = sa.select(people.c.id).where(people.c.managed_imports.is_not(None), people.c.managed_imports.not_in(STATES)).limit(1)
    if connection.execute(odd).first() is not None:
        raise RuntimeError("a person's managed-imports setting is not one Darkroom knows; fix it by hand before migrating")
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        batch_op.add_column(sa.Column("managed_imports_since", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("managed_switch_waiting", sa.String(), nullable=True))
    if connection.dialect.name == "postgresql":
        op.create_check_constraint(RULE_NAME, TABLE, RULE)
    else:
        recreate(constrain)


def downgrade() -> None:
    connection = op.get_bind()
    found = sa.inspect(connection)
    if TABLE not in found.get_table_names():
        return
    if RULE_NAME in {constraint["name"] for constraint in found.get_check_constraints(TABLE)}:
        if connection.dialect.name == "postgresql":
            op.drop_constraint(RULE_NAME, TABLE, type_="check")
        else:
            recreate(release)
    present = {column["name"] for column in sa.inspect(connection).get_columns(TABLE)}
    for column in COLUMNS:
        if column in present:
            op.execute(f"ALTER TABLE {TABLE} DROP COLUMN {column}")
