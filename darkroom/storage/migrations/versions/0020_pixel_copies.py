from alembic import op
import sqlalchemy as sa


revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pixel_copies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("pixel_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("person_id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(), server_default="queued", nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("bytes", sa.Integer(), nullable=False),
        sa.Column("sha1", sa.String(), nullable=False),
        sa.Column("queued_at", sa.Float(), nullable=False),
        sa.Column("placed_at", sa.Float(), nullable=True),
        sa.Column("pruned_at", sa.Float(), nullable=True),
        sa.Column("requeued", sa.Integer(), server_default="0", nullable=False),
        sa.Column("always_send", sa.Boolean(), server_default="0", nullable=False),
        sa.CheckConstraint("state IN ('queued', 'placed', 'pruned')", name="ck_pixel_copies_state"),
        sa.ForeignKeyConstraint(["item_id"], ["media_items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pixel_id"], ["pixels.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pixel_id", "item_id", name="uq_pixel_copies_pixel_item"),
    )
    with op.batch_alter_table("pixel_copies", schema=None) as batch_op:
        batch_op.create_index("ix_pixel_copies_pixel_state", ["pixel_id", "state"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("pixel_copies", schema=None) as batch_op:
        batch_op.drop_index("ix_pixel_copies_pixel_state")
    op.drop_table("pixel_copies")
