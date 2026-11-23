from alembic import op
import sqlalchemy as sa


revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("people", schema=None) as batch_op:
        batch_op.add_column(sa.Column("managed_imports", sa.String(), nullable=True))


def downgrade() -> None:
    found = sa.inspect(op.get_bind())
    if "managed_imports" in {column["name"] for column in found.get_columns("people")}:
        op.execute("ALTER TABLE people DROP COLUMN managed_imports")
