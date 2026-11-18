from alembic import op
import sqlalchemy as sa


revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("exports", sa.Column("download_interruptions", sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    found = sa.inspect(op.get_bind())
    if "exports" in found.get_table_names() and "download_interruptions" in {column["name"] for column in found.get_columns("exports")}:
        op.execute("ALTER TABLE exports DROP COLUMN download_interruptions")
