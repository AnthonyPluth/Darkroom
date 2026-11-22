from alembic import op
import sqlalchemy as sa


revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None

COLUMNS = ("free_kept_at", "free_kept_key")


def upgrade() -> None:
    with op.batch_alter_table("export_parts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("free_kept_at", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("free_kept_key", sa.String(), nullable=True))


def downgrade() -> None:
    found = sa.inspect(op.get_bind())
    present = {column["name"] for column in found.get_columns("export_parts")} if "export_parts" in found.get_table_names() else set()
    for column in COLUMNS:
        if column in present:
            op.execute(f"ALTER TABLE export_parts DROP COLUMN {column}")
