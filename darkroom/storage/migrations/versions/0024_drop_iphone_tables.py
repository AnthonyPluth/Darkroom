from alembic import op
import sqlalchemy as sa


revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("phone_resources")
    op.drop_table("phone_assets")
    op.drop_index("ix_devices_push_token_digest", table_name="devices")
    op.drop_table("devices")
    with op.batch_alter_table("alerts", schema=None) as batch_op:
        batch_op.drop_column("pushed_devices")
    op.execute("DELETE FROM auth_pending WHERE kind != 'web'")
    with op.batch_alter_table("auth_pending", schema=None, recreate="always") as batch_op:
        batch_op.drop_constraint("ck_auth_pending_kind", type_="check")
        batch_op.drop_column("device_name")
        batch_op.drop_column("client_state")
        batch_op.create_check_constraint("ck_auth_pending_kind", "kind IN ('web')")


def downgrade() -> None:
    with op.batch_alter_table("auth_pending", schema=None, recreate="always") as batch_op:
        batch_op.drop_constraint("ck_auth_pending_kind", type_="check")
        batch_op.add_column(sa.Column("device_name", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("client_state", sa.String(), nullable=True))
        batch_op.create_check_constraint("ck_auth_pending_kind", "kind IN ('web', 'device')")
    with op.batch_alter_table("alerts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("pushed_devices", sa.Text(), nullable=True))
    op.create_table(
        "devices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("person_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("cleanup_enabled", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("last_checkin", sa.Float(), nullable=True),
        sa.Column("app_version", sa.String(), nullable=True),
        sa.Column("pending_uploads", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created", sa.Float(), nullable=False),
        sa.Column("revoked_at", sa.Float(), nullable=True),
        sa.Column("sub", sa.String(), nullable=True),
        sa.Column("eligibility_checked_at", sa.Float(), nullable=True),
        sa.Column("push_token_sealed", sa.Text(), nullable=True),
        sa.Column("push_token_digest", sa.String(), nullable=True),
        sa.Column("push_token_at", sa.Float(), nullable=True),
        sa.Column("wake_pushed_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_devices_push_token_digest", "devices", ["push_token_digest"], unique=True)
    op.create_table(
        "phone_assets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("device_id", sa.Integer(), nullable=False),
        sa.Column("photokit_id", sa.String(), nullable=False),
        sa.Column("created", sa.Float(), nullable=False),
        sa.Column("eligible_at", sa.Float(), nullable=True),
        sa.Column("eligible_confirmed_at", sa.Float(), nullable=True),
        sa.Column("deleted_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("device_id", "photokit_id", name="uq_phone_assets_device_photokit"),
    )
    op.create_table(
        "phone_resources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("sha1", sa.String(), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("uploaded_at", sa.Float(), nullable=True),
        sa.Column("media_item_id", sa.Integer(), nullable=True),
        sa.CheckConstraint("kind IN ('photo', 'live_video', 'adjusted')", name="ck_phone_resources_kind"),
        sa.ForeignKeyConstraint(["asset_id"], ["phone_assets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["media_item_id"], ["media_items.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("asset_id", "kind", name="uq_phone_resources_asset_kind"),
    )
