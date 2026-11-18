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
    op.execute("ALTER TABLE import_runs DROP COLUMN unpaired_media")
    op.execute("ALTER TABLE import_runs DROP COLUMN orphan_sidecars")
    op.execute("ALTER TABLE import_runs DROP COLUMN not_imported")
