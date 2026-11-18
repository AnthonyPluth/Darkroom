from alembic import op
import sqlalchemy as sa


revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

COLUMNS = ("sidecars_unreadable", "sidecars_repaired")


def upgrade() -> None:
    for name in COLUMNS:
        op.add_column("import_runs", sa.Column(name, sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    found = sa.inspect(op.get_bind())
    present = {column["name"] for column in found.get_columns("import_runs")} if "import_runs" in found.get_table_names() else set()
    for name in COLUMNS:
        if name in present:
            op.execute(f"ALTER TABLE import_runs DROP COLUMN {name}")
