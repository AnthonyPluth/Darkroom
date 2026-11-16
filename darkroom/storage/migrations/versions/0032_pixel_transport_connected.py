from alembic import op
import sqlalchemy as sa


revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("pixel_transport") as batch:
        batch.add_column(sa.Column("connected", sa.Boolean(), server_default="0", nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("pixel_transport") as batch:
        batch.drop_column("connected")
