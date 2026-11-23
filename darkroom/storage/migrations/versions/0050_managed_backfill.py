from alembic import op
import sqlalchemy as sa


revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None

TABLE = "managed_import_intents"
KIND = "ck_managed_import_intents_kind"
KIND_RULE = "kind IN ('import', 'backfill')"
EXPORT = "ck_managed_import_intents_export"
EXPORT_RULE = "kind = 'backfill' OR export_id IS NOT NULL"


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
            raise RuntimeError("the tables that refer to managed_import_intents refer to each other in a cycle")
        order.extend(ready)
        pending -= set(ready)
    return order


def recreate(change) -> None:
    children = dependents()
    for child in children:
        op.execute(f"CREATE TABLE _kept_{child} AS SELECT * FROM {child}")
    for child in reversed(children):
        op.execute(f"DELETE FROM {child}")
    with op.batch_alter_table(TABLE, schema=None, recreate="always") as batch_op:
        change(batch_op)
    for child in children:
        op.execute(f"INSERT INTO {child} SELECT * FROM _kept_{child}")
        op.execute(f"DROP TABLE _kept_{child}")


def widen(batch_op) -> None:
    batch_op.alter_column("export_id", existing_type=sa.Integer(), nullable=True)
    batch_op.create_check_constraint(KIND, KIND_RULE)
    batch_op.create_check_constraint(EXPORT, EXPORT_RULE)


def narrow(batch_op) -> None:
    batch_op.drop_constraint(EXPORT, type_="check")
    batch_op.drop_constraint(KIND, type_="check")
    batch_op.alter_column("export_id", existing_type=sa.Integer(), nullable=False)
    batch_op.drop_column("kind")


def upgrade() -> None:
    with op.batch_alter_table("people", schema=None) as batch_op:
        batch_op.add_column(sa.Column("managed_backfill", sa.String(), nullable=True))
    op.add_column(TABLE, sa.Column("kind", sa.String(), server_default="import", nullable=False))
    if op.get_bind().dialect.name == "postgresql":
        op.alter_column(TABLE, "export_id", existing_type=sa.Integer(), nullable=True)
        op.create_check_constraint(KIND, TABLE, KIND_RULE)
        op.create_check_constraint(EXPORT, TABLE, EXPORT_RULE)
    else:
        recreate(widen)
    op.create_table(
        "managed_backfill_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("person_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("version_id", sa.Integer(), nullable=True),
        sa.Column("receipt_id", sa.Integer(), nullable=True),
        sa.Column("checked_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["item_id"], ["media_items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["version_id"], ["managed_asset_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["receipt_id"], ["managed_import_receipts.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("item_id", name="uq_managed_backfill_items_item"),
        sa.CheckConstraint("outcome IN ('mapped', 'exception')", name="ck_managed_backfill_items_outcome"),
        sa.CheckConstraint("outcome = 'exception' OR version_id IS NOT NULL", name="ck_managed_backfill_items_mapped"),
        sa.CheckConstraint("outcome = 'mapped' OR reason IS NOT NULL", name="ck_managed_backfill_items_reason"),
    )
    op.create_index("ix_managed_backfill_items_person_outcome", "managed_backfill_items", ["person_id", "outcome"])


def downgrade() -> None:
    connection = op.get_bind()
    intents = sa.table(TABLE, sa.column("id"), sa.column("kind"))
    if connection.execute(sa.select(intents.c.id).where(intents.c.kind == "backfill").limit(1)).first() is not None:
        raise RuntimeError("Immich backfill receipts are kept; roll the schema forward instead of downgrading past 0050")
    op.drop_index("ix_managed_backfill_items_person_outcome", table_name="managed_backfill_items")
    op.drop_table("managed_backfill_items")
    if connection.dialect.name == "postgresql":
        op.drop_constraint(EXPORT, TABLE, type_="check")
        op.drop_constraint(KIND, TABLE, type_="check")
        op.alter_column(TABLE, "export_id", existing_type=sa.Integer(), nullable=False)
        op.drop_column(TABLE, "kind")
    else:
        recreate(narrow)
    if "managed_backfill" in {column["name"] for column in sa.inspect(connection).get_columns("people")}:
        op.execute("ALTER TABLE people DROP COLUMN managed_backfill")
