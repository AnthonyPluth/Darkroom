from alembic import op
import sqlalchemy as sa


revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("exports", schema=None) as batch_op:
        batch_op.add_column(sa.Column("ingest_route", sa.String(), nullable=True))


def downgrade() -> None:
    found = sa.inspect(op.get_bind())
    present = {column["name"] for column in found.get_columns("exports")} if "exports" in found.get_table_names() else set()
    if "ingest_route" in present:
        op.execute("ALTER TABLE exports DROP COLUMN ingest_route")
