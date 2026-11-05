from alembic import op
import sqlalchemy as sa


revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("alerts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("pushed_devices", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("alerts", schema=None) as batch_op:
        batch_op.drop_column("pushed_devices")
