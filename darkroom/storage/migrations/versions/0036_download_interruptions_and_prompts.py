from alembic import op
import sqlalchemy as sa


revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("exports", sa.Column("download_interruptions", sa.Integer(), server_default="0", nullable=False))
    op.add_column("export_parts", sa.Column("sign_in_prompts", sa.Integer(), server_default="0", nullable=False))
    op.execute("UPDATE export_parts SET sign_in_prompts = downloads_used - attempts WHERE downloads_used > attempts")


def downgrade() -> None:
    found = sa.inspect(op.get_bind())
    tables = found.get_table_names()
    if "export_parts" in tables and "sign_in_prompts" in {column["name"] for column in found.get_columns("export_parts")}:
        op.execute("ALTER TABLE export_parts DROP COLUMN sign_in_prompts")
    if "exports" in tables and "download_interruptions" in {column["name"] for column in found.get_columns("exports")}:
        op.execute("ALTER TABLE exports DROP COLUMN download_interruptions")
