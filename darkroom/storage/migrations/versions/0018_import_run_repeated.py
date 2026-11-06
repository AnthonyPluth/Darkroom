from alembic import op
import sqlalchemy as sa


revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("import_runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("repeated", sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    op.execute("ALTER TABLE import_runs DROP COLUMN repeated")
