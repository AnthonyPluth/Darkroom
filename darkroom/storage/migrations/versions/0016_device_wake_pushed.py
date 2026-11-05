from alembic import op
import sqlalchemy as sa


revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("devices", schema=None) as batch_op:
        batch_op.add_column(sa.Column("wake_pushed_at", sa.Float(), nullable=True))


def downgrade() -> None:
    op.execute("ALTER TABLE devices DROP COLUMN wake_pushed_at")
