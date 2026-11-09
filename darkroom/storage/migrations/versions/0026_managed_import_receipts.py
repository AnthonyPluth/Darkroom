from alembic import op
import sqlalchemy as sa


revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    byte_count = sa.Integer().with_variant(sa.BigInteger(), "postgresql")
    op.create_table(
        "managed_import_intents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("person_id", sa.Integer(), nullable=False),
        sa.Column("export_id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("resource_manifest", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.CheckConstraint("schema_version = 1", name="ck_managed_import_intents_schema_version"),
        sa.CheckConstraint("length(owner_id) > 0", name="ck_managed_import_intents_owner"),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["export_id"], ["exports.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("person_id", "owner_id", "export_id", name="uq_managed_import_intents_owner_export"),
    )
    op.create_table(
        "managed_import_receipts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("intent_id", sa.Integer(), nullable=False),
        sa.Column("resource_key", sa.String(), nullable=False),
        sa.Column("compound_key", sa.String(), nullable=False),
        sa.Column("source_sha1", sa.String(), nullable=False),
        sa.Column("expected_bytes", byte_count, nullable=False),
        sa.Column("expected_metadata", sa.Text(), nullable=False),
        sa.Column("state", sa.String(), server_default="prepared", nullable=False),
        sa.Column("asset_id", sa.String(), nullable=True),
        sa.Column("upload_outcome", sa.String(), nullable=True),
        sa.Column("verified_sha1", sa.String(), nullable=True),
        sa.Column("verified_bytes", byte_count, nullable=True),
        sa.Column("bytes_verified_at", sa.Float(), nullable=True),
        sa.Column("metadata_evidence", sa.Text(), nullable=True),
        sa.Column("metadata_verified_at", sa.Float(), nullable=True),
        sa.Column("committed_at", sa.Float(), nullable=True),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint("state IN ('prepared', 'outcome_unknown', 'returned', 'bytes_verified', 'metadata_verified', 'committed')", name="ck_managed_import_receipts_state"),
        sa.CheckConstraint("expected_bytes >= 0 AND (verified_bytes IS NULL OR verified_bytes >= 0)", name="ck_managed_import_receipts_bytes"),
        sa.CheckConstraint("length(source_sha1) = 40", name="ck_managed_import_receipts_source_sha1"),
        sa.CheckConstraint("length(resource_key) > 0 AND length(compound_key) > 0 AND (asset_id IS NULL OR length(asset_id) > 0)",
                           name="ck_managed_import_receipts_identity"),
        sa.CheckConstraint("upload_outcome IS NULL OR upload_outcome IN ('created', 'duplicate')", name="ck_managed_import_receipts_outcome"),
        sa.CheckConstraint("(state IN ('prepared', 'outcome_unknown') AND asset_id IS NULL AND upload_outcome IS NULL) OR "
                           "(state IN ('returned', 'bytes_verified', 'metadata_verified', 'committed') AND asset_id IS NOT NULL AND upload_outcome IS NOT NULL)",
                           name="ck_managed_import_receipts_asset"),
        sa.CheckConstraint("(state IN ('prepared', 'outcome_unknown', 'returned') AND verified_sha1 IS NULL AND verified_bytes IS NULL AND bytes_verified_at IS NULL) OR "
                           "(state IN ('bytes_verified', 'metadata_verified', 'committed') AND verified_sha1 IS NOT NULL AND verified_sha1 = source_sha1 "
                           "AND verified_bytes IS NOT NULL AND verified_bytes = expected_bytes AND bytes_verified_at IS NOT NULL)",
                           name="ck_managed_import_receipts_verified_bytes"),
        sa.CheckConstraint("(state IN ('prepared', 'outcome_unknown', 'returned', 'bytes_verified') AND metadata_evidence IS NULL AND metadata_verified_at IS NULL) OR "
                           "(state IN ('metadata_verified', 'committed') AND metadata_evidence IS NOT NULL AND metadata_verified_at IS NOT NULL)",
                           name="ck_managed_import_receipts_metadata"),
        sa.CheckConstraint("(state = 'committed' AND committed_at IS NOT NULL) OR (state != 'committed' AND committed_at IS NULL)",
                           name="ck_managed_import_receipts_commit"),
        sa.ForeignKeyConstraint(["intent_id"], ["managed_import_intents.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("intent_id", "resource_key", "source_sha1", name="uq_managed_import_receipts_resource"),
    )
    op.create_index("ix_managed_import_receipts_intent_state", "managed_import_receipts", ["intent_id", "state"])


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("LOCK TABLE managed_import_intents, managed_import_receipts IN ACCESS EXCLUSIVE MODE"))
    else:
        connection.execute(sa.text("UPDATE managed_import_intents SET id = id WHERE 1 = 0"))
    for table in ("managed_import_intents", "managed_import_receipts"):
        if connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError("the managed import journal is not empty; retain the additive schema and its receipts")
    op.drop_index("ix_managed_import_receipts_intent_state", table_name="managed_import_receipts")
    op.drop_table("managed_import_receipts")
    op.drop_table("managed_import_intents")
