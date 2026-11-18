from alembic import op
import sqlalchemy as sa


revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("import_runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("not_imported", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("orphan_sidecars", sa.Integer(), server_default="0", nullable=False))
        batch_op.add_column(sa.Column("unpaired_media", sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    found = sa.inspect(op.get_bind())
    columns = {column["name"] for column in found.get_columns("import_runs")} if "import_runs" in found.get_table_names() else set()
    if "unpaired_media" in columns:
        op.execute("ALTER TABLE import_runs DROP COLUMN unpaired_media")
    if "orphan_sidecars" in columns:
        op.execute("ALTER TABLE import_runs DROP COLUMN orphan_sidecars")
    if "not_imported" in columns:
        op.execute("ALTER TABLE import_runs DROP COLUMN not_imported")
