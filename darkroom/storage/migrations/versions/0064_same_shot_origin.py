from alembic import op
import sqlalchemy as sa


revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None

TABLE = "managed_asset_origins"
LINK_NAME = "ck_managed_asset_origins_receipt"
TAKEOUT_NAME = "ck_managed_asset_origins_takeout"
OWNER_DECISION = "substr(source_key, 1, 10) = 'same-shot:'"
LINK_BEFORE = ("(receipt_id IS NULL AND intent_id IS NULL AND receipt_state IS NULL) OR "
               "(receipt_id IS NOT NULL AND intent_id IS NOT NULL AND receipt_state IS NOT NULL AND receipt_state = 'committed')")
LINK_AFTER = ("(receipt_id IS NULL AND receipt_state IS NULL AND (intent_id IS NULL OR (kind = 'takeout' AND " + OWNER_DECISION + "))) OR "
              "(receipt_id IS NOT NULL AND intent_id IS NOT NULL AND receipt_state IS NOT NULL AND receipt_state = 'committed')")
TAKEOUT_BEFORE = "kind != 'takeout' OR receipt_id IS NOT NULL"
TAKEOUT_AFTER = "kind != 'takeout' OR receipt_id IS NOT NULL OR (intent_id IS NOT NULL AND " + OWNER_DECISION + ")"


def begin_sqlite_ddl(connection: sa.Connection) -> None:
    if connection.dialect.name != "sqlite":
        return
    driver = connection.connection.driver_connection
    assert driver is not None
    if not driver.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def rules(link: str, takeout: str) -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.drop_constraint(LINK_NAME, TABLE, type_="check")
        op.drop_constraint(TAKEOUT_NAME, TABLE, type_="check")
        op.create_check_constraint(LINK_NAME, TABLE, link)
        op.create_check_constraint(TAKEOUT_NAME, TABLE, takeout)
        return
    with op.batch_alter_table(TABLE, schema=None, recreate="always") as batch_op:
        batch_op.drop_constraint(LINK_NAME, type_="check")
        batch_op.drop_constraint(TAKEOUT_NAME, type_="check")
        batch_op.create_check_constraint(LINK_NAME, link)
        batch_op.create_check_constraint(TAKEOUT_NAME, takeout)


def upgrade() -> None:
    begin_sqlite_ddl(op.get_bind())
    rules(LINK_AFTER, TAKEOUT_AFTER)


def downgrade() -> None:
    connection = op.get_bind()
    begin_sqlite_ddl(connection)
    if TABLE not in sa.inspect(connection).get_table_names():
        return
    origins = sa.table(TABLE, sa.column("id"), sa.column("receipt_id"), sa.column("intent_id"))
    owned = sa.select(origins.c.id).where(origins.c.receipt_id.is_(None), origins.c.intent_id.is_not(None)).limit(1)
    if connection.execute(owned).first() is not None:
        raise RuntimeError("the owner's same-shot decisions are kept as origins; roll the schema forward instead of downgrading past 0064")
    rules(LINK_BEFORE, TAKEOUT_BEFORE)
