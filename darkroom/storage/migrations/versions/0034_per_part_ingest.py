from alembic import op
import sqlalchemy as sa


revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

Wide = sa.Integer().with_variant(sa.BigInteger(), "postgresql")
PART_COLUMNS = ("ingest_state", "ingest_queued_at", "ingested_at", "entries", "entries_done", "held", "failed_items")
PART_CHECKS = ("ck_export_parts_ingest_state", "ck_export_parts_entries", "ck_export_parts_ingest_counts")


def upgrade() -> None:
    with op.batch_alter_table("export_parts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("ingest_state", sa.String(), server_default="not_queued", nullable=False))
        batch_op.add_column(sa.Column("ingest_queued_at", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("ingested_at", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("entries", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("entries_done", sa.Integer(), server_default="0", nullable=False))
        batch_op.add_column(sa.Column("held", sa.Integer(), server_default="0", nullable=False))
        batch_op.add_column(sa.Column("failed_items", sa.Integer(), server_default="0", nullable=False))
        batch_op.create_check_constraint("ck_export_parts_ingest_state",
                                         "ingest_state IN ('not_queued', 'queued', 'ingesting', 'ingested', 'refused', 'failed')")
        batch_op.create_check_constraint("ck_export_parts_entries", "entries IS NULL OR entries >= 0")
        batch_op.create_check_constraint("ck_export_parts_ingest_counts", "entries_done >= 0 AND held >= 0 AND failed_items >= 0")
    op.create_table(
        "takeout_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("export_id", sa.Integer(), nullable=False),
        sa.Column("part_number", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("bytes", Wide, nullable=False),
        sa.Column("crc32", Wide, nullable=False),
        sa.Column("outcome", sa.String(), server_default="listed", nullable=False),
        sa.Column("metadata_pending", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("sha1", sa.String(), nullable=True),
        sa.Column("phash", sa.String(), nullable=True),
        sa.Column("captured_utc", sa.Float(), nullable=True),
        sa.Column("captured_local", sa.String(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("make", sa.String(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("is_video", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("is_edited", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("edited_of", sa.String(), nullable=True),
        sa.Column("copy_of_id", sa.Integer(), nullable=True),
        sa.Column("sidecar_entry_id", sa.Integer(), nullable=True),
        sa.Column("decision_id", sa.Integer(), nullable=True),
        sa.Column("item_id", sa.Integer(), nullable=True),
        sa.Column("held_path", sa.String(), nullable=True),
        sa.Column("listed_at", sa.Float(), nullable=False),
        sa.Column("done_at", sa.Float(), nullable=True),
        sa.CheckConstraint("kind IN ('media', 'sidecar', 'other')", name="ck_takeout_entries_kind"),
        sa.CheckConstraint("outcome IN ('listed', 'placed', 'review', 'skipped', 'copy', 'waiting_sidecar', 'waiting_export', 'failed', 'recorded')",
                           name="ck_takeout_entries_outcome"),
        sa.CheckConstraint("crc32 BETWEEN 0 AND 4294967295", name="ck_takeout_entries_crc32"),
        sa.CheckConstraint("bytes >= 0", name="ck_takeout_entries_bytes"),
        sa.ForeignKeyConstraint(["export_id", "part_number"], ["export_parts.export_id", "export_parts.number"], name="fk_takeout_entries_part", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["copy_of_id"], ["takeout_entries.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["sidecar_entry_id"], ["takeout_entries.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["decision_id"], ["dedupe_decisions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["item_id"], ["media_items.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("export_id", "part_number", "name", name="uq_takeout_entries_part_name"),
    )
    op.create_index("ix_takeout_entries_export_outcome", "takeout_entries", ["export_id", "outcome"])
    op.create_index("ix_takeout_entries_export_sha1", "takeout_entries", ["export_id", "sha1"])
    op.create_table(
        "takeout_sidecars",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("export_id", sa.Integer(), nullable=False),
        sa.Column("part_number", sa.Integer(), nullable=False),
        sa.Column("entry_id", sa.Integer(), nullable=False),
        sa.Column("folder", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["export_id", "part_number"], ["export_parts.export_id", "export_parts.number"], name="fk_takeout_sidecars_part", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["entry_id"], ["takeout_entries.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("entry_id", name="uq_takeout_sidecars_entry"),
        sa.UniqueConstraint("export_id", "part_number", "folder", "name", name="uq_takeout_sidecars_part_folder_name"),
    )
    op.create_index("ix_takeout_sidecars_export_folder", "takeout_sidecars", ["export_id", "folder"])


def downgrade() -> None:
    found = sa.inspect(op.get_bind()).get_table_names()
    if "takeout_sidecars" in found:
        op.drop_index("ix_takeout_sidecars_export_folder", table_name="takeout_sidecars")
        op.drop_table("takeout_sidecars")
    if "takeout_entries" in found:
        op.drop_index("ix_takeout_entries_export_sha1", table_name="takeout_entries")
        op.drop_index("ix_takeout_entries_export_outcome", table_name="takeout_entries")
        op.drop_table("takeout_entries")
    with op.batch_alter_table("export_parts", schema=None, recreate="always") as batch_op:
        for check in PART_CHECKS:
            batch_op.drop_constraint(check, type_="check")
        for column in PART_COLUMNS:
            batch_op.drop_column(column)
