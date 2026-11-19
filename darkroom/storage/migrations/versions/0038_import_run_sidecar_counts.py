from alembic import op
import sqlalchemy as sa


revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None

COLUMNS = ("sidecars_unreadable", "sidecars_repaired")


def upgrade() -> None:
    with op.batch_alter_table("import_runs", schema=None) as batch_op:
        for name in COLUMNS:
            batch_op.add_column(sa.Column(name, sa.Integer(), nullable=True))


def downgrade() -> None:
    found = sa.inspect(op.get_bind())
    present = {column["name"] for column in found.get_columns("import_runs")} if "import_runs" in found.get_table_names() else set()
    for name in COLUMNS:
        if name in present:
            op.execute(f"ALTER TABLE import_runs DROP COLUMN {name}")
