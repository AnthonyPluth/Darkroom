from alembic import op
import sqlalchemy as sa


revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("managed_import_receipts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("hold_facts", sa.Text(), nullable=True))


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("LOCK TABLE managed_import_receipts IN ACCESS EXCLUSIVE MODE"))
    else:
        connection.execute(sa.text("UPDATE managed_import_receipts SET id = id WHERE 1 = 0"))
    if "hold_facts" in {column["name"] for column in sa.inspect(connection).get_columns("managed_import_receipts")}:
        op.execute("ALTER TABLE managed_import_receipts DROP COLUMN hold_facts")
