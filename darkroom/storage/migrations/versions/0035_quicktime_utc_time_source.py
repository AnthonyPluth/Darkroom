from alembic import op
import sqlalchemy as sa


revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None

TABLE = "media_items"
CHECK = "ck_media_items_time_source"
WITH_QUICKTIME = "time_source IN ('exif_offset', 'gps_lookup', 'sidecar', 'unknown', 'quicktime_utc')"
BEFORE = "time_source IN ('exif_offset', 'gps_lookup', 'sidecar', 'unknown')"


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
            raise RuntimeError("the tables that refer to media_items refer to each other in a cycle")
        order.extend(ready)
        pending -= set(ready)
    return order


def replace_check(rule: str) -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.drop_constraint(CHECK, TABLE, type_="check")
        op.create_check_constraint(CHECK, TABLE, rule)
        return
    children = dependents()
    for child in children:
        op.execute(f"CREATE TABLE _kept_{child} AS SELECT * FROM {child}")
    for child in reversed(children):
        op.execute(f"DELETE FROM {child}")
    with op.batch_alter_table(TABLE, schema=None, recreate="always") as batch_op:
        batch_op.drop_constraint(CHECK, type_="check")
        batch_op.create_check_constraint(CHECK, rule)
    for child in children:
        op.execute(f"INSERT INTO {child} SELECT * FROM _kept_{child}")
        op.execute(f"DROP TABLE _kept_{child}")


def upgrade() -> None:
    replace_check(WITH_QUICKTIME)


def downgrade() -> None:
    op.execute("UPDATE media_items SET time_source = 'unknown' WHERE time_source = 'quicktime_utc'")
    replace_check(BEFORE)
