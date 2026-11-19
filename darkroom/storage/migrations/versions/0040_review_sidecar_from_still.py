from alembic import op
import sqlalchemy as sa


revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("review_items", sa.Column("sidecar_from_still", sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    found = sa.inspect(op.get_bind())
    if "review_items" in found.get_table_names() and "sidecar_from_still" in {column["name"] for column in found.get_columns("review_items")}:
        op.execute("ALTER TABLE review_items DROP COLUMN sidecar_from_still")
