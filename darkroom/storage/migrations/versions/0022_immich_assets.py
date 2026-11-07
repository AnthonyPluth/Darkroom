from alembic import op
import sqlalchemy as sa


revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("people", schema=None) as batch_op:
        batch_op.add_column(sa.Column("immich_reconciled_until", sa.Float(), nullable=True))
    op.create_table(
        "immich_assets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("person_id", sa.Integer(), nullable=False),
        sa.Column("asset_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("sha1", sa.String(), nullable=False),
        sa.Column("linked_asset_id", sa.String(), nullable=True),
        sa.Column("original_item_id", sa.Integer(), nullable=True),
        sa.Column("seen_at", sa.Float(), nullable=False),
        sa.Column("queued_at", sa.Float(), nullable=True),
        sa.CheckConstraint("kind IN ('photo', 'live_video')", name="ck_immich_assets_kind"),
        sa.CheckConstraint("status IN ('stored', 'waiting', 'ignored', 'failed')", name="ck_immich_assets_status"),
        sa.ForeignKeyConstraint(["original_item_id"], ["media_items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("person_id", "asset_id", name="uq_immich_assets_person_asset"),
    )


def downgrade() -> None:
    op.drop_table("immich_assets")
    op.execute("ALTER TABLE people DROP COLUMN immich_reconciled_until")
