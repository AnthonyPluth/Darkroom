from alembic import op
import sqlalchemy as sa


revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None

COLUMNS = ("send_attempts", "send_lease_until")


def upgrade() -> None:
    with op.batch_alter_table("managed_import_receipts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("send_attempts", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("send_lease_until", sa.Float(), nullable=True))


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("LOCK TABLE managed_import_receipts IN ACCESS EXCLUSIVE MODE"))
    else:
        connection.execute(sa.text("UPDATE managed_import_receipts SET id = id WHERE 1 = 0"))
    present = {column["name"] for column in sa.inspect(connection).get_columns("managed_import_receipts")}
    for column in COLUMNS:
        if column in present:
            op.execute(f"ALTER TABLE managed_import_receipts DROP COLUMN {column}")
