from alembic import op
import sqlalchemy as sa


revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pixel_transport", sa.Column("connected", sa.Boolean(), server_default="0", nullable=False))


def downgrade() -> None:
    found = sa.inspect(op.get_bind())
    if "pixel_transport" in found.get_table_names() and "connected" in {column["name"] for column in found.get_columns("pixel_transport")}:
        op.execute("ALTER TABLE pixel_transport DROP COLUMN connected")
