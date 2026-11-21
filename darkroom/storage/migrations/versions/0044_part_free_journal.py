from alembic import op
import sqlalchemy as sa


revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None

Wide = sa.Integer().with_variant(sa.BigInteger(), "postgresql")
COLUMNS = {"export_parts": ("free_state", "free_started_at", "freed_at", "bytes_freed"), "takeout_entries": ("held_state", "held_freed_at")}


def upgrade() -> None:
    with op.batch_alter_table("export_parts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("free_state", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("free_started_at", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("freed_at", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("bytes_freed", Wide, nullable=True))
    with op.batch_alter_table("takeout_entries", schema=None) as batch_op:
        batch_op.add_column(sa.Column("held_state", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("held_freed_at", sa.Float(), nullable=True))


def downgrade() -> None:
    found = sa.inspect(op.get_bind())
    tables = found.get_table_names()
    for table, columns in COLUMNS.items():
        present = {column["name"] for column in found.get_columns(table)} if table in tables else set()
        for column in columns:
            if column in present:
                op.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
