from alembic import op
import sqlalchemy as sa


revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("pixel_copies", schema=None) as batch_op:
        batch_op.add_column(sa.Column("seen_again_at", sa.Float(), nullable=True))


def downgrade() -> None:
    op.execute("ALTER TABLE pixel_copies DROP COLUMN seen_again_at")
