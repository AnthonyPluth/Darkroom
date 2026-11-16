from alembic import op
import sqlalchemy as sa


revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pixel_transport",
        sa.Column("pixel_id", sa.Integer(), nullable=False),
        sa.Column("paired", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("last_seen", sa.Float(), nullable=True),
        sa.Column("completion", sa.Float(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("updated", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["pixel_id"], ["pixels.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("pixel_id"),
    )


def downgrade() -> None:
    if "pixel_transport" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("pixel_transport")
