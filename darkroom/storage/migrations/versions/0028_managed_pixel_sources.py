from alembic import op
import sqlalchemy as sa


revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_managed_asset_versions_pixel_binding", "managed_asset_versions", ["id", "person_id", "owner_id", "asset_id", "sha1", "bytes"], unique=True)
    op.create_index("ix_media_items_pixel_source_binding", "media_items", ["id", "person_id"], unique=True)
    op.create_index("ix_pixel_copies_source_binding", "pixel_copies", ["id", "pixel_id", "item_id", "person_id"], unique=True)
    op.create_table(
        "managed_pixel_sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("copy_id", sa.Integer(), nullable=False),
        sa.Column("pixel_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("person_id", sa.Integer(), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("asset_id", sa.String(), nullable=False),
        sa.Column("sha1", sa.String(), nullable=False),
        sa.Column("bytes", sa.Integer().with_variant(sa.BigInteger(), "postgresql"), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("google_account_id", sa.Integer(), nullable=False),
        sa.Column("destination_email", sa.String(), nullable=False),
        sa.Column("copy_snapshot", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("copy_id", "generation", "google_account_id", name="uq_managed_pixel_sources_generation"),
        sa.ForeignKeyConstraint(["copy_id", "pixel_id", "item_id", "person_id"],
                               ["pixel_copies.id", "pixel_copies.pixel_id", "pixel_copies.item_id", "pixel_copies.person_id"],
                               ondelete="RESTRICT", name="fk_managed_pixel_sources_copy"),
        sa.ForeignKeyConstraint(["item_id", "person_id"], ["media_items.id", "media_items.person_id"],
                               ondelete="RESTRICT", name="fk_managed_pixel_sources_item"),
        sa.ForeignKeyConstraint(["version_id", "person_id", "owner_id", "asset_id", "sha1", "bytes"],
                               ["managed_asset_versions.id", "managed_asset_versions.person_id", "managed_asset_versions.owner_id",
                                "managed_asset_versions.asset_id", "managed_asset_versions.sha1", "managed_asset_versions.bytes"],
                               ondelete="RESTRICT", name="fk_managed_pixel_sources_version"),
        sa.ForeignKeyConstraint(["google_account_id"], ["google_accounts.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("generation >= 0 AND bytes >= 0", name="ck_managed_pixel_sources_counts"),
        sa.CheckConstraint("length(destination_email) > 0", name="ck_managed_pixel_sources_destination"),
    )
    op.create_index("ix_managed_pixel_sources_version", "managed_pixel_sources", ["version_id"])
    op.create_index("ix_managed_pixel_sources_item", "managed_pixel_sources", ["item_id", "person_id"])
    op.create_index("ix_managed_pixel_sources_account", "managed_pixel_sources", ["google_account_id"])


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("LOCK TABLE managed_pixel_sources IN ACCESS EXCLUSIVE MODE"))
    else:
        connection.execute(sa.text("UPDATE managed_pixel_sources SET id = id WHERE 1 = 0"))
    if connection.execute(sa.select(1).select_from(sa.table("managed_pixel_sources")).limit(1)).first() is not None:
        raise RuntimeError("the managed Pixel source history is not empty; retain its versioned references")
    op.drop_index("ix_managed_pixel_sources_account", table_name="managed_pixel_sources")
    op.drop_index("ix_managed_pixel_sources_item", table_name="managed_pixel_sources")
    op.drop_index("ix_managed_pixel_sources_version", table_name="managed_pixel_sources")
    op.drop_table("managed_pixel_sources")
    op.drop_index("ix_pixel_copies_source_binding", table_name="pixel_copies")
    op.drop_index("ix_media_items_pixel_source_binding", table_name="media_items")
    op.drop_index("ix_managed_asset_versions_pixel_binding", table_name="managed_asset_versions")
