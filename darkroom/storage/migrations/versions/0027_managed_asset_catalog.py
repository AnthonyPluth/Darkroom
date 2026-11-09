from alembic import op
import sqlalchemy as sa


revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_managed_import_intents_catalog_binding", "managed_import_intents", ["id", "person_id", "owner_id"], unique=True)
    op.create_index("ix_managed_import_receipts_catalog_binding", "managed_import_receipts", ["id", "intent_id", "asset_id", "source_sha1", "state"], unique=True)
    op.create_table(
        "managed_asset_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("person_id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("asset_id", sa.String(), nullable=False),
        sa.Column("sha1", sa.String(), nullable=False),
        sa.Column("bytes", sa.Integer().with_variant(sa.BigInteger(), "postgresql"), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("state", sa.String(), server_default="observed", nullable=False),
        sa.Column("library_id", sa.String(), nullable=True),
        sa.Column("linked_video_asset_id", sa.String(), nullable=True),
        sa.Column("metadata_snapshot", sa.Text(), nullable=False),
        sa.Column("seen_at", sa.Float(), nullable=False),
        sa.Column("bytes_verified_at", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("person_id", "owner_id", "asset_id", "sha1", name="uq_managed_asset_versions_identity"),
        sa.UniqueConstraint("id", "person_id", "owner_id", "asset_id", "sha1", name="uq_managed_asset_versions_binding"),
        sa.CheckConstraint("length(owner_id) > 0 AND length(asset_id) > 0 AND length(sha1) = 40", name="ck_managed_asset_versions_identity"),
        sa.CheckConstraint("bytes IS NULL OR bytes >= 0", name="ck_managed_asset_versions_bytes"),
        sa.CheckConstraint("library_id IS NULL OR length(library_id) > 0", name="ck_managed_asset_versions_library"),
        sa.CheckConstraint("linked_video_asset_id IS NULL OR length(linked_video_asset_id) > 0", name="ck_managed_asset_versions_link"),
        sa.CheckConstraint("kind IN ('IMAGE', 'VIDEO')", name="ck_managed_asset_versions_kind"),
        sa.CheckConstraint("state IN ('observed', 'bytes_verified', 'missing', 'trashed', 'offline', 'external', 'superseded')", name="ck_managed_asset_versions_state"),
        sa.CheckConstraint("state != 'bytes_verified' OR (library_id IS NULL AND bytes IS NOT NULL AND bytes_verified_at IS NOT NULL)", name="ck_managed_asset_versions_verified"),
        sa.CheckConstraint("state != 'external' OR library_id IS NOT NULL", name="ck_managed_asset_versions_external"),
    )
    op.create_index("ix_managed_asset_versions_owner_state", "managed_asset_versions", ["person_id", "owner_id", "state"])
    op.create_table(
        "managed_asset_origins",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.Column("person_id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("asset_id", sa.String(), nullable=False),
        sa.Column("sha1", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("source_key", sa.String(), nullable=False),
        sa.Column("intent_id", sa.Integer(), nullable=True),
        sa.Column("receipt_id", sa.Integer(), nullable=True),
        sa.Column("receipt_state", sa.String(), nullable=True),
        sa.Column("evidence_snapshot", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version_id", "kind", "source_key", name="uq_managed_asset_origins_source"),
        sa.ForeignKeyConstraint(["version_id", "person_id", "owner_id", "asset_id", "sha1"],
                                ["managed_asset_versions.id", "managed_asset_versions.person_id", "managed_asset_versions.owner_id",
                                 "managed_asset_versions.asset_id", "managed_asset_versions.sha1"], ondelete="RESTRICT", name="fk_managed_asset_origins_version"),
        sa.ForeignKeyConstraint(["intent_id", "person_id", "owner_id"],
                                ["managed_import_intents.id", "managed_import_intents.person_id", "managed_import_intents.owner_id"],
                                ondelete="RESTRICT", name="fk_managed_asset_origins_intent_owner"),
        sa.ForeignKeyConstraint(["receipt_id", "intent_id", "asset_id", "sha1", "receipt_state"],
                                ["managed_import_receipts.id", "managed_import_receipts.intent_id", "managed_import_receipts.asset_id",
                                 "managed_import_receipts.source_sha1", "managed_import_receipts.state"], ondelete="RESTRICT", name="fk_managed_asset_origins_receipt"),
        sa.CheckConstraint("kind IN ('phone', 'takeout', 'legacy', 'other')", name="ck_managed_asset_origins_kind"),
        sa.CheckConstraint("length(source_key) > 0", name="ck_managed_asset_origins_source"),
        sa.CheckConstraint("(receipt_id IS NULL AND intent_id IS NULL AND receipt_state IS NULL) OR "
                           "(receipt_id IS NOT NULL AND intent_id IS NOT NULL AND receipt_state IS NOT NULL AND receipt_state = 'committed')",
                           name="ck_managed_asset_origins_receipt"),
        sa.CheckConstraint("kind != 'takeout' OR receipt_id IS NOT NULL", name="ck_managed_asset_origins_takeout"),
        sa.CheckConstraint("kind != 'phone' OR receipt_id IS NULL", name="ck_managed_asset_origins_phone"),
    )
    op.create_index("ix_managed_asset_origins_receipt", "managed_asset_origins", ["receipt_id", "intent_id"])
    op.create_index("ix_managed_asset_origins_intent", "managed_asset_origins", ["intent_id", "person_id", "owner_id"])


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("LOCK TABLE managed_asset_versions, managed_asset_origins IN ACCESS EXCLUSIVE MODE"))
    else:
        connection.execute(sa.text("UPDATE managed_asset_versions SET id = id WHERE 1 = 0"))
    for table in ("managed_asset_versions", "managed_asset_origins"):
        if connection.execute(sa.select(1).select_from(sa.table(table)).limit(1)).first() is not None:
            raise RuntimeError("the managed asset catalogue is not empty; retain its versions and origins")
    op.drop_index("ix_managed_asset_origins_intent", table_name="managed_asset_origins")
    op.drop_index("ix_managed_asset_origins_receipt", table_name="managed_asset_origins")
    op.drop_table("managed_asset_origins")
    op.drop_index("ix_managed_asset_versions_owner_state", table_name="managed_asset_versions")
    op.drop_table("managed_asset_versions")
    op.drop_index("ix_managed_import_receipts_catalog_binding", table_name="managed_import_receipts")
    op.drop_index("ix_managed_import_intents_catalog_binding", table_name="managed_import_intents")
