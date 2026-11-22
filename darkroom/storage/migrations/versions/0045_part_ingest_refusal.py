from alembic import op
import sqlalchemy as sa


revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None

REFUSALS = ("missing", "not_plain", "empty", "not_a_zip", "damaged", "symlink_entry", "outside", "file_and_folder", "device_entry", "unreadable_entry",
            "name_too_long", "duplicate_name", "too_many_entries", "too_large", "zip_bomb", "sidecars_too_big", "size_lies", "listing_differs", "bytes_differ")
RULE = f"ingest_refusal IS NULL OR ingest_refusal IN ({', '.join(repr(value) for value in REFUSALS)})"


def upgrade() -> None:
    op.execute(f"ALTER TABLE export_parts ADD COLUMN ingest_refusal VARCHAR CONSTRAINT ck_export_parts_ingest_refusal CHECK ({RULE})")


def downgrade() -> None:
    found = sa.inspect(op.get_bind())
    if "export_parts" in found.get_table_names() and "ingest_refusal" in {column["name"] for column in found.get_columns("export_parts")}:
        op.execute("ALTER TABLE export_parts DROP COLUMN ingest_refusal")
