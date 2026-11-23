from alembic import op
import sqlalchemy as sa


revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None

FAILURES = ("files", "database", "metadata", "placing", "memory", "unexpected")
TYPE_LIMIT = 64
FAILURE_RULE = f"ingest_failure IS NULL OR ingest_failure IN ({', '.join(repr(value) for value in FAILURES)})"
TYPE_RULE = f"ingest_failure_type IS NULL OR length(ingest_failure_type) BETWEEN 1 AND {TYPE_LIMIT}"


def upgrade() -> None:
    op.execute(f"ALTER TABLE export_parts ADD COLUMN ingest_failure VARCHAR CONSTRAINT ck_export_parts_ingest_failure CHECK ({FAILURE_RULE})")
    op.execute(f"ALTER TABLE export_parts ADD COLUMN ingest_failure_type VARCHAR CONSTRAINT ck_export_parts_ingest_failure_type CHECK ({TYPE_RULE})")


def downgrade() -> None:
    found = sa.inspect(op.get_bind())
    if "export_parts" not in found.get_table_names():
        return
    columns = {column["name"] for column in found.get_columns("export_parts")}
    for column in ("ingest_failure_type", "ingest_failure"):
        if column in columns:
            op.execute(f"ALTER TABLE export_parts DROP COLUMN {column}")
